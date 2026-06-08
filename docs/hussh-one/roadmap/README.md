# Roadmap

How Hussh One evolves — while staying modular, deterministic, and upgradeable.

## Guiding rule
Every new capability ships with **a module + a config knob + a test + a feature doc page +
a contract entry**. If any of the five is missing, it's not done.

## Near-term candidates
- [ ] `hussh-one capsule add <jid> <name>` one-command capsule creator.
- [ ] Per-capsule model pinning (cheap model for social capsules).
- [ ] Per-capsule rate-limit overrides.
- [ ] Doctor: auto-discover & display owner LIDs; flag unauthorized-but-recurring senders.
- [ ] Helper to copy retrieved WhatsApp attachments into `~/Downloads` on request.
- [ ] Quiet hours / do-not-disturb windows per surface.

## Mid-term
- [ ] Cross-platform local WhatsApp store support (beyond macOS Catalyst).
- [ ] Capsule usage analytics (per-member, inside the capsule vault).
- [ ] Light/dark dashboard theme variants.
- [ ] Public-facing landing page generated from this docs tree.

## Long-term north star
- [ ] One-click adopt: anyone can stand up their own secure personal host.
- [ ] Consent-protocol integration so the agent can broker the owner's data disclosures.
- [ ] Multi-surface presence beyond WhatsApp (signal/telegram/email) under one identity.

## How to propose a change
1. Add a checkbox here.
2. When you start, create the feature page in [`../features/`](../features/) (status: 🚧).
3. Land module + config + test together; flip status to ✅.
4. Add/extend the [contract](../contracts/README.md).
5. Run the guard; commit; push.

## Changelog discipline
Keep commit messages feature-scoped (`feat(whatsapp): …`, `docs(hussh-one): …`) so this
tree and the git history tell the same story when we advertise it.
