# Feature — Natural-Language Model Switching

## What it does
Lets the owner switch models with plain text ("switch to opus 4.8", "back to gemini 3.6
flash") from WhatsApp or the TUI, as a session-only override — without slash-command syntax.

## How it works
- Deterministic intent detection (not LLM-guessed) in the model-switch path.
- Session-scoped `/model` override; global default unchanged unless `--global` is used.
- TUI/dashboard plain-text requests and `/model` converge on the same
  `_apply_model_switch()` transaction. A successful explicit switch updates the
  live agent, records `selection_mode=select`, persists the runtime, and only
  then emits `session.info`. The footer therefore cannot report `[A]` after an
  explicit switch has committed.
- `sessions.model_config.selection_mode` stores selection provenance without
  storing credentials. Resume rehydrates it into the per-session override;
  `/new` clears it with the rest of the session-scoped runtime.

## Config knobs
- `model.default` / `model.provider` — the global default (Gemini 3.6 Flash for Hussh One).
- `/model <id>` — session override (`[S]` in the header).
- `/model auto|reset|clear|default` — restore config routing (`[A]` in the header).
- `/model <id> --global` — change the global default.

## Native provider quirk
- Native `gemini` provider: use prefix-free `gemini-3.6-flash` (the vendor-prefixed
  `gemini/gemini-3.6-flash` is not a native API model identifier).

## Prompt-injection safeguard
Detection rejects slash commands, quoted text, URLs, code blocks, lists, long pastes, help
questions, negations, and injection-shaped phrases ("ignore previous", "system prompt", …).
Matching is intentionally not general fuzzy search. The observed adjacent-letter
typo `gmeini` is normalized narrowly so `switch to gmeini 3.1 pro` remains a
control-plane command without making arbitrary misspelled prose capable of
changing models.

## `[A]` and `[S]` contract

- `[A]` means automatic/config-derived routing. Router escalation to another
  model remains automatic and does not turn the session into `[S]`.
- `[S]` means the owner explicitly chose a model for this session through the
  picker, `/model`, or an accepted natural-language switch.
- The selection marker is committed before the UI event and database write.
  Model name and provenance therefore change atomically from the user's point
  of view.
- A one-turn (`--once`) model does not permanently change provenance.
- No switch mutates process-global model/provider environment variables; one
  session cannot silently switch another session in the shared dashboard
  backend.

## Interrupt-and-redirect behavior

When a switch is submitted while a model turn is still running, the TUI first
interrupts that turn and queues the switch. Seeing `interrupted` describes the
superseded turn; it is not the model-switch result. Once the old stream has
unwound, the queued request is intercepted before any new LLM call and the TUI
emits the model/provider confirmation. This preserves one writer for the live
agent while still supporting redirect-style input.

## Vertex safeguard
Vertex Claude switches run a live access check before mutating the session; stale Vertex
runtimes normalize back to the `google-vertex-claude` adapter.

## Vertex Claude catalog (live-probed, Jul 2026)
Four Claude models are enabled in `hushh-pda-uat` via ADC — all accept **1M-token
prompts natively** (no beta header) and cap output at **128k**:

| Model | Region | Aliases understood |
|-------|--------|--------------------|
| claude-opus-4-8 | global | "opus", "opus 4.8" |
| claude-sonnet-4-6 | global | "sonnet 4.6" |
| claude-sonnet-5 | global | "sonnet 5" |
| claude-fable-5 | **global only** | "fable", "fable 5" |

## Gemini catalog (native `gemini` provider)
| Model | Aliases understood | Context | Thinking levels |
|-------|--------------------|---------|------------------|
| gemini-3.6-flash (default) | "gemini 3.6", "gemini 3.6 flash", "flash" | 1,048,576 in / 65,536 out | low / medium / high |
| gemini-3.5-flash | "gemini 3.5", "gemini 3.5 flash" | 1,048,576 in / 65,536 out | low / medium / high |
| gemini-3.1-pro-preview | "gemini 3.1", "gemini 3.1 pro", "gemini 3.1 pro preview" | 2,097,152 in / 65,536 out | low / medium / high |

`gemini-3.1-pro` and `gemini-3.1-pro-preview` both resolve to the same model —
`gemini-3.1-pro` is not a separately-servable Vertex slug yet, so the proxy config
and CLI catalog point it at `gemini-3.1-pro-preview` until GA drops a stable id.

`_canonical_model()` in `hermes_cli/natural_model_switch.py` matches "gemini 3.1"
**without** requiring the word "pro" — bare version-number switches ("switch to
gemini 3.1") resolve the same as "switch to gemini 3.1 pro". This pattern must be
extended for every future Gemini/Claude minor version so version-only phrasing
never silently falls through to `None` (no-op).

The native Gemini default is separate from VS Code Copilot BYOK's explicitly
live-probed Vertex pool. Do not add Gemini 3.6 to that pool until its target
Vertex region has been verified; see `scripts/copilot-byok/README.md`.

## TUI model popover sync
The model picker popover (opened by clicking/selecting the status-bar model) opens
**reflecting the live session model**, not a stale snapshot:
- The picker accepts an authoritative `liveModel` from `session.info` (the same
  source the status bar renders), which wins over its own `model.options` RPC
  snapshot — so the popover can never disagree with the model shown above the
  tool calls (e.g. Claude Opus on Vertex).
- `resolvePickerSelection()` lands the cursor on the **current provider AND the
  active model within it**, instead of always highlighting provider 0 / model 0.
- Pure, tested helper: `ui-tui/src/components/modelPicker.tsx` →
  `tests`: `ui-tui/src/__tests__/modelPickerSelection.test.ts`.

## Tests
- `tests/hermes_cli/test_natural_model_switch.py`
- `tests/gateway/test_natural_model_switch.py`
- `tests/test_tui_gateway_server.py` — interception, persistence, resume, and
  session isolation.
- `tests/tui_gateway/test_hussh_one_runtime_identity.py` — canonical `[A]/[S]`
  rendering.

Focused verification:

```bash
.venv/bin/python -m pytest \
  tests/hermes_cli/test_natural_model_switch.py \
  tests/tui_gateway/test_hussh_one_runtime_identity.py \
  tests/test_tui_gateway_server.py \
  -q -k 'natural_model_switch or selection_provenance or persist_live_session_runtime'
```

## Status
✅ Shipped.

## Future
- Per-capsule model pinning (e.g. cheap model for social capsules).
