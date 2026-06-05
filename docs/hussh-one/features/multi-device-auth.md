# Feature — Multi-Device (LID) Authorization

## What it does
Lets the owner trigger the agent from **any** of their WhatsApp companion devices —
phone, WhatsApp Desktop, WhatsApp Web — without per-device reconfiguration.

## Why it matters
WhatsApp's multi-device protocol assigns a different **LID** (Linked Identity Device)
identifier per device. A message sent from Desktop carries a different sender ID than the
same owner on mobile. Without handling this, the gateway flags the owner as
"Unauthorized user" and silently drops the command.

## How it works (modules)
- `gateway/run.py` → `_is_user_authorized()` checks the WhatsApp allowlist /
  allow-all flag against the inbound sender LID.
- `.env` carries the authorized identities.

## Config knobs
- `WHATSAPP_ALLOWED_USERS=<number>,<lid>,<lid>,…` — explicit owner identities.
- `WHATSAPP_ALLOW_ALL_USERS=true` — authorize all owner devices at the gateway layer.
  **Safe** because the Node bridge still drops every non-owner message first
  (see [triggering-and-security.md](./triggering-and-security.md)).

## Behavior
- New device appears → its LID is auto-authorized (with allow-all) → commands work instantly.
- Symptom when missing: `WARNING gateway.run: Unauthorized user: <lid> on whatsapp` and no reply.

## Privacy / Security
- Allow-all at the gateway is layered behind the bridge's owner-only (`fromMe`) filter, so
  it does **not** open the agent to other people — only to the owner's own devices.

## Tests
- Covered indirectly via gating tests; authorization path in `tests/gateway/`.

## Status
✅ Shipped.

## Future
- Auto-discovery + display of the owner's known LIDs in `hussh-one-doctor.sh`.
