#!/usr/bin/env python3
"""
Hussh One — LiteLLM auth shim (deterministic 401 + streaming passthrough)

WHY THIS EXISTS
LiteLLM run DB-less (no Postgres) mislabels auth failures: a missing
Authorization header returns HTTP 500 and a wrong key returns HTTP 400, when
both should be 401. A wrong status code can make clients (VS Code Copilot, any
OpenAI SDK) retry a permanent auth error as if it were a transient server fault,
or conclude the proxy is down when it is actually an auth problem. This shim sits
in FRONT of LiteLLM and gives correct, deterministic auth semantics:

    missing/empty/malformed bearer  -> 401
    wrong key                       -> 401
    correct key                     -> transparent passthrough to LiteLLM

SCALING (large + refilling context windows)
The whole point of the Vertex passthrough is big context (Gemini 3.5 Flash =
~1M input tokens). So the shim must NEVER buffer a whole request or response in
memory and must not impose body-size caps or short timeouts:

  * Request bodies are STREAMED upstream (request.stream()), so a multi-MB chat
    payload (long transcript that Copilot refills each turn) is forwarded chunk
    by chunk — constant memory, no 413/size ceiling.
  * Response bodies are STREAMED back (httpx stream + StreamingResponse), so SSE
    token deltas reach the client immediately and a long completion never has to
    fit in RAM.
  * Timeouts: generous connect timeout, but NO read/total timeout (read=None) —
    a large-context first-token can take tens of seconds; we must not cut it off.
  * No max content length — uvicorn/h11 default has no body cap; we add none.

The shim is itself stateless and cheap, so it scales with concurrency: each
request is an independent streamed pipe. It holds one shared httpx.AsyncClient
(connection pooling / keep-alive) for upstream efficiency.

This is the repo-canonical source. `scripts/hussh-one-copilot-setup.sh` copies it
to ~/.hermes/scripts/litellm_auth_shim.py at install time.
"""
import os
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# ── Config (env-driven; no secrets in source) ────────────────────────────────
UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://127.0.0.1:8643").rstrip("/")
LISTEN_HOST = os.environ.get("SHIM_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SHIM_PORT", "8644"))
# The single valid bearer key. Required — refuse to start without it so we never
# accidentally run an open proxy.
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1). Also strip Host so
# httpx sets the correct upstream Host, and content-length (we re-stream).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Connect quickly, but allow unbounded read/write for large-context first-token
# and long streamed completions. pool timeout guards against pool exhaustion.
_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=None, pool=30.0)
_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16)

_client: httpx.AsyncClient | None = None


def _unauthorized(detail: str) -> JSONResponse:
    # OpenAI-shaped error so SDK clients parse it natively.
    return JSONResponse(
        {
            "error": {
                "message": detail,
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": None,
            }
        },
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or request.headers.get("Authorization")
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def _proxy(request: Request) -> Response:
    # ── Auth gate (the whole reason this shim exists) ──
    if not MASTER_KEY:
        # Fail closed — never serve as an open relay.
        return JSONResponse(
            {"error": {"message": "shim misconfigured: no key", "type": "server_error"}},
            status_code=503,
        )
    token = _extract_bearer(request)
    if token is None:
        return _unauthorized("Missing or malformed Authorization header. Expected: 'Bearer <key>'.")
    # Constant-time compare to avoid timing oracles on the key.
    import hmac
    if not hmac.compare_digest(token, MASTER_KEY):
        return _unauthorized("Invalid API key.")

    # ── Build upstream request, streaming the body (no buffering) ──
    assert _client is not None
    url = f"{UPSTREAM}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    fwd_headers = [
        (k, v) for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    ]

    upstream_req = _client.build_request(
        request.method,
        url,
        headers=fwd_headers,
        content=request.stream(),  # async generator → true streaming upload
    )

    try:
        upstream_resp = await _client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        return JSONResponse(
            {"error": {"message": "upstream proxy unavailable", "type": "server_error",
                       "code": "upstream_unavailable"}},
            status_code=502,
        )
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": {"message": f"upstream error: {type(e).__name__}", "type": "server_error"}},
            status_code=502,
        )

    resp_headers = [
        (k, v) for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    ]

    async def _body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        _body_iter(),
        status_code=upstream_resp.status_code,
        headers=dict(resp_headers),
        media_type=upstream_resp.headers.get("content-type"),
    )


async def _health(request: Request) -> Response:
    # Liveness is unauthenticated (no secret revealed); also probes upstream.
    assert _client is not None
    try:
        r = await _client.get(f"{UPSTREAM}/health/liveliness", timeout=5.0)
        up = r.status_code == 200
    except Exception:
        up = False
    return JSONResponse({"shim": "alive", "upstream": "alive" if up else "down"},
                        status_code=200 if up else 503)


@asynccontextmanager
async def _lifespan(app):
    # Starlette >=1.0 uses a lifespan context manager instead of
    # on_startup/on_shutdown. One shared pooled client for the process.
    global _client
    _client = httpx.AsyncClient(timeout=_TIMEOUT, limits=_LIMITS)
    try:
        yield
    finally:
        if _client is not None:
            await _client.aclose()
            _client = None


app = Starlette(
    lifespan=_lifespan,
    routes=[
        Route("/healthz", _health, methods=["GET"]),
        # Catch-all: every other path is auth-gated and proxied.
        Route("/{path:path}", _proxy,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
    ],
)


if __name__ == "__main__":
    if not MASTER_KEY:
        print("FATAL: LITELLM_MASTER_KEY not set; refusing to start open relay.",
              file=sys.stderr)
        sys.exit(2)
    # h11 has no body-size cap by default → large context bodies pass freely.
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning",
                timeout_keep_alive=75)
