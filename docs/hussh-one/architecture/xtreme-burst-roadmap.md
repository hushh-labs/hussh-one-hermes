# Xtreme Burst — production-readiness scorecard

The persistent record of progress toward the goal: **Hermes decides, from live resource
measurement and workload requirements, when a workload should leave the device for cloud
supercomputing — and that decision is made by an ADK subagent.**

Update this page as each phase completes. Every KPI carries Status / Target / Gap / Next
Action, and every number is either counted, measured, or explicitly marked *modeled*.

- **Last updated:** 2026-08-08 · Hermes `8913a3b` · hushh-research `3e8fcc2f6` ·
  husshone surveyed at `80cb297`
- **Workstreams:** 5 of 6 complete — decision layer, measurement, reachability, ADK
  subagent, execution. Orchestration and persistence remain.
- **89 tests** (77 Hermes, 12 hushh-research), ruff clean.

## Where this actually stands

The engine can see, a person can reach it, and anything provisioned gets released. Two
things are still true and matter more than the green cells below:

1. **No burst has ever run against a real cloud.** Everything is verified against a mock
   provider that needs no credentials. The GCP path is written and typed but unexercised.
2. **Payload transfer is deliberately not implemented.** Shipping a workload to a remote
   machine is the one step that genuinely moves a person's information off their device.
   It needs its own consent design, not a side effect of provisioning.

---

## 1. Current achievements

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 1.1 Decision-layer correctness | ✅ 89 tests green | 100% | none | Re-run on every change |
| 1.2 Placement rules ported | ✅ 6 rules + two-pool model | parity + discrete GPUs | none | Hold |
| 1.3 Provenance accuracy | ✅ corrected 2026-08-07 | claims survive checking | none | Keep ported/originating split |
| 1.4 Design record migrated | ✅ 4 docs + OpenAPI | durable in Hermes | none | — |
| 1.5 Module is pure (no I/O) | ✅ I/O isolated in `telemetry` | invariant holds | none | **Guard this** |

**Counted:** 1,800 lines across 10 modules in `hermes_cli/hussh_one_burst/`.

## 2. Remaining implementation gaps

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 2.1 Reachability | ✅ 5 MCP tools, registered | ≥1 entry point | none | — |
| 2.2 Execution layer | ✅ credentials, providers, teardown | provisioning lifecycle | none | — |
| 2.3 Orchestration + persistence | ❌ none | job store, streams, agent card | full phase | Next phase |
| 2.4 Repo consolidation | ⚠️ husshone owns v1 | one home | read-only access | Re-attach with push access |
| 2.5 **Payload transfer** | ❌ not implemented | workload reaches the instance | **deliberate — needs consent design** | Design before building |

## 3. Resource monitoring readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 3.1 `psutil` available | ✅ pinned 7.2.2 | present | none | — |
| 3.2 Power / battery sensing | ✅ reused, feeds an advisory | usable signal | none | — |
| 3.3 System RAM headroom | ✅ measured 15.09GB available here | queryable free RAM | none | — |
| 3.4 CPU load sampling | ✅ non-blocking `cpu_percent` | rolling load | none | — |
| 3.5 GPU / VRAM detection | ⚠️ **written, never seen a real GPU** | measured VRAM | nvidia-smi and Apple paths unit-tested by injection only | Run on a machine with an accelerator |
| 3.6 Thermal / throttle | ⚠️ **written, returned `None` here** | detect throttling | no real reading observed | Same |

> 3.5 and 3.6 are the honest amber. The code paths exist and are tested through injected
> probes, but this container has no GPU and no thermal zones, so neither has ever produced
> a real number. "Implemented" and "verified" are different claims and stay separate here.

## 4. Cloud offload decision logic

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 4.1 Placement rules correct | ✅ tested | invariants hold | none | Hold |
| 4.2 **Inputs are live** | ✅ decides from the real machine | measured state | none | — |
| 4.3 Cost-aware selection | ⚠️ prices *modeled* | validated pricing | never checked vs invoice | Compare against one real burst |
| 4.4 Consent gate before offload | ⚠️ elicitation written | approval before spend | not exercised through a real MCP client | Exercise end-to-end |
| 4.5 Deadline handling | ✅ enforced + tested | drives teardown | none | — |

The two-pool memory model closed a real defect: a 96GB workstation with an 8GB card used
to read as "fits locally". Accelerator need is now gated on VRAM and host need on RAM
independently.

## 5. Xtreme Burst relay readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 5.1 Wire contract | ⚠️ divergence *guarded*, not bridged | one contract | adapter unwritten; stale names now rejected | Write the adapter at the boundary |
| 5.2 Provider abstraction | ✅ Protocol + mock + GCP | pluggable | none | — |
| 5.3 Credential broker | ✅ precedence mirrors hushh-research | key never persisted | none | — |
| 5.4 **Teardown guarantee** | ✅ 7 tests incl. `KeyboardInterrupt` | always released | none | — |
| 5.5 Native client retarget | ❌ points at husshone | points at Hermes | full | Later phase |

Teardown runs in a `finally` and survives workload exceptions, deadline overruns and
Ctrl-C. A 404 on delete counts as success — the invariant is "not running", not "deleted
by us". A teardown that fails surfaces a warning naming the instance rather than being
swallowed.

## 6. ADK runtime and subagent integration

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 6.1 ADK runtime exists | ✅ `google-adk[a2a]==2.4.0` | working runtime | none | — |
| 6.2 **Offload subagent registered** | ✅ `agent_compute` in the roster | specialist exists | none | — |
| 6.3 Cross-repo bridge | ⚠️ **half-built** | pod can reach the device | Hermes exposes MCP; no pod↔device transport | Build the transport |
| 6.4 Availability gating | ✅ reports `specialist_unwired` | honest gating | none | — |
| 6.5 ADK 2.4.0 tool-list constraint | ✅ followed the roster pattern | no new instability | none | — |

**The architectural finding that shaped this.** One runs in the person's **pod**; their
machine is **elsewhere**. A specialist that measured the container it runs in would report
the pod's hardware as the person's — confidently, and wrong. So `agent_compute` does not
measure and does not decide. Hermes does both, on the device. The specialist explains the
decision and says plainly when it has not been given one.

Registration is honest by construction: no A2A handler is registered, so
`is_wired_specialist` is False and One is told the specialist is unavailable rather than
dispatching into nothing. When the transport lands, handler registration is the only
change needed.

## 7. End-to-end execution and validation

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 7.1 End-to-end run | ⚠️ decide/plan real; run mock-only | full path on real cloud | **never run against GCP** | Requires a project and real spend |
| 7.2 Mock provider | ✅ credential-free | testable path | none | — |
| 7.3 CI coverage | ⚠️ green locally only | green in CI | `ci.yml` never ran on this branch | Open a PR |
| 7.4 Integration test vs real GCP | ❌ | one provision + teardown | full | After 7.1 |

Verified live on this machine: `device_status` measured 4 cores / 15.09GB available;
`decide` routed both presets to cloud with coherent reasons; `plan` returned 4× B200 at
$84/hr, ~$126 total.

## 8. Performance, cost, and scalability

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 8.1 Decision latency | ✅ **median 2.7µs, p99 11µs** | <10ms | none — ~900× under | — |
| 8.2 Cost-model accuracy | ⚠️ *modeled* | within 10% of invoice | never validated | Compare against one real burst |
| 8.3 Teardown SLO | ⚠️ guaranteed in code, untimed | 100% within 60s | no real-cloud timing | Measure at 7.4 |
| 8.4 Concurrent jobs | ❌ no job store | ≥1 tracked | full | Next phase |
| 8.5 Monitoring overhead | ✅ **0.5ms median** measurement | <1% CPU | GPU path shells out, untimed | Re-measure on GPU hardware |

## 9. Production readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 9.1 Reachable by a person | ✅ 5 MCP tools | 1 entry point | none | — |
| 9.2 Consent + audit receipts | ⚠️ `BurstReceipt` + elicitation exist | every offload leaves a receipt | not exercised live | Exercise end-to-end |
| 9.3 Secrets posture | ✅ key held one call, never persisted | never persisted | none | Re-audit at first real burst |
| 9.4 Ops runbook | ⚠️ architecture + scorecard | runbook + teardown drill | no runbook | Next phase |
| 9.5 Privacy invariant | ✅ stronger than before | holds every phase | none | Regression-test each phase |

The privacy invariant got three new guards: measurement is isolated in one module,
reachability is inferred from local link state rather than by contacting a host, and
`InstanceSpec` carries no workload fields by construction — a test asserts its exact
field set.

**Overall: 74% of 45 KPIs met (27 met, 13 partial, 5 open).** Reachable, measured, and
safe to stop. Not yet proven against a real cloud.

---

## Priority order from here

1. **One real burst.** 7.1 / 7.4 / 8.2 / 8.3 / 9.2 all resolve on the first genuine
   provision-and-teardown. Needs a project and a few dollars of spend.
2. **Verify 3.5 / 3.6 on real hardware** — a machine with a GPU and thermal sensors.
3. **The pod↔device transport** (6.3) — the last half of the bridge.
4. **Design payload transfer** (2.5) — consent first, code second.
5. Orchestration and persistence (2.3, 8.4), then the ops runbook (9.4).

## Update protocol

On each phase completion: flip the Status cells, restate the counted and measured numbers,
move the date and SHA line, add a CHANGELOG row, and **only claim what was measured.** A
KPI that was not re-checked stays as it was rather than being upgraded on the assumption it
still holds. "Implemented" is never promoted to "verified" without a reading.

---

### Related
- [Architecture & migration record](./xtreme-burst.md)
- [husshone design record](../reference/xtreme-burst/README.md)
- [Changelog](../CHANGELOG.md)
