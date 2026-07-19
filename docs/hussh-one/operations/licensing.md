# Hussh One distribution licensing

Hussh One is a mixed-provenance distribution, represented publicly by the SPDX
expression `Apache-2.0 AND MIT`. This is deliberate: it licenses Hushh Labs'
2026 additions under Apache-2.0 without pretending that Hermes Agent upstream
code has been relicensed.

## Source boundary

| Path provenance | Terms |
| --- | --- |
| Retained unchanged from Hermes Agent upstream | MIT |
| Retained and modified from Hermes Agent upstream | `MIT AND Apache-2.0` |
| Added by Hussh One / Hushh Labs | Apache-2.0 |
| Nested plugins and skills with their own license | Nested terms control |

`LICENSES/attribution.toml` is the canonical, machine-readable map. It records
the upstream comparison commit and each nested-license exception. Added Hussh
source files carry `SPDX-FileCopyrightText: 2026 Hushh Labs` and
`SPDX-License-Identifier: Apache-2.0`; do not mass-rewrite headers in retained
upstream files.

## Redistribution and release procedure

1. Preserve [`LICENSE`](../../../LICENSE), [`NOTICE`](../../../NOTICE), and
   [`LICENSES/UPSTREAM-MIT.txt`](../../../LICENSES/UPSTREAM-MIT.txt).
2. Preserve the nested artifacts indexed in
   [`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md).
3. Run `python3 scripts/hussh-one-license-audit.py` and
   `bash scripts/hussh-one-guard.sh`.
4. Build the Python distributions and confirm the legal artifacts are present
   before publishing.

The audit is a repository-compliance control, not legal advice. Obtain counsel
review before a public release requiring formal legal assurance.
