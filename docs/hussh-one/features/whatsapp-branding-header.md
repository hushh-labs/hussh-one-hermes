# Feature — WhatsApp Branding & Stacked Header

## What it does
Stamps every outgoing WhatsApp message with a consistent, robust header so recipients can
always distinguish the **hussh 🤫 One** agent from the human owner typing manually.

```
hussh 🤫 One
<Display Model> [S|A]
════════════════════
<message body>
```

## Why it matters
Hussh One runs on the owner's own WhatsApp account (self-chat mode), so agent replies and
human messages share one identity. The header is the visual signature that keeps them
distinguishable — critical for trust in shared groups and DMs.

## How it works (modules)
- `hermes_cli/hussh_one_header.py` — single source of truth. Exposes
  `build_whatsapp_header()`, `apply_whatsapp_header()` (strip-then-prepend),
  `display_model_name()`, `mode_token()`, `strip_contaminated_header()`.
- `gateway/run.py` — imports and calls `apply_whatsapp_header(...)` on outgoing WhatsApp text.
- `gateway/platforms/whatsapp.py` — forces the Node bridge prefix to empty so exactly one
  composer stamps the header (no double-stamping).

## Config knobs
- `brand.whatsapp_reply_prefix` / env `WHATSAPP_REPLY_PREFIX` — override or disable (empty = off).
- `whatsapp.reply_prefix: null` — use the dynamic composed header (recommended).
- Override precedence: env `WHATSAPP_REPLY_PREFIX` > `config.yaml whatsapp.reply_prefix` > composed standard.

## Behavior
- `[S]` Select mode — a model was manually pinned this session.
- `[A]` Auto mode — running the configured default.
- `strip_contaminated_header()` recursively removes any self-echoed header lines (and the
  `高度` CJK tokenizer-collision artifact) before prepending the clean one.

## Privacy / Security
N/A (presentation layer) — but it is the trust signal that makes shared-group use safe.

## Tests
- `tests/hermes_cli/test_hussh_one_header.py`
- `tests/gateway/test_whatsapp_reply_prefix.py`
- `tests/hermes_cli/test_hussh_one_branding.py`

## Status
✅ Shipped.

## Future
- Per-surface header variants (e.g. compact mobile vs. desktop).
- Optional localized brand line.

## Pitfall (known, solved)
Do **not** use the continuous `═` (`\u2550`) inside model-visible history — its UTF-8 bytes
decode as `高度` and contaminate Gemini output. The header is post-processed/stripped to
avoid this; keep the recursive stripper in place.
