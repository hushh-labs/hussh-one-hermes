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

Set `HERMES_HOME` explicitly when starting or restarting a non-default profile.
The direct launcher prints the selected environment before it waits for
activation. Check that line before an owner rehearsal: an unset `HERMES_HOME`
selects the default profile, which may point at a different environment.

To keep the client ready before owner approval, add `--wait-for-activation` to
`--direct`. This waits on the existing hub device-control lane without contacting
or waking the pod. Enable Puppy for this device in the owner app, then start an
inference request; that request supplies the activation signal. Activation is
only a wake signal: the client still obtains a fresh verified binding and pod
admission. Temporary readiness, expiry and owner-grant refusals discard the pod
session and return to this hub control lane. A later owner-authorized request can
activate the same trusted identity again, without polling the sleeping pod.
Device revocation and signature/owner mismatches stop the client. Start only one relay
per profile; Ctrl-C stops an owned foreground process. Signing in alone does not
start the relay.

For an already enrolled profile, the existing Hermes gateway can own this lane.
Opt in through that profile's `config.yaml`:

```yaml
hussh_one:
  puppy:
    auto_start: true
```

Start the normal gateway with the intended profile selected. Its existing
supervisor starts the relay in activation-wait mode and cancels it on shutdown;
no extra daemon, launch agent or cloud keepalive is required. This setting is
off by default and grants no model access. Owner approval, current device
identity and fresh pod admission remain required. An existing explicit relay
holds the same profile lock, so gateway startup does not create a second
connection. Stop that owned launcher before handing the lane to the gateway.
Revoked identity or invalid local-model configuration stops automatic retries;
repair the profile before restarting the gateway.

The computer and local model server must be awake and reachable. A heartbeat
shows that the device has checked in; it does not prove a connected inference
lane or wake a sleeping computer. The app may request an authorized connection
while visible and retain cached model choices without keeping the pod awake.

The direct launcher reads the selected model and base URL from that profile's
existing model settings. Its endpoint must be on loopback; a selected cloud
model is refused rather than used as an inference fallback. Keep the local model
server running. Restart the owned relay process after changing the model selection.

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

The endpoint comes from the owner's current registry assignment, not a fixed GCP
project or an old Azure placement. A changed pod key requires fresh signed
admission. Heartbeats and control reads go to the hub; they do not extend the
ten-minute pod relay idle grace. Being online does not imply an open or billable
pod socket.

Admission logs contain only a fixed request stage, outcome and elapsed milliseconds;
they never include tokens, endpoints, frames or raw provider errors. Separate hub
metadata and binding requests from the pod challenge when diagnosing a slow wake.
The verified pod challenge has a finite 90-second read budget for minimum-zero
startup; other request timeouts and all signature, expiry and owner checks are
unchanged. This is a recovery allowance, not a cold-response performance claim.
