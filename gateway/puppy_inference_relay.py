"""Puppy One's inference-only outbound relay client.

This process is a local-model adapter, not the Hermes agent loop. It dials the
Hussh hub relay while the trusted profile is enabled, forwards one bounded
request at a time to the configured local OpenAI-compatible model endpoint,
and returns text/tool-call frames. No shell, filesystem, MCP, or Hermes tool
executor is reachable through this client.
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
                        "arguments": json.dumps(item.get("toolArguments") or {}, separators=(",", ":")),
                    },
                }
            ]
        elif role == "tool" and (item.get("toolName") or item.get("toolCallId")):
            message["tool_call_id"] = str(item.get("toolCallId") or "")
            message["content"] = json.dumps(item.get("toolResult"), separators=(",", ":"))
        messages.append(message)
    return messages


def _tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "parameters": item.get("parameters") or {"type": "object", "properties": {}},
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
    ) -> None:
        self.relay_url = (relay_url or os.getenv("PUPPY_RELAY_URL") or "").strip()
        self.token = (token or os.getenv("PUPPY_RELAY_TOKEN") or "").strip()
        self.device_id = (device_id or os.getenv("PUPPY_DEVICE_ID") or "").strip()
        self.model_url = (model_url or os.getenv("PUPPY_LOCAL_MODEL_URL") or "http://127.0.0.1:1234/v1").rstrip("/")
        self.model = (model or os.getenv("PUPPY_LOCAL_MODEL") or "local").strip()
        self.model_api_key = model_api_key or os.getenv("PUPPY_LOCAL_MODEL_API_KEY") or ""
        self.model_timeout = float(os.getenv("PUPPY_LOCAL_MODEL_TIMEOUT_SECONDS") or _DEFAULT_MODEL_TIMEOUT)
        if not self.relay_url.startswith(("wss://", "ws://")):
            raise ValueError("PUPPY_RELAY_URL must be a ws:// or wss:// URL")
        if not self.token or not self.device_id:
            raise ValueError("PUPPY_RELAY_TOKEN and PUPPY_DEVICE_ID are required")
        model_origin = urlparse(self.model_url)
        if model_origin.scheme not in {"http", "https"} or model_origin.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("PUPPY_LOCAL_MODEL_URL must point to a loopback model endpoint")

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
                    {"type": "relay.heartbeat", "status": "ready", "deviceId": self.device_id},
                    separators=(",", ":"),
                )
            )

    async def _infer(self, request: dict[str, Any], websocket: Any) -> None:
        request_id = str(request.get("requestId") or "")
        if not request_id:
            return
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(request),
            "stream": True,
        }
        tools = _tools(request)
        if tools:
            payload["tools"] = tools
        if request.get("temperature") is not None:
            payload["temperature"] = request["temperature"]
        if request.get("maxOutputTokens") is not None:
            payload["max_tokens"] = request["maxOutputTokens"]
        headers = {"Authorization": f"Bearer {self.model_api_key}"} if self.model_api_key else {}
        calls: dict[int, dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(timeout=self.model_timeout) as client:
                async with client.stream("POST", f"{self.model_url}/chat/completions", json=payload, headers=headers) as response:
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
                        choices = chunk.get("choices") or []
                        delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else {}
                        text = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(text, str) and text:
                            await websocket.send(json.dumps({"type": "inference.delta", "requestId": request_id, "text": text}))
                        for call in (delta.get("tool_calls") or []) if isinstance(delta, dict) else []:
                            if not isinstance(call, dict):
                                continue
                            index = int(call.get("index") or 0)
                            target = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                            target["id"] = target["id"] or str(call.get("id") or "")
                            function = call.get("function") or {}
                            target["name"] = target["name"] or str(function.get("name") or "")
                            target["args"] += str(function.get("arguments") or "")
            function_calls: list[dict[str, Any]] = []
            for call in calls.values():
                try:
                    args = json.loads(call["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if call["name"]:
                    function_calls.append({"id": call["id"], "name": call["name"], "args": args})
            await websocket.send(
                json.dumps(
                    {"type": "inference.result", "requestId": request_id, "functionCalls": function_calls}
                )
            )
            await websocket.send(json.dumps({"type": "inference.done", "requestId": request_id}))
        except Exception:  # noqa: BLE001 - do not disclose local endpoint or credentials
            await websocket.send(
                json.dumps(
                    {"type": "inference.error", "requestId": request_id, "code": "LOCAL_MODEL_UNAVAILABLE"}
                )
            )

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
                        json.dumps({"type": "relay.hello", "role": "device", "deviceId": self.device_id})
                    )
                    ready = json.loads(await websocket.recv())
                    if ready.get("type") != "relay.ready":
                        raise RuntimeError("Puppy relay admission refused")
                    heartbeat_task = asyncio.create_task(self._heartbeat(websocket))
                    try:
                        async for raw in websocket:
                            if isinstance(raw, bytes) or len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
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
                await asyncio.sleep(reconnect_delay + random.uniform(0.0, min(1.0, reconnect_delay / 4)))
                reconnect_delay = min(reconnect_delay * 2.0, 30.0)


async def run_puppy_inference_relay(**kwargs: Any) -> None:
    await PuppyInferenceRelay(**kwargs).serve()
