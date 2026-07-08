# hussh 🤫 One — Product & Architecture Documentation

> **hussh** = **Hu**man **S**ecure **S**ocket **H**ost.
> The 🤫 icon is a double-entendre: the raised index finger means *"shh!"* (confidentiality)
> **and** visually forms the number **"One"** — a single, unified, secure personal agent
> present across every surface.

This is the canonical, evolving knowledge base for everything we are building on top of
Hermes Agent to create **hussh 🤫 One**. It is designed to be:

- **Modular** — every capability is an isolated, independently-documented module.
- **Deterministic** — behavior is config-driven and test-backed, not emergent.
- **Upgradeable** — built as an overlay so upstream Hermes merges never wipe our work.
- **Advertisable** — structured so it can one day become public-facing product marketing.

---

## 📚 Documentation Map

| Section | What's inside |
|---------|---------------|
| [`overview/`](./overview/) | What Hussh One is, the brand story, the philosophy, the north star |
| [`architecture/`](./architecture/) | How the abstraction is layered over Hermes; the overlay contract |
| [`features/`](./features/) | One page per shipped capability (the product surface) |
| [`operations/`](./operations/) | Bootstrapping, supervising, upgrading, doctor/guard runbooks |
| [`contracts/`](./contracts/) | Machine-readable invariants every build must satisfy |
| [`roadmap/`](./roadmap/) | What's next; how we evolve without breaking determinism |
| [`CHANGELOG.md`](./CHANGELOG.md) | **Dated, categorized history** of every Hussh-One-only capability — the crystal-clear index of what we ship on top of upstream (WhatsApp/capsules, Vertex ADC, Copilot BYOK, Open WebUI, and more) |

> The dense machine-readable spec still lives at the repo root in
> [`HUSSH_ONE.md`](../../HUSSH_ONE.md). This `docs/hussh-one/` tree is the
> **human-readable, nested, advertisable** companion to it.

---

## 🧭 Quick Status (audited)

| Layer | Components | Status |
|-------|-----------|--------|
| Core brand | `brand.py`, skin, dashboard theme, header, mcp-scan | ✅ intact |
| WhatsApp layer | adapter, capsule, bridge.js, memory/send tools | ✅ intact |
| Reliability | session-model resume, Vertex-Claude pinning, dashboard OOM guard | ✅ shipped |
| Ops scripts | bootstrap, supervisor, doctor, guard, restart | ✅ intact |
| Docs | Feature catalog, contracts, **changelog** — all self-checking | ✅ intact |
| Tests | branding, header, scripts, capsule, gating, prefix, resume-model | ✅ passing |
| Stale legacy brand data | — | ✅ none found |

Re-run the audit anytime with:

```bash
bash scripts/hussh-one-guard.sh
python -m pytest tests/hermes_cli/test_hussh_one_*.py tests/gateway/test_whatsapp_*.py -q
python3 scripts/hussh-one-changelog-check.py   # is the changelog current?
```

---

## 🔑 First Principles

1. **Overlay, never fork.** Logic lives in `hermes_cli/`, dedicated modules, and
   `scripts/hussh-one-*`, so an upstream merge to core files (e.g. `run.py`) cannot
   silently erase the Hussh One personality. The guard suite proves it after every merge.
2. **Config is the contract.** Behavior is driven by `config.yaml` + `.env`, not hardcoded.
   Any machine that applies the documented config inherits the identical personality.
3. **Privacy by construction.** Capsules, owner-only triggering, and injection-proof
   gating are structural guarantees, not best-effort filters.
4. **Every feature has: a module, a config knob, a test, and a doc page.** No exceptions.
