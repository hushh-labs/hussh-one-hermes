# Feature Catalog

Every shipped Hussh One capability gets its own page here. Each page follows the same
template so the catalog stays modular, deterministic, and advertisable.

> **Feature page template:** What it does · Why it matters · How it works (modules) ·
> Config knobs · Triggering/Behavior · Privacy/Security · Tests · Status · Future.

## Shipped features

| Feature | Surface | Status | Page |
|---------|---------|--------|------|
| Brand & stacked WhatsApp header | WhatsApp | ✅ Shipped | [whatsapp-branding-header.md](./whatsapp-branding-header.md) |
| Owner-only triggering & injection-proofing | WhatsApp | ✅ Shipped | [triggering-and-security.md](./triggering-and-security.md) |
| Strict tagging (`@One`) in groups & DMs | WhatsApp | ✅ Shipped | [triggering-and-security.md](./triggering-and-security.md) |
| Multi-device (LID) authorization | WhatsApp | ✅ Shipped | [multi-device-auth.md](./multi-device-auth.md) |
| Social-group capsules (sandboxed) | WhatsApp | ✅ Shipped | [whatsapp-capsules.md](./whatsapp-capsules.md) |
| Capsule isolated memory vault | WhatsApp | ✅ Shipped | [whatsapp-capsules.md](./whatsapp-capsules.md) |
| Non-owner capsule triggering + anti-DOS rate limit | WhatsApp | ✅ Shipped | [whatsapp-capsules.md](./whatsapp-capsules.md) |
| Clean output (no reasoning/logs/jargon) | WhatsApp | ✅ Shipped | [clean-output.md](./clean-output.md) |
| Autopilot approvals (no friction) | All | ✅ Shipped | [clean-output.md](./clean-output.md) |
| Local WhatsApp history & media retrieval | WhatsApp | ✅ Shipped | [whatsapp-local-data.md](./whatsapp-local-data.md) |
| Message edit / recovery | WhatsApp | ✅ Shipped | [whatsapp-local-data.md](./whatsapp-local-data.md) |
| CLI/TUI + Dashboard theming | CLI/Web | ✅ Shipped | [theming.md](./theming.md) |
| Natural-language model switching | WhatsApp/TUI | ✅ Shipped | [model-switching.md](./model-switching.md) |

## Planned features
See the [roadmap](../roadmap/README.md).

---

### How to add a feature page
1. Copy the template from any existing page.
2. Fill every section — a feature without a config knob + test isn't done.
3. Add a row to the table above.
4. Add or confirm its invariant in [contracts](../contracts/README.md).
