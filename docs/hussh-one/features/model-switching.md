# Feature — Natural-Language Model Switching

## What it does
Lets the owner switch models with plain text ("switch to opus 4.8", "back to gemini 3.5
flash") from WhatsApp or the TUI, as a session-only override — without slash-command syntax.

## How it works
- Deterministic intent detection (not LLM-guessed) in the model-switch path.
- Session-scoped `/model` override; global default unchanged unless `--global` is used.

## Config knobs
- `model.default` / `model.provider` — the global default (Gemini 3.5 Flash for Hussh One).
- `/model <id>` — session override (`[S]` in the header).
- `/model auto|reset|clear|default` — restore config routing (`[A]` in the header).
- `/model <id> --global` — change the global default.

## Native provider quirk
- Native `gemini` provider: use prefix-free `gemini-3.5-flash` (the vendor-prefixed
  `gemini/gemini-3.5-flash` returns HTTP 404 on the native API).

## Prompt-injection safeguard
Detection rejects slash commands, quoted text, URLs, code blocks, lists, long pastes, help
questions, negations, and injection-shaped phrases ("ignore previous", "system prompt", …).

## Vertex safeguard
Vertex Claude switches run a live access check before mutating the session; stale Vertex
runtimes normalize back to the `google-vertex-claude` adapter.

## Tests
- `tests/hermes_cli/test_natural_model_switch.py`
- `tests/gateway/test_natural_model_switch.py`

## Status
✅ Shipped.

## Future
- Per-capsule model pinning (e.g. cheap model for social capsules).
