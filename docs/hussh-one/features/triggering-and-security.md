# Feature — Triggering & Injection-Proof Security

## What it does
Controls precisely **when** the agent responds and guarantees that **only the owner** can
ever invoke it outside of explicitly sandboxed capsule groups.

## Why it matters
The agent runs on the owner's own WhatsApp. Without strict gating, any group message could
trigger it (wasting compute, leaking replies) or a stranger could prompt-inject it. Hussh
One makes unauthorized invocation **structurally impossible**, not best-effort.

## How it works (modules)
- `scripts/whatsapp-bridge/bridge.js` — first line of defense at the network layer:
  - In `self-chat` mode, **only the owner's own messages (`fromMe`)** are forwarded to the
    Python gateway. Everyone else's messages are dropped before they reach the agent.
  - Owner messages in non-self chats require an explicit trigger (`@One` / `@husshOne` /
    `@hussh-one` or a `/` command).
- `gateway/platforms/whatsapp.py` — second layer: `_should_process_message()`,
  `require_mention`, `require_mention_on_replies`, mention-pattern regexes.

## Config knobs
| Knob | Effect |
|------|--------|
| `whatsapp.require_mention: true` | Bot stays silent on ambient group chatter |
| `whatsapp.require_mention_on_replies: true` | Even quote-replies need a fresh tag |
| `whatsapp.mention_patterns: [...]` | Anchored, case-insensitive wake-word regexes |
| `WHATSAPP_ALLOWED_USERS` | Authorized owner identities (numbers + LIDs) |
| `WHATSAPP_ALLOW_ALL_USERS=true` | Authorize all owner companion devices (still bridge-gated) |

## Triggering matrix
| Context | Who can trigger | How |
|---------|-----------------|-----|
| Owner self-chat | Owner | any message |
| Any group (non-capsule) | Owner only | explicit `@One` tag / `/` |
| Any DM with others | Owner only | explicit `@One` tag / `/` |
| Capsule group | Owner **and** other members | explicit `@One` tag (rate-limited for non-owners) |

## Privacy / Security
- **Injection-proof:** non-owner messages in non-capsule contexts never reach the agent —
  prompt injection is mathematically impossible there.
- **Deterministic wake words:** anchored regexes avoid false positives on casual brand mentions.

## Tests
- `tests/gateway/test_whatsapp_group_gating.py` (incl. `require_mention_on_replies`).

## Status
✅ Shipped.

## Future
- Per-group mention-pattern overrides.
- Optional time-of-day quiet hours.
