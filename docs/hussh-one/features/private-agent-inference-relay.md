# Private-agent inference relay

Puppy One can supply local-model inference to an owner's BYOC private pod while
the pod keeps orchestration, consent, memory, and tool execution. The relay is a
separate outbound WebSocket lane and does not reuse the Hermes conversation loop.

For the direct pod path, keep the existing trusted Mac identity in the intended
Hussh One profile and run `scripts/hussh-one-puppy-inference-relay.py --direct`
with that profile selected through the existing `HERMES_HOME` mechanism. The
device obtains a fresh owner and pod-bound inference binding, proves possession
of its existing signing key, and connects to the pod with sealed frames. A
missing, revoked, or wrong-environment device identity requires enrollment
repair; a healthy trusted device does not need to register again. The direct
path requires the owner's active BYOC pod and currently has no live two-device
acceptance result.

To keep the client ready before owner approval, add `--wait-for-activation` to
`--direct`. This waits on the existing hub device-control lane without contacting
or waking the pod. Enable Puppy for this device in the owner app, then start an
inference request; that request supplies the activation signal. Activation is
only a wake signal: the client still obtains a fresh verified binding and pod
admission. Revocation or refused admission stops the client. Start only one relay
process per profile; Ctrl-C stops an owned foreground process. Signing in to
Hermes alone does not start this separate relay process.

The older hub compatibility path still accepts these values in its existing
profile configuration:

```text
PUPPY_RELAY_URL=wss://<dev-hub>/api/one/puppy/relay
PUPPY_RELAY_TOKEN=<short-lived cap.puppy.inference grant>
PUPPY_DEVICE_ID=<trusted device id>
PUPPY_LOCAL_MODEL_URL=http://127.0.0.1:1234/v1
PUPPY_LOCAL_MODEL=<resident model label>
```

That token is obtained from the authenticated Hussh account flow. Never put it in
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

The direct client reconnects when the device sleeps or the network changes. A
request in flight is failed rather than replayed; cancellation stops local
generation, and the pod rejects late or mismatched sealed frames. The old hub
broker remains a compatibility path and is not evidence of a direct BYOC link.
