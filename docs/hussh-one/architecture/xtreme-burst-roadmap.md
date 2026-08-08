# Xtreme Burst — production-readiness scorecard

The persistent record of progress toward the goal: **Hermes decides, from live resource
measurement and workload requirements, when a workload should leave the device for cloud
supercomputing — and that decision is made by an ADK subagent.**

Update this page as each phase completes. Every KPI carries Status / Target / Gap / Next
Action, and every number is either counted from the tree or explicitly marked *modeled*.

- **Last updated:** 2026-08-08 · Hermes `3a99e96` · husshone surveyed at `80cb297` ·
  hushh-research `claude/hushh-infrastructure-analysis-7o991c`
- **Phase:** 1 of 4 complete — *and not reachable by a person*
- **Capability delivered to a person: 0%.** Nothing imports the burst module.

## The one-line summary

The decision engine is correct, tested, and **blind**. It decides from a static hardware
catalog, not from the machine it is running on. Making it see is the next phase, and it is
worth more than any amount of further placement logic.

---

## 1. Current achievements

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 1.1 Decision-layer correctness | ✅ 31/31 tests, ruff clean | 100% green | none | Hold; re-run on every change |
| 1.2 Placement rules ported | ✅ 6 rules (offline, TPU, unknown-size, 80% safety, unified-memory, binding constraint) | parity with `placement.ts` | none | Hold |
| 1.3 Provenance accuracy | ✅ corrected 2026-08-07 | claims survive checking | none | Keep ported/originating split in docstrings |
| 1.4 Design record migrated | ✅ 4 docs + OpenAPI, behind history banner | durable in Hermes | none | — |
| 1.5 Module is pure (no I/O) | ✅ no network, credential, or clock | privacy invariant holds | none | **Guard this** — it is the privacy argument |

**Counted:** 544 lines across 5 modules in `hermes_cli/hussh_one_burst/`.

## 2. Remaining implementation gaps

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 2.1 **Reachability** | ❌ **0 call sites** | ≥1 (tool, command, or MCP) | **total — the #1 blocker** | Expose over MCP; see 6.3 |
| 2.2 Execution layer | ❌ 0 of ~713 lines | providers + credential broker | full phase | Phase 2 |
| 2.3 Orchestration + persistence | ❌ 0 of ~599 lines | job store, streams, agent card | full phase | Phase 3 |
| 2.4 Repo consolidation | ⚠️ husshone still owns v1 (~1,003 lines: routes/UI/Swift) | one home | read-only access here | Re-attach husshone with push access |

The only reference to `hussh_one_burst` outside its own package and tests is a path string
in `scripts/hussh-one-changelog-check.py`. That is not a caller.

## 3. Resource monitoring readiness

Hermes has more here than expected — and nothing that serves placement.

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 3.1 `psutil` available | ✅ pinned `7.2.2`, used in 10+ modules | dependency present | none | Reuse, don't add |
| 3.2 Power / battery sensing | ✅ `agent/battery.py` — `percent`, `plugged`, `charging()` | usable signal | none | **Feed into placement** — don't burn battery on a heavy local job |
| 3.3 System RAM headroom | ⚠️ `gateway/memory_monitor.py` measures **process RSS only**, and logs it | queryable free-RAM API | no system-wide read, no return value | Add a sampler returning free/total |
| 3.4 CPU load sampling | ❌ none for placement | rolling load average | full | Add to sampler |
| 3.5 **GPU / VRAM detection** | ❌ **zero** | measured VRAM + utilization | **full — the critical gap** | Add detection per platform |
| 3.6 Thermal / sustained throttle | ❌ none | detect throttled state | full | Phase 2+; a throttled Mac is a different machine |

> **VRAM appears in Hermes only as comments and as static catalog values.** `devices.py`
> ships six hand-written `DeviceProfile`s; `placement.py` consumes whichever one it is
> handed. Nothing measures the actual machine. "Real-time resource monitoring" is, today, a
> lookup table — this row is the single largest gap between the stated goal and the tree.

## 4. Cloud offload decision logic

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 4.1 Placement rules correct | ✅ tested | invariants hold | none | Hold |
| 4.2 **Inputs are live** | ❌ static profile | measured device state | **engine is correct but blind** | Wire §3 sampler into `PlacementInput` |
| 4.3 Cost-aware selection | ⚠️ `hardware.py` perf-per-dollar matcher exists; prices are *modeled* | validated against real pricing | never checked against an invoice | Validate catalog vs live GCP pricing |
| 4.4 **Consent gate before offload** | ❌ none | explicit consent + receipt | **full — Hushh-critical** | No workload leaves the device without a consent check |
| 4.5 Deadline / soft-deadline | ❌ not in Hermes | soft deadline drives teardown | full | Phase 2 |

## 5. Xtreme Burst relay readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 5.1 Wire contract | ⚠️ v1 OpenAPI exists; Hermes deliberately incompatible | one agreed contract | `device`/`cloud` ↔ `puppy`/`gcp` adapter unwritten | Write the 2-line adapter at the boundary |
| 5.2 Provider abstraction | ❌ in Hermes | pluggable backend | full | Model on `ComputeBackend` Protocol (hushh-research) |
| 5.3 Credential broker | ❌ | BYOC, key never persisted | full | Reuse `load_operator_credentials()` precedence — do not re-port `credentials.ts` |
| 5.4 **Teardown guarantee** | ❌ **none** | runs on success, failure *and* deadline | **full — highest financial risk** | Build teardown *before* first provision |
| 5.5 Native client retarget | ❌ `BurstClient.swift` points at husshone | points at Hermes | full | Phase 3 |

> Ship teardown before provisioning. An orphaned accelerator bills by the hour, and the
> first real burst is exactly when that lesson is most expensive to learn.

## 6. ADK runtime and subagent integration

**The ADK runtime is real, tested, and in the other repo.**

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 6.1 ADK runtime exists | ✅ `google-adk[a2a]==2.4.0`, root `one` `LlmAgent`, 5 ADK test files | working runtime | **lives in hushh-research, not Hermes** | — |
| 6.2 **Offload subagent registered** | ❌ **none** | an offload specialist in the roster | full | Add following the `ask_*_agent` template |
| 6.3 **Cross-repo bridge** | ❌ none | Hermes capability reachable by the ADK root | full | **Expose burst as an MCP server** |
| 6.4 Specialist availability gating | ⚠️ mechanism exists (`specialist_availability.py`); burst absent from `_SPECIALIST_LABELS` | gated like every specialist | registration | Add label + wiring check |
| 6.5 ADK 2.4.0 tool-list constraint | ⚠️ known: `LlmAgent.tools=[...]` unstable at this version | follow existing workaround | — | Do not hand-roll; copy the roster pattern |

**The bridge already has precedent in this codebase.** `hermes_cli/hussh_one_pkm/mcp_server.py`
shows a Hermes overlay exposing itself over MCP, registered in `mcp_config.py` as
`python -m hermes_cli.hussh_one_pkm.mcp_server`. Burst should reach the ADK runtime the same
way — it solves 2.1 (reachability) and 6.3 (bridge) with one mechanism, and keeps the
decision layer pure.

**Target shape.** In `one_adk/agent_tree.py`, an offload specialist alongside the existing
roster, delegating through `_specialist_turn` exactly as `ask_email_agent` does — the
docstring is the tool description the root agent reasons over:

```
async def ask_compute_agent(request, tool_context) -> dict[str, Any]:
    """Ask the Compute specialist whether a workload should run on this device
    or burst to the person's own cloud, and what hardware it needs."""
    return await _specialist_turn("agent_compute", request, tool_context)
```

The subagent calls the Hermes MCP tool for the decision. Placement stays pure and local; the
subagent supplies judgment, consent and narration.

## 7. End-to-end execution and validation

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 7.1 E2E run | ❌ 0% — never executed | device → decide → burst → result | full | Phase 2 exit criterion |
| 7.2 Mock provider | ❌ in Hermes | credential-free test path | full | Port first — it unblocks all other testing |
| 7.3 **CI coverage** | ⚠️ 31 tests green **locally only** | green in CI | **`ci.yml` has never run on this branch** — it fires on PRs and pushes to `main` | Open a PR, or extend triggers |
| 7.4 Integration test vs real GCP | ❌ | one provisioned + torn-down instance | full | After 5.4 |

## 8. Performance, cost, and scalability

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 8.1 Decision latency | ✅ *likely* <10ms — pure, no I/O; 31 tests in 0.90s | <10ms | **unmeasured** | Add a benchmark; claim only once measured |
| 8.2 Cost-model accuracy | ⚠️ *modeled* | within 10% of invoice | never validated | Compare against one real burst |
| 8.3 Teardown SLO | ❌ | 100% within 60s of terminal state | no teardown exists | Phase 2 |
| 8.4 Concurrent jobs | ❌ no job store in Hermes | ≥1 tracked concurrently | full | Phase 3 |
| 8.5 Monitoring overhead | ❌ n/a | sampler <1% CPU | sampler doesn't exist | Budget when built (§3) |

## 9. Production readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 9.1 Reachable by a person | ❌ | 1 documented entry point | total | §6.3 |
| 9.2 Consent + audit receipts | ❌ | every offload leaves a receipt | full | §4.4 |
| 9.3 Secrets posture | ✅ nothing to leak — no credential code yet | key never persisted | none *yet* | Re-assess at 5.3, when it becomes real |
| 9.4 Ops runbook | ⚠️ architecture + migration record exist | runbook with teardown drill | no runbook | Phase 2 |
| 9.5 Privacy invariant | ✅ decision is local, contents never inspected | holds through every phase | none | **Regression-test it each phase** |

**Overall: not production-ready.** One of four phases is complete, and its output is
unreachable. Engineering progress ≈ 25% of the backend; delivered capability 0%.

---

## Priority order

1. **Make the decision layer see** (§3.3–3.5). A correct engine on static inputs cannot do
   the job that was asked for. Highest value per line.
2. **Make it reachable** (§2.1 / §6.3) — one MCP server closes reachability and the
   cross-repo bridge together.
3. **Register the offload subagent** (§6.2) — this is where the goal is actually met.
4. **Teardown before provisioning** (§5.4) — build the brake before the accelerator.
5. **Consent gate** (§4.4) — before any workload leaves the device, not after.
6. Execution + credentials (§2.2), then orchestration (§2.3).

## Update protocol

On each phase completion: flip the Status cells, restate the counted numbers, move the date
and SHA line at the top, add a CHANGELOG row, and — per the correction in the migration
record — **only claim what was measured.** A KPI that was not re-checked stays as it was
rather than being upgraded on the assumption it still holds.

---

### Related
- [Architecture & migration record](./xtreme-burst.md)
- [husshone design record](../reference/xtreme-burst/README.md)
- [Changelog](../CHANGELOG.md)
