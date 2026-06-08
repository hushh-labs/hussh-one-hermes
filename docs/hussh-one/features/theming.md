# Feature — CLI/TUI & Dashboard Theming

## What it does
Applies the **hussh 🤫 One** visual identity across the interactive terminal (CLI/TUI) and
the web dashboard.

## How it works (modules)
- `hermes_cli/skins/hussh-one.yaml` — CLI/TUI skin (banner colors, spinner faces/verbs,
  tool prefix, response box, branding text).
- `hermes_cli/dashboard_themes/hussh-one.yaml` — dashboard theme.
- `hermes_cli/brand.py` — supplies the canonical slug/name the skins derive from.

## Config knobs
- `display.skin: hussh-one`
- `display.skin` (alias used by some paths) and `display.platforms.*`
- `dashboard.theme: hussh-one`
- `brand.display_skin` / `brand.dashboard_theme`

## Behavior
- TUI shows Hussh One branding, kawaii status indicators, themed tool feed.
- Dashboard embeds the real Hermes TUI (not a forked React chat) themed as Hussh One.

## Privacy / Security
N/A (presentation).

## Tests
- `tests/hermes_cli/test_hussh_one_branding.py`
- Dashboard chat health asserted by `scripts/hussh-one-guard.sh` (Contract D).

## Status
✅ Shipped.

## Future
- Light/dark dashboard variants.
- Per-profile accent colors.
