# Xtreme Burst — production-readiness scorecard

The persistent record of progress toward the goal: **Hermes decides, from live resource
measurement and workload requirements, when a workload should leave the device for cloud
supercomputing — and that decision is made by an ADK subagent.**

Update this page as each phase completes. Every KPI carries Status / Target / Gap / Next
Action, and every number is either counted, measured, or explicitly marked *modeled*.

- **Last updated:** 2026-09-04 · Hermes `b332f54`+ · hushh-research `3e8fcc2f6` ·
  husshone surveyed at `80cb297`
- **Workstreams:** 5 of 6 complete — decision layer, measurement, reachability, ADK
  subagent, execution. Orchestration and persistence remain.
- **140 tests** (128 Hermes burst, 12 hushh-research), ruff clean. A further **63** were
  restored to the suite by fixing a collection error that had silently disabled them.

## What self-review found

A pass over the code after it was written turned up a family of defects that shared one
shape: **a number shown to a person that did not describe the machine they would get.**
None were caught by the tests written alongside the code, and all were found by
cross-checking two components against each other.

| Defect | Consequence |
|---|---|
| Whole-node parts priced per chip | H100/H200/B200 sell only as 8-GPU nodes; a 90GB job was quoted one H200 chip when GCP bills eight — **12× understated** |
| Recommender and provisioner disagreed | 9 of 14 realistic workloads produced a recommendation Compute Engine cannot fulfil |
| GCP request body incomplete | No `machineType`, `disks` or `networkInterfaces` — it would have 400'd on arrival |
| TPUs built as Compute Engine VMs | Would boot an accelerator-less VM that bills hourly doing nothing |
| Instance names derived from shape alone | Two concurrent bursts of one shape collide with a 409 |
| Ranking scored a config it did not deliver | Scored 4 chips, billed 8; lost to a node finishing 6× sooner for less |
| `parallel_chips` silently negotiable | Asking for 8× parallel could return a 4× machine |
| Benchmark table carried the same pricing bug | The artifact a person audits priced machines no cloud sells |
| `run` never checked `fits` | Would take payment for hardware too small for the job |
| **Teardown trusted an accepted `DELETE`** | Reported `torn_down: true` while a T4 was still STAGING and billing — *found only by the real burst* |
| Default boot image family did not exist | 404 against the live API; the first provision would have failed |

All are fixed and regression-tested. The lesson is recorded because it generalises: the
first four survived a full green test suite, so **the tests were confirming the code's own
assumptions rather than checking it against the world.** Every fix above came with a test
that asserts against an external fact — what a cloud sells, what an API requires — rather
than against the implementation.

## Where this actually stands

The engine can see, a person can reach it, and anything provisioned gets released. Two
things are still true and matter more than the green cells below:

1. **One real burst has now run.** A T4 was provisioned in `hushh-pda-dev`, released, and
   independently confirmed gone — 33.9s, $0.0033. It immediately found the most serious
   defect in the whole feature (see below). One sample is not a track record.
2. **Payload transfer is deliberately not implemented.** Shipping a workload to a remote
   machine is the one step that genuinely moves a person's information off their device.
   It needs its own consent design, not a side effect of provisioning.

---

## 1. Current achievements

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 1.1 Decision-layer correctness | ✅ 151 tests green | 100% | none | Re-run on every change |
| 1.2 Placement rules ported | ✅ 6 rules + two-pool model | parity + discrete GPUs | none | Hold |
| 1.3 Provenance accuracy | ✅ corrected 2026-08-07 | claims survive checking | none | Keep ported/originating split |
| 1.4 Design record migrated | ✅ 4 docs + OpenAPI, **links now checked** | durable in Hermes | 3 cross-references in `design.md` point into husshone and are kept broken on purpose | Hold — rewriting them would cost the *verbatim* guarantee |
| 1.5 Module is pure (no I/O) | ✅ I/O isolated in `telemetry` | invariant holds | none | **Guard this** |

**Counted:** 2,382 lines across 12 modules in `hermes_cli/hussh_one_burst/`.

## 2. Remaining implementation gaps

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 2.1 Reachability | ✅ 5 MCP tools, registered | ≥1 entry point | none | — |
| 2.2 Execution layer | ✅ credentials, providers, teardown | provisioning lifecycle | none | — |
| 2.3 Orchestration + persistence | ⚠️ receipts persist (`ledger.py`) | job store, streams, agent card | streams + agent card | Build when a caller needs them |
| 2.4 Repo consolidation | ⚠️ husshone owns v1 | one home | read-only access | Re-attach with push access |
| 2.5 **Payload transfer** | ⚠️ [design written](./xtreme-burst-payload-transfer.md), not built | workload reaches the instance | consent artifact + 3 open questions need a human | Answer the open questions |

## 3. Resource monitoring readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 3.1 `psutil` available | ✅ pinned 7.2.2 | present | none | — |
| 3.2 Power / battery sensing | ✅ feeds advisories, which now **accumulate** | usable signal | advises, never decides — placement ignores power | Decide whether power should bind placement |
| 3.3 System RAM headroom | ✅ measured 15.09GB available here | queryable free RAM | none | — |
| 3.4 CPU load sampling | ✅ non-blocking; **first sample reports unknown, not `0.0`** | rolling load | displayed only — placement ignores it | Decide whether load should bind placement |
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
| 4.3 Cost-aware selection | ⚠️ structure fixed; prices still *modeled* | validated pricing | never checked vs invoice | Compare against one real burst |
| 4.4 **Consent gate before offload** | ✅ **exercised through a real MCP client**, all four answers | approval before spend | none | Re-check when payload transfer lands |
| 4.5 Deadline handling | ✅ **enforced by GCP**, detected locally | drives teardown | a hung `execute` is not interrupted — `run_burst` reads the clock after it returns | Revisit when the payload seam (2.5) can be interrupted |
| 4.6 **Quote is purchasable** | ✅ sellable counts **and a live zone/quota pre-flight in both `plan` and `run`** | quote = what is billed | none | Validate a quote against a real invoice |

**The consent gate is now exercised, not just written.** Every other test of
`hussh_burst_run` mocks the elicitation away, which proves the handler branches and proves
nothing about whether a person is ever asked. Driven by a real MCP client through the SDK's
`elicitation_callback`, all four answers behave: accept runs and tears down, decline and
cancel provision nothing, and **accept-with-the-box-unticked is a refusal** — a person who
opened the dialog and said no. The prompt itself is asserted to name the hardware, the rate,
the total and the teardown promise, because consent to an unnamed price is not consent.

Two defects fell out of doing it for real rather than in-process:

* **Driving the real tool wrote mock receipts into the owner's ledger.** `hussh_burst_run`
  records unconditionally, so a simulated burst landed in `~/.hermes/burst-receipts.jsonl`
  looking like any other — same `status: completed`, same `success: true` — in the one file
  somebody opens to ask what a burst cost. Receipts now carry `simulated: true`, and the
  consent tests point `HERMES_HOME` at a temp profile, with a test pinning that the receipt
  lands *there* and a refusal writes nothing at all.
* **The first fix was too blunt.** Filtering simulated rows out of `leaked_instances()`
  broke the leak test, and rightly: `MockBurstProvider(fail_on_teardown=True)` is exactly
  how the leak path gets exercised without spending money. Rows are marked, not dropped; a
  caller wanting real bursts only can say so explicitly.

The two-pool memory model closed a real defect: a 96GB workstation with an 8GB card used
to read as "fits locally". Accelerator need is now gated on VRAM and host need on RAM
independently.

**Sellable is not the same as obtainable.** 4.6 was met on the vendor's terms — whole-node
counts, so the quote matches the bill. It said nothing about whether *this* project in
*this* zone can get the part. Checked against `hushh-pda-dev` on 2026-09-04:
`us-central1-a` carries GB200 and H100 and carries **neither H200 nor B200**, which the
recommender quotes at $88 and $110 an hour; spot quota for A100-80GB is **0** while the
catalog offers it. Both would have been discovered by the person, after approving, at
provision time. `GcpBurstProvider.preflight` now asks the project before the elicitation
and refuses with the number it read. It refuses only on positive evidence: Compute v1
publishes no spot-quota metric at all for H100 and newer — verified across five regions —
so those become a caveat shown to the person, not a refusal, because refusing on an absent
reading is a confident guess pointed the other way.

## 5. Xtreme Burst relay readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 5.1 Wire contract | ✅ `wire.py` translates and refuses both ways | one contract | **no caller** — correct but unapplied | Use it at the first husshone boundary, before anyone hand-rolls the mapping |
| 5.2 Provider abstraction | ✅ Protocol + mock + GCP; pre-flight is a **declared** optional capability | pluggable | none | — |
| 5.3 Credential broker | ✅ precedence mirrors hushh-research | key never persisted | none | — |
| 5.4 **Teardown guarantee** | ✅ confirms absence, **and sweeps after a create that never answered** | always released | none | Extend to bucket+secret at 2.5 |
| 5.5 Native client retarget | ❌ points at husshone | points at Hermes | full | Later phase |

Teardown runs in a `finally` and survives workload exceptions, deadline overruns and
Ctrl-C. A 404 on delete counts as success — the invariant is "not running", not "deleted
by us". A teardown that fails surfaces a warning naming the instance rather than being
swallowed.

**Pluggable meant discoverable, and for two hours it was not.** `GcpBurstProvider.preflight`
was added without touching the Protocol, and both call sites reached it through
`getattr(backend, "preflight", None)`. A second cloud implemented faithfully against
`BurstProvider` would have satisfied the contract, skipped the zone/quota check, and let a
person approve hardware their project cannot get — the exact failure the pre-flight exists
to prevent, reintroduced through the seam meant to make backends interchangeable. It is now
a `runtime_checkable` `SupportsPreflight` Protocol, deliberately separate because a mock has
no project to ask, and a test builds a backend this package has never heard of to prove it
gets pre-flighted purely by declaring the method.

**`provision_failed` used to mean "nothing to release" unconditionally**, and a POST that
times out or has its connection dropped says nothing about whether Compute Engine accepted
the create — the same mistake as trusting an accepted `DELETE`, pointed the other way. The
instance name is chosen *before* the request, which is what makes it recoverable: on a
failed create the provider goes and looks for that name, deletes what it finds, and raises
`OrphanedInstance` carrying the id when it cannot confirm removal. The receipt then reports
a leak with a name somebody can act on, rather than a bill nobody can trace. SPOT plus
`maxRunDuration` bounds the loss either way; this is about the receipt telling the truth.

## 6. ADK runtime and subagent integration

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 6.1 ADK runtime exists | ✅ `google-adk[a2a]==2.4.0` | working runtime | none | — |
| 6.2 **Offload subagent registered** | ✅ in the roster **and attributable**; roster read off source by three guards | specialist exists | none | — |
| 6.3 Cross-repo bridge | ⚠️ **half-built** | pod can reach the device | Hermes exposes MCP; no pod↔device transport | Build the transport |
| 6.4 Availability gating | ✅ reports `specialist_unwired` | honest gating | none | — |
| 6.5 ADK 2.4.0 tool-list constraint | ✅ followed the roster pattern | no new instability | none | — |

**The architectural finding that shaped this.** One runs in the person's **pod**; their
machine is **elsewhere**. A specialist that measured the container it runs in would report
the pod's hardware as the person's — confidently, and wrong. So `agent_compute` does not
measure and does not decide. Hermes does both, on the device. The specialist explains the
decision and says plainly when it has not been given one.

> **Registered was one step short of reachable, again.** `agent_compute` was in the roster
> `build_one_root_agent` hands One, and in `_SPECIALIST_LABELS`, and missing from
> `text_runtime`'s `_SPECIALIST_TOOL_SOURCES` — the table Agent Chat reads to say *which*
> specialist answered. One could consult Compute and the person would read the answer with
> nothing on it. The test that passed asserted `ask_compute_agent` was importable and
> callable, which says nothing about whether One can reach it. Three guards now derive the
> specialist set from `agent_tree`'s source and require every one of them to be in the
> roster, in the source table, and in the labels — so the next specialist is covered on the
> day it is added. Fixed in `hushh-research@45f885028`.

Registration is honest by construction: no A2A handler is registered, so
`is_wired_specialist` is False and One is told the specialist is unavailable rather than
dispatching into nothing. When the transport lands, handler registration is the only
change needed.

## 7. End-to-end execution and validation

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 7.1 End-to-end run | ✅ **provisioned + released a real T4** | full path on real cloud | none | — |
| 7.2 Mock provider | ✅ credential-free — **proven with the environment stripped** | testable path | none | — |
| 7.3 CI coverage | ❌ **PR cannot reach green**; the suite was run locally instead | green in CI | the two large-runner jobs are **never assigned a runner** | Needs org access: runner capacity |
| 7.4 Integration test vs real GCP | ✅ `hushh-pda-dev`, 404 confirmed after | one provision + teardown | not yet automated in CI | Automate behind an opt-in marker |

Verified live on this machine: `device_status` measured 4 cores / 15.09GB available;
`decide` routed both presets to cloud with coherent reasons; `plan` returned 4× B200 at
$84/hr, ~$126 total.

### Why CI cannot tell us — three corrections, the last one settled by experiment

**Correction 3 supersedes both earlier ones.** All three are kept, because the sequence is
the lesson.

1. I reported the two required test jobs as "cancelled at exactly 31 minutes."
2. I corrected that to "still queued at 18 minutes," and built a conclusion on it. **That
   run never happened** — I inferred elapsed time from a wall clock I assumed rather than
   read. The real figure was 3m24s.
3. Then the durations themselves turned out to be the wrong measurement.

**Every run lifetime in the first two corrections measured my own push cadence.** Four runs
ended within seconds of a push of mine:

| Run | Aggregator concluded | My push | Δ |
|---|---|---|---|
| `ccedd0d5` | 05:25:38Z | 05:25:24Z | 14s |
| `77b16b3cf` | 05:56:04Z | 05:55:46Z | 18s |
| `1e0c89b9c` | 07:00:28Z | 07:00:10Z | 18s |
| `db07460d9` | 07:16:40Z | 07:16:25Z | 15s |

Four for four is a mechanism, not a coincidence: the push cancels the queued jobs through
the concurrency group, which releases the aggregator, which then reports failure because
the lints genuinely had failed. The spread I could not explain — 57s, 1m27s, 1m40s, 1m49s,
2m34s, 2m57s, then 15m55s and 55m7s — is just how long I happened to stay quiet. There is
no queue timeout, and nothing ever supported the jobs getting runners.

**Tested rather than assumed.** Prediction: with nobody pushing, a run stays open
indefinitely. `6b680f0c2` was pushed at 07:16:25Z, settled to those two jobs pending at
07:18:33Z, and at **07:53Z was still open — 35 minutes untouched**, against 14–18 seconds
whenever a push arrived. Confirmed. (The window is only a lower bound: recording this
finding required a push, which ends it. The next quiet interval extends the number.)

**What is established:** these two jobs are never assigned a runner in any window they get,
while inside the same run every `ubuntu-latest` and `macos-latest` job is assigned one in
**2–9 seconds** and finishes. They do not time out; they simply never start.

**Now measured, not inferred.** The API came back at 16:24Z after roughly eleven hours of
`invalid session`. Two runs read identically, and the second is the strongest evidence in
this whole workstream:

```
run 33850894288 (68e73068e)   both jobs   07:55:26Z -> 09:38:06Z   1h42m40s
run 33859313405 (ac4498cdf)   both jobs   09:38:44Z -> 16:26:53Z   6h48m09s
  labels: ubuntu-latest-96-core / windows-latest-32-core
  runner_id 0   runner_name ""   runner_group_id 0   runner_group_name ""
  conclusion: cancelled
```

**Six hours and forty-eight minutes without a runner**, ending nine seconds after a push —
the seventh instance of that signature. That settles it at the level the evidence allows.
The alternative worth ruling out — that they were eventually assigned runners and simply
ran long — is refuted outright: a job that ran would carry a runner id and steps, and these
carry neither.

**What is still a reading rather than a measurement:** *why*. `runner_group_id: 0` on an
unassigned job is an unset field, not the identity of a misconfigured group, so it names no
culprit. Capacity or runner-group configuration on those two labels remains the best
explanation — the label is the only variable, and every `ubuntu-latest` and `macos-latest`
job in the same run was assigned within 2–9 seconds — but nothing in the API confirms the
cause, only the effect.

**What I withdrew:** that fixing the inherited lint failures would not free the test jobs.
That came from the imaginary 18-minute run. It is now moot in the other direction — the
`PLW1514` fix landed (`db07460d9`) and seven footgun fixes with it (`6b680f0c2`), and the
jobs still never start, because the aggregator was never what was blocking them.

**Method, three times over.** First: reading superseded runs as facts about CI when they
were facts about my own cadence. Second: correcting a fabricated number with another
fabricated number in the same breath. Third, and the only one that worked: stating a
falsifiable prediction, changing nothing, and watching. A duration needs two clock
readings. A mechanism needs an experiment.

### What CI could not tell us, run locally instead

The full suite — 34,424 tests, four ways in parallel — finished at **39,100 passed, 94
failed across 48 files**. None of the 94 is in a file this workstream authored or touched.

That claim was checked rather than asserted. The 48 failing files were re-run on this
branch and again on a clean `origin/main` worktree, and the two sorted failure lists are
**identical — 94 and 94, empty in both directions**. Every failure this branch has,
`main` has. (An earlier aggregate showed 95 against 94; the sorted sets match, so that was
one flaky test, not a difference. Counting the totals would have said "one regression";
diffing the names said what was true.)

The burst suite itself is 173 green, and every burst change in this round is
mutation-checked — the defect re-introduced, the new test confirmed to fail, the fix
confirmed to restore it.

## 8. Performance, cost, and scalability

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 8.1 Decision latency | ✅ **2.3ms median / 4.7ms p95 over real MCP**; the pure function is 2.7µs | <10ms | none | Re-measure on a machine with a GPU probe to shell out to |
| 8.2 Cost-model accuracy | ⚠️ *modeled* | within 10% of invoice | never validated | Compare against one real burst |
| 8.3 Teardown SLO | ✅ **33.9s** full lifecycle, confirmed absent | 100% within 60s | single sample | Re-measure across shapes |
| 8.4 Concurrent jobs | ❌ no job store | ≥1 tracked | full | Next phase |
| 8.5 Monitoring overhead | ✅ warm **0.40ms**; **first call in a process 13.5ms** | <1% CPU | GPU path costs ≥1.1ms once a binary exists to spawn — never measured on real hardware | Re-measure on GPU hardware |

> **8.1 measures what a person waits for, not what the function costs.** The 2.7µs
> figure was `decide_placement` called in a loop — real, and answering a question the
> `<10ms` target was not asking. Spoken to over stdio MCP the way a client speaks to it,
> `hussh_burst_decide` returns in **2.3ms median, 4.7ms p95, 6.0ms worst of 30**, with the
> first call after start at 14ms and server boot plus handshake at 581ms once. `plan` and
> `device_status` sit at 2.4ms and 2.2ms. Measured on this container *while the full test
> suite ran four ways in parallel*, so they are a loaded machine's numbers, not an idle
> one's. The transport and the measurement dominate the decision by roughly 850×, which is
> the honest shape of it: the arithmetic was never going to be the cost.

> **8.5 had the same warm/cold flaw, found by looking for it rather than tripping over it.**
> The quoted 0.5ms is a warm loop. Measured across seven fresh interpreters: importing
> `telemetry` costs **36ms** once, the **first** `measure_device()` costs **13.5ms median**
> (psutil's own first-call setup), and only from the second does it settle to **0.40ms**. A
> person's first `hussh_burst_device_status` therefore costs roughly 50ms, not half a
> millisecond. The verdict holds — that is nowhere near 1% of a CPU — but the number was
> again measuring something the target was not asking about.
>
> The "GPU path shells out, untimed" gap is now **bounded rather than unknown**. It reads as
> free here (**0.031ms**) *because* `_run` calls `shutil.which` first and finds nothing, so
> no process is created. Once a binary exists the floor is a real spawn: **1.1ms** measured
> for `/bin/true` on this container, and `nvidia-smi` does considerably more than exit. Two
> tests now pin that a GPU-less machine spawns **nothing at all** — the existing test checked
> only the return value, so a rewrite that tried the spawn and caught `OSError` would return
> the same `None`, pass that test, and add a process launch to every measurement on every
> machine without an accelerator.

## 9. Production readiness

| KPI | Status | Target | Gap | Next action |
|---|---|---|---|---|
| 9.1 Reachable by a person | ✅ 5 MCP tools | 1 entry point | none | — |
| 9.2 Consent + audit receipts | ✅ approval exercised; receipts durable, **simulated ones marked** | every offload leaves a receipt | no *real* burst has written a ledger line — the one that ran predates the ledger | Check the ledger after the next real burst |
| 9.3 Secrets posture | ✅ key held one call, never persisted | never persisted | none | Re-audit at first real burst |
| 9.4 Ops runbook | ✅ **its snippets were run**, not just written | runbook + teardown drill | the `run` snippet costs money, so it is the one still unexercised as written | Re-run the checks after each real burst |
| 9.5 Privacy invariant | ✅ pinned token-by-token across a whole burst | holds every phase | none | Re-check when payload transfer (2.5) lands |

The privacy invariant got three new guards: measurement is isolated in one module,
reachability is inferred from local link state rather than by contacting a host, and
`InstanceSpec` carries no workload fields by construction — a test asserts its exact
field set.

**Overall: 83% of 46 KPIs met (33 met, 10 partial, 3 open).** Reachable, measured, safe to
stop, and now quoting hardware that can actually be bought. Not yet proven against a real
cloud.

---

## The host-metrics overlap — decision-ready

`main` landed `hermes_cli/hussh_one_host_metrics.py` while `hussh_one_burst/telemetry.py`
was being written. Both read the same machine. Measured on the same container:

| | `hussh_one_host_metrics` | `hussh_one_burst/telemetry` |
|---|---|---|
| CPU cores | ✅ `4` | ✅ `4` |
| RAM total / available | ✅ `16461028` / `14273264` KiB | ✅ `15.7` / `15.09` GB |
| Battery | ✅ `{"present": false}` | ⚠️ `battery_pct=None, on_ac_power=None` |
| Processor name | ✅ `Intel(R) Xeon(R) @ 2.10GHz` | ❌ |
| Swap, process RSS | ✅ | ❌ |
| GPU / VRAM / memory model | ❌ | ✅ |
| Disk free | ❌ | ✅ |
| CPU load, thermal, throttle | ❌ | ✅ |
| Reachability | ❌ | ✅ |
| Converts to a `DeviceProfile` | ❌ | ✅ |

They are **complementary far more than duplicative**, and the overlap is exactly the host
half: cores, memory, battery.

**Recommendation: `telemetry` delegates the host half to `host_metrics` and keeps the
accelerator, thermal and placement half.** Not merely to remove duplication — delegating
*fixes a real weakness*. On a machine with no battery, `host_metrics` returns
`{"present": false}` while `telemetry` returns `None`/`None`. Those are not the same
answer: one says "this machine has no battery", the other says "I could not tell". The
placement advisory that warns about draining a battery should never fire on a desktop, and
only the first shape makes that distinction reliable. `host_metrics` also reads Darwin
battery through `pmset`, which is better than the `agent.battery` fallback `telemetry`
currently uses.

**This is still a human's call**, because it makes burst depend on a module owned by
another workstream, and that coupling is a real cost. It is recorded here rather than
actioned, and deliberately not smuggled into a merge commit.

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
- [Operations runbook](../operations/xtreme-burst-runbook.md) — pre-flight, teardown proof, leak response
- [Architecture & migration record](./xtreme-burst.md)
- [husshone design record](../reference/xtreme-burst/README.md)
- [Changelog](../CHANGELOG.md)
