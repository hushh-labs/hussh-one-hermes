# Machine-Readable Contracts

Every Hussh One build must satisfy these invariants. They are the deterministic spine of
the product — each maps to a test or guard check. The dense source remains in
[`HUSSH_ONE.md`](../../../HUSSH_ONE.md) §5–6; this is the navigable index.

| Contract | Invariant | Verification |
|----------|-----------|--------------|
| **A — Group routing safeguard** | Bridge never forwards non-allowed JIDs in self-chat mode | `bridge.js` review + gating tests |
| **B — Zero-width unicode leakage** | Assembled cron prompts contain zero `U+200B/C/D/FEFF` | `auto_dream.py` output scan |
| **C — Upstream update guard** | Merges never erase brand/skin/theme/prefix/provider overlay | `scripts/hussh-one-guard.sh` |
| **D — Dashboard chat surface** | Dashboard uses embedded real TUI, not a forked React chat | guard + doctor (`--require-services`) |
| **E — NL model switching** | Deterministic, injection-safe, Vertex-safe model switches | `test_natural_model_switch.py` (cli+gateway) |
| **F — Capsule sandbox** | Isolated memory, read-only toolset, no lateral send, tag-gated non-owner triggering, anti-DOS rate limit | `tests/gateway/test_whatsapp_capsule.py` + gating tests |
| **G — Branding & header** | Canonical stacked header; no legacy brand strings in tracked files | `test_hussh_one_branding.py`, `test_hussh_one_header.py` |

## Determinism rules (apply to all features)
1. **A feature isn't done without:** a module, a config knob, a test, and a doc page.
2. **No change-detector tests.** Assert relationships/invariants, not data snapshots
   (model catalogs, version literals, enumeration counts).
3. **Config is the contract.** Behavior must be reproducible from documented config + .env.
4. **Overlay over fork.** New behavior goes in overlay modules with small core call sites.

## Run all contract checks
```bash
scripts/hussh-one-guard.sh
python -m pytest tests/hermes_cli/test_hussh_one_*.py tests/gateway/test_whatsapp_*.py -q
```
