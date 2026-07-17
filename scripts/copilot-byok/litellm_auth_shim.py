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
import asyncio
import ipaddress
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("litellm_auth_shim")

# ── Config (env-driven; no secrets in source) ────────────────────────────────
UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://127.0.0.1:8643").rstrip("/")
LISTEN_HOST = os.environ.get("SHIM_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SHIM_PORT", "8644"))
# The single valid bearer key. Required — refuse to start without it so we never
# accidentally run an open proxy.
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
# VS Code Insiders compatibility escape hatch. Disabled by default: when a
# particular custom-endpoint build silently drops the configured provider key,
# accept only a headerless request that arrived through the loopback listener.
# The shim injects the real key only on its private upstream hop to LiteLLM.
ALLOW_LOOPBACK_ANONYMOUS = os.environ.get(
    "HUSSH_SHIM_ALLOW_LOOPBACK_ANONYMOUS", "0"
).strip().lower() in {"1", "true", "yes", "on"}

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1). Also strip Host so
# httpx sets the correct upstream Host, and content-length (we re-stream).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def _is_loopback_request(request: Request) -> bool:
    """Return true only for a direct loopback TCP peer."""
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _is_missing_or_blank_bearer(raw_auth: str | None) -> bool:
    """Recognize the two credential-drop shapes emitted by VS Code Insiders."""
    return raw_auth is None or raw_auth.strip().lower() == "bearer"

# ── Graceful upstream recovery (the whole point of this layer) ────────────────
# The upstream LiteLLM proxy can die (OOM/jetsam on a big Opus turn) and be
# respawned by launchd in well under a second. We make that invisible to the
# client: before the first response byte, a connect/transient failure is RETRIED
# with bounded backoff instead of surfacing a 502. To retry safely we must be
# able to re-send the request, so the request body is buffered first.
#
# Memory note: buffering the request is cheap and safe. Even a ~1M-token Gemini
# context is only a few MB of JSON — orders of magnitude smaller than the LiteLLM
# proxy's own per-call buffering that caused the OOM. The shim never buffers the
# RESPONSE (still streamed), so long completions stay constant-memory.
_MAX_BUFFER_BYTES = int(os.environ.get("SHIM_MAX_BUFFER_MB", "64")) * 1024 * 1024
# Total seconds to keep retrying the INITIAL connect/send before giving up.
_RETRY_BUDGET_S = float(os.environ.get("SHIM_RETRY_BUDGET_S", "20"))
# Per-attempt backoff schedule (seconds), clamped to the budget.
_BACKOFF = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 3.0, 3.0)

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


async def _read_body_bounded(request: Request) -> bytes | None:
    """Buffer the full request body, capped at _MAX_BUFFER_BYTES.

    Returns None if the body exceeds the cap (caller should fall back to a
    non-retryable streamed passthrough). Buffering is what makes transparent
    retry possible — we can re-send the exact same bytes on a fresh upstream.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BUFFER_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _is_streaming_request(body: bytes, content_type: str) -> bool:
    """Best-effort: does this chat request ask for SSE streaming?"""
    if "application/json" not in (content_type or ""):
        return False
    try:
        import json as _json
        return bool(_json.loads(body or b"{}").get("stream"))
    except Exception:
        return False


# ── Transcript scrub (fixes the "[System: Empty message content …]" loop) ────
# LiteLLM's Anthropic prompt factory replaces EMPTY message content with the
# literal placeholder below (factory.py: _sanitize_empty_text_content) because
# the Anthropic API rejects empty text blocks. Copilot's agent mode routinely
# produces assistant turns with empty content (tool-call-only "Edited file"
# turns), so on Claude models the placeholder gets injected into the model's
# context, the model starts ECHOING it in its visible output, the echo lands
# in the next transcript, and the loop amplifies. We fix it at the source:
#  * strip placeholder echoes wherever they appear (breaks the feedback loop),
#  * assistant turns with tool_calls + empty content → content: null (LiteLLM
#    builds pure tool_use blocks for those; no text block, no sanitizer),
#  * assistant turns with NO tool_calls and no content → dropped (carry no
#    information; Anthropic rejects them),
#  * empty user turns → "." (inert; sanitizer never fires).
_LITELLM_EMPTY_PLACEHOLDER = (
    "[System: Empty message content sanitised to satisfy protocol]"
)


def _scrub_chat_body(body: bytes, content_type: str) -> bytes:
    """Sanitize an OpenAI-format chat body so LiteLLM's Anthropic empty-text
    placeholder never enters the model context. Fails open — any parse issue
    returns the body untouched."""
    if "application/json" not in (content_type or ""):
        return body
    try:
        import json as _json
        data = _json.loads(body)
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            return body
        changed = False
        out: list = []
        for m in msgs:
            if not isinstance(m, dict):
                out.append(m)
                continue
            role = m.get("role")
            content = m.get("content")

            # 1) Strip placeholder echoes (string + list-of-blocks shapes).
            if isinstance(content, str) and _LITELLM_EMPTY_PLACEHOLDER in content:
                content = content.replace(_LITELLM_EMPTY_PLACEHOLDER, "").strip()
                m = {**m, "content": content}
                changed = True
            elif isinstance(content, list):
                new_blocks = []
                block_changed = False
                for blk in content:
                    if (isinstance(blk, dict)
                            and isinstance(blk.get("text"), str)
                            and _LITELLM_EMPTY_PLACEHOLDER in blk["text"]):
                        text = blk["text"].replace(_LITELLM_EMPTY_PLACEHOLDER, "").strip()
                        block_changed = True
                        if text:
                            new_blocks.append({**blk, "text": text})
                        # empty after strip → drop the block
                    else:
                        new_blocks.append(blk)
                if block_changed:
                    m = {**m, "content": new_blocks}
                    content = new_blocks
                    changed = True

            def _is_empty(c) -> bool:
                if c is None:
                    return True
                if isinstance(c, str):
                    return not c.strip()
                if isinstance(c, list):
                    return not any(
                        (isinstance(b, dict)
                         and (b.get("type") != "text"
                              or (isinstance(b.get("text"), str) and b["text"].strip())))
                        for b in c
                    )
                return False

            # 2) Normalize empty-content turns so LiteLLM's sanitizer never fires.
            if role == "assistant" and _is_empty(content):
                if m.get("tool_calls"):
                    if content is not None:
                        m = {**m, "content": None}
                        changed = True
                else:
                    changed = True
                    continue  # informationless — drop
            elif role == "user" and _is_empty(content):
                m = {**m, "content": "."}
                changed = True

            out.append(m)
        if not changed:
            return body
        data["messages"] = out
        return _json.dumps(data).encode("utf-8")
    except Exception:
        return body


# ── Gemini tool-schema sanitizer (fixes hard 400s on Vertex function calling) ─
# Vertex's Gemini function-calling API rejects ANY tool whose `parameters`
# schema carries a ROOT-LEVEL `anyOf` / `oneOf` / `allOf` — even when
# `type: "object"` is also present — with a hard, non-retryable 400:
#   "functionDeclaration parameters schema should be of type OBJECT"
# Claude (via the same Vertex proxy, same shim) accepts this shape natively.
# MCP servers routinely emit root-level `anyOf` to express conditional
# requirements ("provide `scope` OR `request_id`") — e.g. the real
# `hushh-consent` MCP server's `check_consent_status` and `request_consent`
# tools both do this. Without this fix, EVERY Gemini model in the BYOK
# lineup 400s on any turn where Copilot includes one of these tools in its
# `tools` array — even if the model never calls it — because Vertex validates
# the full tool manifest up front, not per-call.
#
# Fix: for Gemini-bound requests only (Claude is untouched — it doesn't need
# this and we never want to silently reshape a schema more than necessary),
# strip the root-level anyOf/oneOf/allOf and fold its meaning into the tool
# description as a plain-English constraint, so Gemini can still read intent
# even though the JSON Schema conditional is gone. Never touches nested
# anyOf inside `properties` (Vertex's schema builder already handles those
# — see litellm's `_build_vertex_schema`/`process_schema`).
def _is_gemini_model(model: str) -> bool:
    return isinstance(model, str) and "gemini" in model.lower()


def _describe_requirement_group(group: dict) -> str | None:
    """Turn a single anyOf/oneOf branch like {"required": ["scope"]} into a
    readable fragment ("scope"). Returns None for branches we can't describe
    simply (composite/nested) — caller falls back to a generic note."""
    if not isinstance(group, dict):
        return None
    required = group.get("required")
    if isinstance(required, list) and required and all(isinstance(r, str) for r in required):
        return " and ".join(required)
    return None


def _sanitize_gemini_tool_schema(schema: dict) -> dict:
    """Strip root-level anyOf/oneOf/allOf from a single tool's parameters
    schema, folding the constraint into an appended note on the schema's
    (or the tool's) description where we can express it simply."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    removed_notes: list[str] = []
    for key in ("anyOf", "oneOf"):
        branches = out.pop(key, None)
        if not branches or not isinstance(branches, list):
            continue
        descs = [_describe_requirement_group(b) for b in branches]
        if all(descs):
            joiner = " or " if key == "anyOf" else " (exactly one of) "
            removed_notes.append(
                f"Must also provide {joiner.join(f'`{d}`' for d in descs)}."
            )
        else:
            removed_notes.append(
                "This tool has additional conditional field requirements not "
                "expressible in this schema — check the tool description."
            )
    # allOf on parameters is rare and usually just a merge — drop it too if
    # present, since Vertex applies the same "must be OBJECT" restriction to
    # any composition keyword at the root, not anyOf/oneOf specifically.
    if "allOf" in out:
        out.pop("allOf", None)
        removed_notes.append(
            "This tool composes additional schema constraints not "
            "expressible here — check the tool description."
        )
    if removed_notes:
        note = " ".join(removed_notes)
        existing_desc = out.get("description")
        out["description"] = (
            f"{existing_desc.rstrip()} {note}" if isinstance(existing_desc, str) and existing_desc.strip()
            else note
        )
    return out


def _scrub_tools_for_gemini(body: bytes, content_type: str) -> bytes:
    """If this is a Gemini-bound chat request carrying a `tools` array,
    strip any root-level anyOf/oneOf/allOf from each tool's parameters
    schema so Vertex's function-calling validator accepts the manifest.
    No-op (fails open) for non-Gemini models, missing tools, or parse
    errors — Claude requests are never touched by this path."""
    if "application/json" not in (content_type or ""):
        return body
    try:
        import json as _json
        data = _json.loads(body)
        if not _is_gemini_model(data.get("model", "")):
            return body
        tools = data.get("tools")
        if not isinstance(tools, list) or not tools:
            return body
        changed = False
        new_tools = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                new_tools.append(tool)
                continue
            fn = tool.get("function")
            if not isinstance(fn, dict):
                new_tools.append(tool)
                continue
            params = fn.get("parameters")
            if not isinstance(params, dict) or not any(
                k in params for k in ("anyOf", "oneOf", "allOf")
            ):
                new_tools.append(tool)
                continue
            sanitized_params = _sanitize_gemini_tool_schema(params)
            new_tools.append({**tool, "function": {**fn, "parameters": sanitized_params}})
            changed = True
            logger.warning(
                "shim: stripped root-level anyOf/oneOf/allOf from tool "
                "'%s' for Gemini model '%s' (Vertex rejects this shape); "
                "folded constraint into description.",
                fn.get("name", "<unnamed>"), data.get("model", ""),
            )
        if not changed:
            return body
        data["tools"] = new_tools
        return _json.dumps(data).encode("utf-8")
    except Exception:
        return body


async def _send_with_retry(method: str, url: str, headers, body: bytes):
    """Send to upstream, retrying connect/transient failures with bounded
    backoff UNTIL THE FIRST RESPONSE BYTE. Safe because the body is buffered
    and we have not yet emitted anything to the client. Once we get a response
    object back (headers received), we stop retrying — the response is then
    streamed and any mid-stream death is handled by the body iterator.
    """
    assert _client is not None
    deadline = time.monotonic() + _RETRY_BUDGET_S
    attempt = 0
    last_exc: Exception | None = None
    while True:
        try:
            req = _client.build_request(method, url, headers=headers, content=body)
            resp = await _client.send(req, stream=True)
            return resp, None
        except (httpx.ConnectError, httpx.ConnectTimeout,
                httpx.RemoteProtocolError, httpx.ReadError,
                httpx.WriteError, httpx.PoolTimeout) as e:
            # Transient — upstream is down/restarting (launchd is respawning it)
            # or dropped the connection before sending headers. Retry until the
            # budget is exhausted.
            last_exc = e
            if time.monotonic() >= deadline:
                return None, last_exc
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            delay = min(delay, max(0.0, deadline - time.monotonic()))
            attempt += 1
            logger.warning(
                "shim: upstream unavailable (%s); retry %d in %.2fs (budget %.1fs)",
                type(e).__name__, attempt, delay, _RETRY_BUDGET_S,
            )
            await asyncio.sleep(delay)
        except httpx.HTTPError as e:
            # Non-retryable transport error.
            return None, e


async def _proxy(request: Request) -> Response:
    # ── Auth gate (the whole reason this shim exists) ──
    if not MASTER_KEY:
        # Fail closed — never serve as an open relay.
        return JSONResponse(
            {"error": {"message": "shim misconfigured: no key", "type": "server_error"}},
            status_code=503,
        )
    token = _extract_bearer(request)
    accepted_anonymous_loopback = False
    if token is None:
        raw_auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if (
            ALLOW_LOOPBACK_ANONYMOUS
            and _is_missing_or_blank_bearer(raw_auth)
            and _is_loopback_request(request)
        ):
            accepted_anonymous_loopback = True
            logger.warning("Accepted missing/blank-bearer request via loopback compatibility mode.")
        else:
            # Redact: log only whether a header was present and its scheme/length,
            # never the raw value — a malformed header can still contain a real
            # (if truncated/garbled) credential, and this log file is not a secret
            # store. Full header dump likewise dropped for the same reason.
            auth_shape = (
                "absent" if raw_auth is None
                else f"present (len={len(raw_auth)}, scheme={raw_auth.split(None, 1)[0]!r})" if raw_auth
                else "empty"
            )
            logger.error(f"Auth failed: token is None. Authorization header: {auth_shape}.")
            return _unauthorized("Missing or malformed Authorization header. Expected: 'Bearer <key>'.")
    else:
        # Constant-time compare to avoid timing oracles on the key.
        import hmac
        if not hmac.compare_digest(token, MASTER_KEY):
            logger.error(f"Auth failed: token mismatch (received len={len(token)}, expected len={len(MASTER_KEY)}).")
            return _unauthorized("Invalid API key.")

    assert _client is not None
    url = f"{UPSTREAM}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    fwd_headers = [
        (k, v) for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    ]
    if accepted_anonymous_loopback:
        # A blank `Bearer` header must not reach LiteLLM beside the injected
        # credential: duplicate Authorization headers are rejected upstream.
        fwd_headers = [(k, v) for k, v in fwd_headers if k.lower() != "authorization"]
        fwd_headers.append(("authorization", f"Bearer {MASTER_KEY}"))

    # Buffer the request so we can retry transparently across an upstream
    # restart. Cap protects against a pathological body; over the cap we fall
    # back to a single streamed attempt (still correct, just not retryable).
    body = await _read_body_bounded(request)
    content_type = request.headers.get("content-type", "")
    if body is not None:
        # DEBUG CAPTURE (temporary, env-gated, zero cost when unset): dump the
        # raw pre-sanitization body of any Gemini request carrying `tools` so
        # we can inspect the REAL schema VS Code Copilot sends for its native
        # built-in tools (copilot_replaceString, run_in_terminal, etc.) — not
        # just MCP server tools. Controlled by HUSSH_SHIM_CAPTURE_TOOLS=1.
        if os.environ.get("HUSSH_SHIM_CAPTURE_TOOLS") == "1":
            try:
                import json as _json
                _peek = _json.loads(body)
                if _is_gemini_model(_peek.get("model", "")) and _peek.get("tools"):
                    import time as _time
                    _cap_path = f"/tmp/hussh_shim_capture_{int(_time.time()*1000)}.json"
                    with open(_cap_path, "wb") as _f:
                        _f.write(body)
                    logger.warning("shim: captured Gemini tools request to %s", _cap_path)
            except Exception:
                pass
        # Scrub the transcript so LiteLLM's Anthropic empty-content placeholder
        # never enters (or re-enters) the model context. No-op for non-chat
        # bodies; fails open on parse errors. httpx recomputes content-length
        # from the (possibly shorter) scrubbed bytes.
        body = _scrub_chat_body(body, content_type)
        # Strip root-level anyOf/oneOf/allOf from tool schemas on Gemini-bound
        # requests — Vertex's function-calling validator hard-400s on this
        # shape (real MCP tools like hushh-consent's check_consent_status
        # use it). No-op for Claude / non-tool requests. Order doesn't matter
        # relative to the scrub above — they touch disjoint JSON keys.
        body = _scrub_tools_for_gemini(body, content_type)
    wants_stream = body is not None and _is_streaming_request(body, content_type)

    if body is None:
        # Oversized: single streamed attempt, no retry.
        try:
            req = _client.build_request(request.method, url, headers=fwd_headers,
                                        content=request.stream())
            upstream_resp = await _client.send(req, stream=True)
            err = None
        except httpx.HTTPError as e:
            upstream_resp, err = None, e
    else:
        upstream_resp, err = await _send_with_retry(
            request.method, url, fwd_headers, body)

    if upstream_resp is None:
        # Exhausted the retry budget — upstream never came back in time. This is
        # the rare hard-down case; surface a clean, correctly-typed error.
        detail = f"upstream proxy unavailable after retries: {type(err).__name__ if err else 'unknown'}"
        logger.error("shim: %s", detail)
        return JSONResponse(
            {"error": {"message": detail, "type": "server_error",
                       "code": "upstream_unavailable"}},
            status_code=503,  # 503 (try again) is more honest than 502 here
            headers={"Retry-After": "2"},
        )

    resp_headers = [
        (k, v) for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    ]

    async def _body_iter():
        """Stream the response. If upstream dies MID-STREAM (after headers, so
        we can't change the status code), emit a graceful tail instead of a raw
        truncation: for an SSE stream, a final error event + [DONE]; otherwise
        just close. This keeps the client from hanging on an incomplete read.
        """
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        except (httpx.RemoteProtocolError, httpx.ReadError,
                httpx.StreamError) as e:
            logger.error("shim: upstream died mid-stream (%s)", type(e).__name__)
            if wants_stream:
                # OpenAI SSE-shaped error so Copilot renders it as a message
                # rather than a hard transport failure, then a clean terminator.
                import json as _json
                payload = _json.dumps({
                    "error": {
                        "message": ("upstream interrupted mid-response (proxy "
                                    "restarted). Please retry — the service is "
                                    "back up."),
                        "type": "server_error",
                        "code": "upstream_interrupted",
                    }
                })
                yield f"\ndata: {payload}\n\n".encode()
                yield b"data: [DONE]\n\n"
            # Non-stream: nothing more we can safely append; just stop.
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
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # h11 has no body-size cap by default → large context bodies pass freely.
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="warning",
                timeout_keep_alive=75)
