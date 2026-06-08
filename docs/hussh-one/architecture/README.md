# Architecture — The Overlay Model

Hussh One is **not a fork** of Hermes Agent. It is a deterministic **overlay** built so
that upstream Hermes can keep evolving underneath us while our personality, branding,
and privacy guarantees stay intact.

## The layering

```
┌─────────────────────────────────────────────────────────────┐
│  HUSSH ONE OVERLAY  (ours — survives upstream merges)         │
│                                                               │
│  hermes_cli/brand.py            → identity source of truth    │
│  hermes_cli/hussh_one_header.py → WhatsApp stacked header     │
│  hermes_cli/skins/…             → CLI/TUI theming             │
│  hermes_cli/dashboard_themes/…  → dashboard theming           │
│  gateway/whatsapp_capsule.py    → social-group sandboxes      │
│  scripts/hussh-one-*.sh         → bootstrap/supervise/guard   │
│  config.yaml + .env             → the behavioral contract     │
└─────────────────────────────────────────────────────────────┘
                          ▲  calls into / configures
                          │
┌─────────────────────────────────────────────────────────────┐
│  HERMES AGENT CORE  (upstream — we merge, never overwrite)    │
│  run_agent.py · gateway/run.py · model_tools.py · tools/…     │
└─────────────────────────────────────────────────────────────┘
```

## Why overlay (the upgrade-safety contract)

When core files like `gateway/run.py` need a Hussh One behavior, they **import and call**
a dedicated overlay module rather than inlining the logic. Example: the WhatsApp header is
composed by `hussh_one_header.apply_whatsapp_header(...)`, so a merge that rewrites
`run.py` cannot silently drop our branding — the logic isn't there to lose.

After every upstream merge, run `scripts/hussh-one-guard.sh`. It re-asserts branding,
header composition, capsule isolation, and dashboard-chat health. Green = the overlay
survived.

## The three runtime planes

1. **CLI / TUI plane** — interactive terminal experience (skins, header rules differ here:
   reasoning *can* show in the TUI, never on WhatsApp).
2. **Gateway plane** — the always-on messaging brain (`gateway/run.py`) that drives
   WhatsApp via the Node Baileys bridge on port 3000.
3. **Dashboard plane** — `hermes dashboard --tui` embedding the real TUI over a PTY bridge.

## Determinism model

- **Config is the contract.** `config.yaml` (settings) + `.env` (keys/policy) fully
  determine behavior. Any machine applying the documented config gets identical behavior.
- **Tests are the proof.** Every invariant has a test; CI/guard re-checks them.
- **No emergent behavior.** Triggering, gating, rate limits, capsules — all explicit.

## Profiles & isolation

- `HERMES_PROFILE` keeps separate users' universes (memory, sessions, SQLite) isolated
  under `~/.hermes/profiles/<name>/`.
- WhatsApp runs as a **single** Baileys session — capsules are therefore built
  **in-process** (per-session overrides), not as second profiles. See
  [features/whatsapp-capsules.md](../features/whatsapp-capsules.md).

---

### Related
- [Root machine-readable spec](../../../HUSSH_ONE.md)
- [Upgrade & maintenance runbook](../operations/upgrading.md)
- [Contracts](../contracts/README.md)
