# Third-party and nested component notices

This index identifies repository components that retain their own license or
notice. Their nested terms override the Hussh One distribution default for the
paths they cover; this file does not alter any of those terms.

| Component | License or terms | Required notice files |
| --- | --- | --- |
| Hermes Agent upstream | MIT | [`LICENSES/UPSTREAM-MIT.txt`](LICENSES/UPSTREAM-MIT.txt) |
| Hermes Achievements plugin | MIT | [`plugins/hermes-achievements/LICENSE`](plugins/hermes-achievements/LICENSE) |
| Security Guidance plugin | Apache-2.0 | [`plugins/security-guidance/LICENSE`](plugins/security-guidance/LICENSE), [`plugins/security-guidance/NOTICE`](plugins/security-guidance/NOTICE) |
| Creative Humanizer skill | MIT | [`skills/creative/humanizer/LICENSE`](skills/creative/humanizer/LICENSE) |
| PowerPoint skill materials | Anthropic commercial terms | [`skills/productivity/powerpoint/LICENSE.txt`](skills/productivity/powerpoint/LICENSE.txt) |

Dependency-specific license information remains in the relevant package manager
metadata and installed dependency distributions. The repository license audit
checks the source components above; it does not replace dependency-license
scanning for a release.
