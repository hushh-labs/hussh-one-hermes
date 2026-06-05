# Feature — Clean Output & Autopilot Approvals

## What it does
Guarantees WhatsApp replies are warm, crisp, and free of technical noise — no reasoning
blocks, no tool-progress spam, no approval prompts cluttering the chat.

## Why it matters
People on the other end (friends, family, colleagues) should see a polished human-like
message, never the agent's internal machinery. And the owner shouldn't have to babysit
approval prompts inside a chat thread.

## How it works (modules / config)
- `gateway/run.py` — gates reasoning/interim output per-platform before sending.
- `tools/approval.py` — honors `approvals.mode: off` to auto-approve.

## Config knobs
| Knob | Value | Effect |
|------|-------|--------|
| `display.show_reasoning` | `false` | No 💭 reasoning block prepended to replies |
| `display.tool_progress` | `false` | No mid-turn tool-progress messages |
| `display.interim_assistant_messages` | `false` | No streaming draft fragments |
| `display.runtime_footer.enabled` | `false` | No model-metadata footer |
| `approvals.mode` | `off` | Auto-approve dangerous commands (no in-chat prompts) |

> Note: reasoning is intentionally allowed in the **CLI/TUI** for the owner's own
> debugging — it is muted only on WhatsApp. This is a per-surface distinction.

## Behavior
- Tools (file reads, web, scripts) run silently in the background; only the final, clean
  answer is delivered.
- Dangerous-command approval is bypassed (owner trusts their own agent), so no
  `/approve` buttons appear in chat.

## Privacy / Security
- `approvals.mode: off` is scoped to the owner's trusted environment. Capsule sessions are
  separately constrained to read-only toolsets, so autopilot there still can't mutate anything.

## Tests
- Output gating covered in `tests/gateway/`; approval behavior in `tests/tools/`.

## Status
✅ Shipped.

## Future
- Per-capsule stricter approval policy if ever needed.
- Optional "show reasoning to owner only" relay.
