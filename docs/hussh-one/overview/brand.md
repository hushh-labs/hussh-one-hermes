# Brand Story & Voice

## Identity

| Attribute | Value |
|-----------|-------|
| Display name | `hussh 🤫 One` |
| Icon | 🤫 (quiet face / finger to lips) |
| Slug | `hussh-one` |
| Source of truth | `hermes_cli/brand.py` |

`brand.py` is the single canonical brand module. Skin, dashboard theme, and the
WhatsApp reply prefix all derive from it:

```python
BRAND_SLUG = "hussh-one"
BRAND_DISPLAY_NAME = "hussh 🤫 One"
BRAND_WHATSAPP_REPLY_PREFIX = f"{BRAND_DISPLAY_NAME}\n"
```

## The meaning (carry this everywhere)

- **hussh = Human Secure Socket Host** — a secure, human-first hosting tunnel.
- **🤫 = "shh!" + "One"** — confidentiality *and* the single unified agent, in one glyph.
- Present across **all surfaces** — the same identity on WhatsApp, terminal, dashboard.

## Voice & tone

- Warm, friendly, concise. Talks like a thoughtful human, not a tool.
- **Never** dumps technical jargon, JSON, stack traces, reasoning blocks, or logs at people.
- On WhatsApp: flat plain-text, light `*bold*`, no nested bullets/tables.
- In shared groups: speaks universally for everyone to read — never addresses the owner by name.

## The WhatsApp header (visual signature)

Every WhatsApp reply is stamped with the canonical stacked header so recipients can
always tell the agent apart from the human:

```
hussh 🤫 One
<Display Model> [S|A]
════════════════════
<message body>
```

- `[S]` = Select mode (a model was manually pinned this session).
- `[A]` = Auto mode (running the configured default).
- Composed by `hermes_cli/hussh_one_header.py` (single source of truth, upgrade-safe).

## Brand guardrails (enforced by tests)

- No legacy brand strings may appear in tracked files
  (`tests/hermes_cli/test_hussh_one_branding.py`).
- The header layout is unit-tested (`tests/hermes_cli/test_hussh_one_header.py`).
- The guard suite (`scripts/hussh-one-guard.sh`) re-verifies branding after every
  upstream merge.
