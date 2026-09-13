"""Puppy One's inference-only outbound relay client.

This process is a local-model adapter, not the Hermes agent loop. It dials the
Hussh hub relay while the trusted profile is enabled, forwards one bounded
request at a time to the configured local OpenAI-compatible model endpoint,
and returns text/tool-call frames. No shell, filesystem, MCP, or Hermes tool
executor is reachable through this client.

The device says what it is and what it can do. ``relay.hello`` carries the
resident model id and a capability profile in the Puppy One harness vocabulary
(``hermes_cli/hussh_one_routing/profile.py``: ``tool_calling``, ``json_schema``,
``streaming``, plus ``probe_mode``), and every ``inference.result`` names the
model that answered. A request that needs a capability this profile lacks is
refused with ``inference.error code=UNSUPPORTED_CAPABILITY`` BEFORE the local
model is called: a schema or tool choice that was silently dropped produced an
answer that looked right and was not the one the pod asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
from typing import Any
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)
_MAX_FRAME_BYTES = 1_048_576
_DEFAULT_MODEL_TIMEOUT = 60.0
_DEFAULT_HEARTBEAT_SECONDS = 30.0
_MAX_MODEL_ID_LENGTH = 128

#: The capability names this relay declares, in the harness vocabulary. An
#: OpenAI-compatible chat endpoint supports all three unless the operator says
#: otherwise; the declaration is what lets the pod refuse before dispatch.
CAPABILITY_NAMES = ("tool_calling", "json_schema", "streaming")
DEFAULT_CAPABILITIES = {name: True for name in CAPABILITY_NAMES}
UNSUPPORTED_CAPABILITY_CODE = "UNSUPPORTED_CAPABILITY"
_PROBE_SUITE_ID = "puppy-inference-relay"
_PROBE_OUTPUT_PROTOCOL = "openai_chat_completions"


def _capabilities(value: dict[str, Any] | None) -> dict[str, bool]:
    """Allowlisted names with boolean values; anything else falls back to default."""
    declared = dict(DEFAULT_CAPABILITIES)
    for name, flag in (value or {}).items():
        if name in CAPABILITY_NAMES and isinstance(flag, bool):
            declared[name] = flag
    return declared


def _reportable_model(value: Any) -> str:
    """A model id the wire may carry: bounded, no scheme, no path, no whitespace.

    The local endpoint and any key must never leave the device, so a model field
    that looks like either is not reported at all.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > _MAX_MODEL_ID_LENGTH:
        return ""
    if "://" in text or text.startswith("/") or any(char.isspace() for char in text):
        return ""
    return text


def _response_format(value: Any) -> dict[str, Any] | None:
    """Neutral ``responseFormat`` -> OpenAI ``response_format``."""
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or "")
    schema = value.get("jsonSchema")
    if kind == "json_schema" and isinstance(schema, dict):
        return {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": schema},
        }
    if kind in {"json", "json_object"}:
        return {"type": "json_object"}
    return None


_TOOL_CHOICE = {"auto": "auto", "any": "required", "none": "none"}


def needs_capability(request: dict[str, Any]) -> list[str]:
    """Capability names one neutral request needs, in check order."""
    needed: list[str] = []
    if (
        request.get("tools")
        or request.get("toolChoice")
        or request.get("allowedFunctionNames")
    ):
        needed.append("tool_calling")
    if request.get("responseFormat"):
        needed.append("json_schema")
    return needed


def _messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = body.get("systemInstruction")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    for item in body.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        message: dict[str, Any] = {"role": role, "content": str(item.get("text") or "")}
        if role == "assistant" and item.get("toolName"):
            message["tool_calls"] = [
                {
                    "id": str(item.get("toolCallId") or f"call_{item['toolName']}"),
                    "type": "function",
                    "function": {
                        "name": str(item["toolName"]),
                        "arguments": json.dumps(
                            item.get("toolArguments") or {}, separators=(",", ":")
                        ),
                    },
                }
            ]
        elif role == "tool" and (item.get("toolName") or item.get("toolCallId")):
            message["tool_call_id"] = str(item.get("toolCallId") or "")
            message["content"] = json.dumps(
                item.get("toolResult"), separators=(",", ":")
            )
        messages.append(message)
    return messages


def _tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "parameters": item.get("parameters")
                or {"type": "object", "properties": {}},
            },
        }
        for item in body.get("tools") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


class PuppyInferenceRelay:
    def __init__(
        self,
        *,
        relay_url: str | None = None,
        token: str | None = None,
        device_id: str | None = None,
        model_url: str | None = None,
        model: str | None = None,
        model_api_key: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self.relay_url = (relay_url or os.getenv("PUPPY_RELAY_URL") or "").strip()
        self.token = (token or os.getenv("PUPPY_RELAY_TOKEN") or "").strip()
        self.device_id = (device_id or os.getenv("PUPPY_DEVICE_ID") or "").strip()
        self.model_url = (
            model_url
            or os.getenv("PUPPY_LOCAL_MODEL_URL")
            or "http://127.0.0.1:1234/v1"
        ).rstrip("/")
        self.model = (model or os.getenv("PUPPY_LOCAL_MODEL") or "local").strip()
        self.model_api_key = (
            model_api_key or os.getenv("PUPPY_LOCAL_MODEL_API_KEY") or ""
        )
        self.model_timeout = float(
            os.getenv("PUPPY_LOCAL_MODEL_TIMEOUT_SECONDS") or _DEFAULT_MODEL_TIMEOUT
        )
        self.capabilities = _capabilities(capabilities)
        self.probe_mode = self._probe_mode()
        if not self.relay_url.startswith(("wss://", "ws://")):
            raise ValueError("PUPPY_RELAY_URL must be a ws:// or wss:// URL")
        if not self.token or not self.device_id:
            raise ValueError("PUPPY_RELAY_TOKEN and PUPPY_DEVICE_ID are required")
        model_origin = urlparse(self.model_url)
        if model_origin.scheme not in {
            "http",
            "https",
        } or model_origin.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError(
                "PUPPY_LOCAL_MODEL_URL must point to a loopback model endpoint"
            )

    def _probe_mode(self) -> str:
        """The harness comparability key for this relay's declared profile.

        Built from the same ``CapabilityProfile`` the harness serialises, so the
        receipt vocabulary is one vocabulary. No recommendation is measured here,
        and the key says so (``?``) rather than inventing an effort or budget.
        """
        from hermes_cli.hussh_one_routing.profile import (
            PROFILE_SCHEMA_VERSION,
            Capability,
            CapabilityProfile,
        )

        profile = CapabilityProfile(
            schema_version=PROFILE_SCHEMA_VERSION, model=self.model
        )
        for name, supported in self.capabilities.items():
            profile.capabilities[name] = Capability(
                name=name, supported=supported, evidence="declared by the relay profile"
            )
        return profile.probe_mode(_PROBE_SUITE_ID, _PROBE_OUTPUT_PROTOCOL)

    def hello(self) -> dict[str, Any]:
        """The admission frame: identity plus what this device can do. Never the
        endpoint, never a key."""
        return {
            "type": "relay.hello",
            "role": "device",
            "deviceId": self.device_id,
            "model": _reportable_model(self.model),
            "capabilities": dict(self.capabilities),
            "probe_mode": self.probe_mode,
        }

    def unsupported_capability(self, request: dict[str, Any]) -> str:
        """The first capability this request needs that the profile lacks, or ''."""
        for name in needs_capability(request):
            if not self.capabilities.get(name, False):
                return name
        return ""

    def _payload(self, request: dict[str, Any]) -> dict[str, Any]:
        """Neutral ``inference.request`` -> OpenAI-compatible chat completion body.

        Every knob the pod set is mapped or the request was refused upstream;
        nothing is dropped on the floor. ``thinking`` has no portable field on
        this endpoint family and is not mapped; the pod is told so in the docs,
        not by silence here.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(request),
            "stream": True,
        }
        tools = _tools(request)
        allowed = [
            str(name)
            for name in (request.get("allowedFunctionNames") or [])
            if str(name or "")
        ]
        if tools and allowed:
            tools = [
                tool for tool in tools if tool["function"]["name"] in allowed
            ] or tools
        if tools:
            payload["tools"] = tools
        choice = _TOOL_CHOICE.get(str(request.get("toolChoice") or ""))
        if len(allowed) == 1 and tools and choice != "none":
            # Function selection where the endpoint supports it: one allowed name
            # is exactly the OpenAI single-function form.
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": allowed[0]},
            }
        elif choice is not None and tools:
            payload["tool_choice"] = choice
        response_format = _response_format(request.get("responseFormat"))
        if response_format is not None:
            payload["response_format"] = response_format
        if request.get("temperature") is not None:
            payload["temperature"] = request["temperature"]
        if request.get("maxOutputTokens") is not None:
            payload["max_tokens"] = request["maxOutputTokens"]
        if request.get("topP") is not None:
            payload["top_p"] = request["topP"]
        stops = [
            str(stop)
            for stop in (request.get("stopSequences") or [])
            if str(stop or "")
        ]
        if stops:
            payload["stop"] = stops
        seed = request.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            payload["seed"] = seed
        return payload

    async def _heartbeat(self, websocket: Any) -> None:
        try:
            interval = float(
                os.getenv("PUPPY_RELAY_HEARTBEAT_SECONDS") or _DEFAULT_HEARTBEAT_SECONDS
            )
        except ValueError:
            interval = _DEFAULT_HEARTBEAT_SECONDS
        interval = max(10.0, min(interval, 120.0))
        while True:
            await asyncio.sleep(interval + random.uniform(-2.0, 2.0))
            await websocket.send(
                json.dumps(
                    {
                        "type": "relay.heartbeat",
                        "status": "ready",
                        "deviceId": self.device_id,
                    },
                    separators=(",", ":"),
                )
            )

    async def _infer(self, request: dict[str, Any], websocket: Any) -> None:
        request_id = str(request.get("requestId") or "")
        if not request_id:
            return
        lacking = self.unsupported_capability(request)
        if lacking:
            # Refuse BEFORE the local model is called. The pod maps this code to a
            # typed refusal, never to "unavailable" and never to a fallback.
            await websocket.send(
                json.dumps({
                    "type": "inference.error",
                    "requestId": request_id,
                    "code": UNSUPPORTED_CAPABILITY_CODE,
                    "capability": lacking,
                })
            )
            return
        payload = self._payload(request)
        headers = (
            {"Authorization": f"Bearer {self.model_api_key}"}
            if self.model_api_key
            else {}
        )
        calls: dict[int, dict[str, Any]] = {}
        observed_model = ""
        lease = None
        try:
            from hermes_cli.hussh_one_routing.local_runtime import (
                LocalInferenceAdmission,
                LocalModelOverloaded,
                normalize_front_url,
            )

            admission_key = f"{normalize_front_url(self.model_url)}|{self.model}"

            # The relay is already a single async consumer, but the shared
            # process-wide permit also accounts for Hermes chat/cron calls.
            # Never wait behind an interactive request long enough to make the
            # hub believe this device disappeared.
            lease = await asyncio.to_thread(
                LocalInferenceAdmission.acquire,
                admission_key,
                wait=min(0.25, max(0.0, self.model_timeout / 10.0)),
                priority="interactive",
            )
        except LocalModelOverloaded:
            await websocket.send(
                json.dumps({
                    "type": "inference.error",
                    "requestId": request_id,
                    "code": "LOCAL_MODEL_OVERLOADED",
                })
            )
            return
        try:
            async with httpx.AsyncClient(timeout=self.model_timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.model_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        observed_model = observed_model or _reportable_model(
                            chunk.get("model")
                        )
                        choices = chunk.get("choices") or []
                        delta = (
                            choices[0].get("delta")
                            if choices and isinstance(choices[0], dict)
                            else {}
                        )
                        text = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(text, str) and text:
                            await websocket.send(
                                json.dumps({
                                    "type": "inference.delta",
                                    "requestId": request_id,
                                    "text": text,
                                })
                            )
                        for call in (
                            (delta.get("tool_calls") or [])
                            if isinstance(delta, dict)
                            else []
                        ):
                            if not isinstance(call, dict):
                                continue
                            index = int(call.get("index") or 0)
                            target = calls.setdefault(
                                index, {"id": "", "name": "", "args": ""}
                            )
                            target["id"] = target["id"] or str(call.get("id") or "")
                            function = call.get("function") or {}
                            target["name"] = target["name"] or str(
                                function.get("name") or ""
                            )
                            target["args"] += str(function.get("arguments") or "")
            function_calls: list[dict[str, Any]] = []
            for call in calls.values():
                try:
                    args = json.loads(call["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if call["name"]:
                    function_calls.append({
                        "id": call["id"],
                        "name": call["name"],
                        "args": args,
                    })
            await websocket.send(
                json.dumps({
                    "type": "inference.result",
                    "requestId": request_id,
                    "functionCalls": function_calls,
                    # The model that answered: what the server reported for
                    # this completion, else the configured resident id.
                    "model": observed_model or _reportable_model(self.model),
                })
            )
            await websocket.send(
                json.dumps({"type": "inference.done", "requestId": request_id})
            )
        except Exception:  # noqa: BLE001 - do not disclose local endpoint or credentials
            await websocket.send(
                json.dumps({
                    "type": "inference.error",
                    "requestId": request_id,
                    "code": "LOCAL_MODEL_UNAVAILABLE",
                })
            )
        finally:
            if lease is not None:
                lease.release()

    async def serve(self) -> None:
        """Keep the outbound socket alive while the profile is enabled."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Hussh-Relay-Role": "device",
        }
        relay_environment = str(
            os.getenv("PUPPY_RELAY_ENV") or os.getenv("HUSHH_DEPLOY_ENV") or ""
        ).strip()
        if relay_environment:
            headers["X-Hussh-Deploy-Env"] = relay_environment
        reconnect_delay = 2.0
        while True:
            try:
                async with connect(
                    self.relay_url,
                    additional_headers=headers,
                    max_size=_MAX_FRAME_BYTES,
                    ping_interval=20,
                    ping_timeout=20,
                ) as websocket:
                    await websocket.send(
                        json.dumps(self.hello(), separators=(",", ":"))
                    )
                    ready = json.loads(await websocket.recv())
                    if ready.get("type") != "relay.ready":
                        raise RuntimeError("Puppy relay admission refused")
                    heartbeat_task = asyncio.create_task(self._heartbeat(websocket))
                    try:
                        async for raw in websocket:
                            if (
                                isinstance(raw, bytes)
                                or len(raw.encode("utf-8")) > _MAX_FRAME_BYTES
                            ):
                                raise RuntimeError("Puppy relay frame too large")
                            frame = json.loads(raw)
                            if frame.get("type") == "inference.request":
                                await self._infer(frame, websocket)
                    finally:
                        heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat_task
                    reconnect_delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.info("puppy_inference.reconnecting")
                await asyncio.sleep(
                    reconnect_delay + random.uniform(0.0, min(1.0, reconnect_delay / 4))
                )
                reconnect_delay = min(reconnect_delay * 2.0, 30.0)


async def run_puppy_inference_relay(**kwargs: Any) -> None:
    await PuppyInferenceRelay(**kwargs).serve()
