# Xtreme Burst Compute — architecture and migration record

Burst orchestration belongs in **Hermes**. This page records where it came from, what has
actually moved, what has not, and the one deliberate incompatibility introduced on the way.

Hermes is the local compute and burst-orchestration layer. A workload runs on the device in
hand when it fits, and bursts to the *person's own* cloud when it does not. The decision is
made from resource **numbers** — never from the workload's contents — so it can be made
locally, and nothing about a job has to leave the machine to place it. That property is the
whole privacy argument, and every later phase has to preserve it.

## Where the work started, and why it moved

The capability was first built in **`hushh-labs/husshone`** (the Next.js One webapp) as
`src/lib/burst/**` plus five API routes, a setup UI, a macOS Swift client and four design
documents. That was the wrong home: burst placement is a *local compute* decision, and
putting it behind a web route means the estimate — and eventually the workload — travels to
a server before anything can be decided about it.

**`husshone` is no longer the target for burst work.** New burst code lands here. The
husshone tree stays as the historical record and as the still-running v1 control plane until
its callers are cut over; it is read-only from this workstream's point of view.

## What is in husshone today

Inventory taken at husshone `80cb297` (2026-08-07), so a later reader can tell what was
surveyed rather than guessing.

| Area | Files | Lines | Phase |
|---|---|---|---|
| `src/lib/burst/placement.ts`, `types.ts` | 2 | 213 | **1 — landed here** |
| `src/lib/burst/providers/{gcp,gcp-tpu,gcp-common,mock}.ts`, `provider-factory.ts` | 5 | 534 | 2 — execution |
| `src/lib/burst/credentials.ts`, `lru.ts` | 2 | 179 | 2 — credential broker |
| `src/lib/burst/{setup,client,agent-card}.ts` | 3 | 410 | 3 — orchestration |
| `src/lib/db/burst-store.ts` | 1 | 189 | 3 — persistence |
| `src/app/api/one/burst/**` (5 routes) | 5 | 534 | control plane — stays until cut over |
| `src/app/burst/setup/**` | 2 | 222 | web surface — stays |
| `macos/OnePuppyAgent/**/BurstClient.swift` | 1 | 247 | native client — stays, retargets |
| `docs/xtreme-compute-burst*.md`, `docs/specs/burst-control-plane.openapi.yaml` | 4 | 679 | design record |

## Phase 1 — the decision layer (landed)

`hermes_cli/hussh_one_burst/` — an overlay module per
[the overlay rule](./README.md), so an upstream Hermes merge cannot touch it.

**Faithfully ported from husshone:** the placement engine. The offline rule, the TPU rule,
the unknown-size rule, the 80% safety fraction, the unified-memory `max(vram, ram)`
treatment and the binding-constraint messages all correspond to `placement.ts`.

**Originating in Hermes, with no husshone counterpart:** `hardware.py` (the accelerator
catalog and the perf-per-dollar matcher), `devices.py` (six device profiles, seven workload
presets), and the `AcceleratorClass` / `HardwareRecommendation` / `BenchmarkRow` /
`WorkloadPreset` types. husshone has exactly one device profile (`DEFAULT_PUPPY_PROFILE`,
a Mac Studio), no preset catalog, and no hardware matching at all.

> The commit that introduced this module described the whole thing as "ported from the
> TypeScript implementation" and claimed a parity harness across "42 preset × device
> decisions" comparing accelerator choice, chip count, hourly cost and benchmark rows.
> **No such comparison is possible**: husshone has no presets, no device list, no
> accelerator catalog and no benchmark rows to compare against. The claim is recorded here
> as withdrawn so it is not repeated. The code itself is sound and tested — only its stated
> provenance was wrong.

### Vocabulary — the one deliberate incompatibility

| Concept | husshone | Hermes |
|---|---|---|
| Runs on the person's machine | `puppy` | `device` |
| Runs in the person's cloud | `gcp` | `cloud` |

husshone's names are pinned in `types.ts`, in the `[puppy]` enums of
`burst-control-plane.openapi.yaml`, and in `BurstClient.swift`. They bake in one device tier
("a Mac") and one provider ("GCP"). Hermes has to describe a Windows workstation bursting to
a cloud that is not GCP, so it uses the general names.

The mapping is total and exact — `puppy ↔ device`, `gcp ↔ cloud`. Any adapter is a two-line
translation. **Write it deliberately at the boundary**; do not assume the two vocabularies
interoperate, because a `target` field will silently deserialize into neither.

## Phase 2 — execution and the credential broker (not started)

Two hard constraints, both inherited rather than invented here.

**Follow `hushh-research`'s GCP pattern; do not re-port `credentials.ts`.** That repo's
`consent-protocol/hushh_mcp/services/gcp_run_client.py` already solves this problem in
Python: `load_operator_credentials()` resolves a base64 service-account JSON or falls back
to Application Default Credentials, scoped to `cloud-platform`, and `GcpRunClient` layers
create / get / replace / delete / `wait_ready` on top. That is the same precedence
`credentials.ts` implements in TypeScript (per-request SA → env → ADC). Burst provisions
Compute Engine instances rather than Cloud Run services, so the resource calls differ — the
**auth half should be reused in pattern, not rewritten**.

**Pin execution to the person's own project.** The `claude/hushh-infrastructure-analysis-7o991c`
branch of `hushh-research` establishes this for pods: a user-owned pod pins model access to
the user's project, not the hub's. Burst is the same principle applied to accelerators — the
instance is provisioned in the customer's project with their credentials, and the
service-account key is never persisted. husshone's `credentials.ts` holds the key in memory
for one request and stores only `projectId` / `region` / `credsSource` on the job row; that
invariant carries over unchanged.

Also carried over from husshone's design, because each exists for a reason:

- **Teardown is not optional.** It runs on completion, failure *and* the soft deadline, and
  a `404` on delete counts as already-gone. A burst instance is never orphaned, because an
  orphaned accelerator bills by the hour.
- **The provider interface isolates the cloud.** `ComputeBurstProvider` exists so a second
  backend is a drop-in. Keep that seam.
- **A mock provider that needs no credentials** is what makes the path testable at all.

## Phase 3 — orchestration, persistence, receipts

Job store, resumable streams, the agent card, and receipt sealing. Deferred; the control
plane in husshone continues to serve v1 until these land.

## Status

| Claim | State |
|---|---|
| Decision layer exists in Hermes | ✅ committed, 31 tests, ruff clean |
| Decision layer reachable by a person | ❌ nothing imports it — no tool, command or config knob |
| Execution / credentials in Hermes | ❌ not started |
| husshone burst code removed | ❌ untouched; read-only from here |

The tool registration, config knob, feature page and contract row land with the capability
that first needs them — not before.

---

### Related
- [Architecture — the overlay model](./README.md)
- [Design record, migrated from husshone](../reference/xtreme-burst/README.md)
- [Changelog](../CHANGELOG.md)
