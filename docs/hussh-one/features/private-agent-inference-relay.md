# Private-agent inference relay

Puppy One can supply local-model inference to an owner's Hussh private pod while
the pod keeps orchestration, consent, memory, and tool execution. The relay is a
separate outbound WebSocket lane and does not reuse the Hermes conversation loop.

Set these values in the intended profile only:

```text
PUPPY_RELAY_URL=wss://<dev-hub>/api/one/puppy/relay
PUPPY_RELAY_TOKEN=<short-lived cap.puppy.inference grant>
PUPPY_DEVICE_ID=<trusted device id>
PUPPY_LOCAL_MODEL_URL=http://127.0.0.1:1234/v1
PUPPY_LOCAL_MODEL=<resident model label>
```

The token is obtained from the authenticated Hussh account flow. Never put it in
URLs, logs, telemetry, or a checked-in profile. `PUPPY_LOCAL_MODEL_URL` is local
to the device; no local API key or endpoint is sent to the pod. The adapter only
handles text, structured output, and tool-call/result frames. It does not execute
tools, access the filesystem, or use a cloud fallback.

The device says what it is and what it can do. `relay.hello` carries the
resident model id and a capability profile in the Puppy One harness vocabulary
(`tool_calling`, `json_schema`, `streaming`, plus `probe_mode`), and every
`inference.result` names the model that answered. The pod maps the request's
schema, tool choice, allowed function names, `top_p`, stop sequences and seed
onto the wire; the relay maps them onto the local chat-completions request. A
request that needs a capability the profile lacks is refused with
`inference.error code=UNSUPPORTED_CAPABILITY` before the local model is called,
and the pod reports it as unsupported rather than falling back. The `thinking`
field has no portable equivalent on this endpoint family and is not mapped.

The relay reconnects when the device sleeps or the network changes. A request in
flight is failed rather than replayed, and the pod rejects late or mismatched
frames. The dev broker is process-local; do not promote it to a multi-instance
production service until an external rendezvous/lease store and rollout evidence
exist.
