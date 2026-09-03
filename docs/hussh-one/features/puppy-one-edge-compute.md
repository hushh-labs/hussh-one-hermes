# Puppy One — on-device edge compute

Puppy One is the edge tier of Hussh One: the owner's PKM work answered by a model
running on hardware they already have, instead of rented from a vendor.

Two names, one thing, not interchangeable in code. **Puppy One** is the product
the owner sees; **Hermes** is the runtime. Identifiers stay `hermes_*` — renaming
one to match the brand breaks a config that already exists on the owner's machine
and buys nothing.

```text
owner utterance
  -> local model on LM Studio (loopback only)
  -> save_to_pkm tool call
  -> vault key + propose + fresh approval + commit
  -> encrypted PKM write
```

## Turning it on

```yaml
hussh_one:
  on_device_only: true
auxiliary:
  compression:
    provider: auto     # NOT a cloud provider, and not left pinned
    model: ""
```

`auto` matters more than the flag. With the gate on and an explicit cloud
provider, the task is **refused**, which means *skipped* rather than run
locally: the gate removes the work instead of relocating it. `auto` resolves
through step 1 of auto-route to the main provider, which is the local model, so
the work actually happens here.

Verify with `python3 -m hermes_cli.hussh_one_egress_audit`, which exits non-zero
while anything leaves.

**The cost is real and should be stated.** A compression call that a flash-class
cloud model answers in about a second took **20.2 s** on
`gemma-4-26b-a4b-qat`. On-device trades latency for the work never leaving the
machine. Sessions long enough to trigger compression will feel it.

## What actually enforces "on-device"

| Layer | Mechanism | Failure mode without it |
|---|---|---|
| Main turn | `model.provider: lmstudio`, explicit load mode | JIT load hides whether the model is resident |
| Auxiliary tasks | `hussh_one.on_device_only` fail-closed gate | `provider: auto` falls through to a paid Gemini |
| Bridge | Loopback-only by **parsed hostname** | `http://127.0.0.1.evil.example` passes a substring check |

The gate is the load-bearing one. Pinning the provider only ever covered the main
turn; compression fires exactly when a session accumulates reasoning, which is why
a PKM save appeared to think on Gemini while the config said otherwise.

## Measuring it

Two units, timed separately and **never summed**. They scale on different things,
and one blended number hides which half a regression landed in.

| Unit | What it covers | Scales with |
|---|---|---|
| `T_model` | The turn that emits a well-formed `save_to_pkm` call | Model size, quant, memory bandwidth |
| `T_commit` | Vault key, propose, approve, commit | Network and payload, not the model |

Validity is scored beside latency, because latency alone ranks a model that
answers in prose above one that does the work. A present-but-empty `scope_path`
counts as missing: it is the cheapest output available and must not win.

Measured on `Mac16,5 / Apple M4 Max / 128 GB`, 5 cases x 2 reps, warm p50:

| Model | Valid | Warm p50 | tok/s |
|---|---|---|---|
| `gemma-4-e2b` | **0%** | 2579 ms | 138.4 |
| `gemma-4-26b-a4b-qat` | **100%** | 2600 ms | 94.3 |
| `gemma-4-12b-qat` | 100% | 4847 ms | 49.8 |
| `qwen3.6-35b-a3b` | 100% (2 errors) | 8165 ms | 79.7 |
| `gemma-4-31b-qat` | 100% | 9289 ms | 22.4 |

Read the first two rows together: **the fastest model on the ladder produces zero
usable saves.** A latency-only benchmark would have recommended it.
`gemma-4-26b-a4b-qat` matches its speed to within 1% with everything usable,
because `a4b` means roughly 4B parameters active per token.

## Judging correctness, not just shape

Structural validity is cheap to satisfy while being wrong — a well-formed call can
still file a dietary restriction under `finance.accounts`. So a stronger model
grades the output against the rules the agent was given, and its verdicts
accumulate into a regression corpus.

Three rules, because a judge that rubber-stamps is worse than none: it
manufactures evidence.

1. **The judge may not be the answerer.** Same model on both sides refuses to run,
   before a call is spent. The existing `evals/compaction/runner.py` routes judge,
   answerer and question-generator through one `call_llm(task="compression")`.
2. **An adverse verdict must cite, and the citation is checked** against the graded
   output. Otherwise the judge invents the evidence for its own verdict.
3. **Planted controls decide whether the run counts.** Miss one and the run is
   void, publishing no accuracy at all — a number with a caveat gets quoted
   without the caveat.

First live run caught a real hallucination: asked *"I run the trust org now"*, the
model wrote the title `"Head of Trust Org"`. The structural benchmark had scored
that same output 100% valid.

## Memory and eviction

Reloading weights on an edge device is expensive, so eviction is conservative:
only `IDLE` models are candidates, `protect` matches case-insensitively, the plan
surrendering the **least** memory wins, and an impossible fit is refused outright
rather than unloading everything and still not fitting.

## Presence

Push on transitions with a 600s keepalive underneath, not a fixed poll — an idle
laptop should cost nothing. `post_heartbeat` reads `read_state` and deliberately
does **not** call `auth_headers`, which would refresh the token and run the
revocation check. Telemetry must never be able to seal the device as a side effect
of being sent.

The snapshot carries brand, processor, RAM and battery. Names only: brand
describes a machine, a serial number identifies one. A desktop reports **no
battery** rather than 0% — the same number and the opposite fact.

The snapshot **is** the request body: every field (`current_model`,
`agent_version`, `busy`, `active_sessions`, hardware, battery) sits at the top
level of `POST /api/account/trusted-devices/{device_id}/heartbeat`. Builds from
2026-08-28 to 2026-09-03 wrapped it under a `heartbeat` key, and the server of
that time dropped the wrapper, so One stamped `last_heartbeat_at` and stored
`heartbeat: null` on every push. The server now reads both shapes (top-level
keys win), so an un-updated install reports too; the flat body is canonical.
Text fields are capped at 120 characters on the device (`SERVER_TEXT_MAX`) to
match One's cap, so a long model id can never be the reason a beat is refused.

`current_model` is the **configured** pin (`model.default`), read inside the
snapshot lambda per push, together with `agent_version`; either reader failing
yields "" and the field is omitted, never a blocked heartbeat. Pushes happen on
unlock and on the keepalive, so a new pin reaches One on the next push, and the
devices page is showing the pin, not proof that the model is loaded.

## Upstream safety

This fork syncs from `NousResearch/hermes-agent` almost daily. Everything here
lives under the `hussh_one_` prefix, which is the fork's mechanism for surviving
that, not a naming preference.

| Path | Sync risk |
|---|---|
| `hermes_cli/hussh_one_pkm/*` | None — fork-owned package |
| `hermes_cli/hussh_one_lmstudio.py` | None — namespaced |
| `hermes_cli/hussh_one_host_metrics.py` | None — namespaced |
| `agent/auxiliary_client.py` | **Will conflict.** Unavoidable: the gate must sit where providers resolve |
| `hermes_cli/web_server.py` | **Will conflict.** 14-line hardware block on `/api/system/stats` |

Keep the two exceptions small and obvious, so a sync conflict is a two-minute read
rather than an investigation.

## The resource monitor

`GET /api/hussh-one/resources` on the loopback API server (bearer-authed, same
key as every other route there) answers the four questions an owner of an edge
machine actually has. It is not a CPU graph, and each question has been a real
incident here:

| Question | What it reports | Why it earns the space |
| --- | --- | --- |
| Is the answer generated here? | `model`, `provider`, `on_device`, `on_device_gate` | The pin covers the main turn; the gate is what stops an auxiliary task reaching a vendor. Collapsing the two is how a PKM save came to think on Gemini while the config said otherwise. |
| Is there room? | resident models with size, `resident_gb`, `available_gb` | Models are tens of gigabytes and eviction is conservative, so this is the number that predicts whether the next load fits or drives the host into swap. `available_gb` comes from the same conservative source eviction uses. |
| Will it survive tonight? | `ram_used_pct`, disk free and used percent, battery | A laptop at 27% and discharging runs the same jobs and fails them by thermal throttling rather than by any traceable error. The vault, the replica and every session live on the disk. |
| Is the work landing? | enabled and disabled job counts, next run, last 24h outcomes | The scheduled jobs are the product. Disabled jobs are counted, not hidden: the owner disables them on purpose and a monitor that drops them cannot explain why something it used to show no longer runs. |

Every probe is bounded and independently fallible, and a section that cannot be
answered is **omitted**. A machine with no battery is `{"present": false}` with
no percentage at all, exactly as the heartbeat allow-list does it: a zero would
read as a measurement, and nothing downstream could tell a desktop from a
laptop about to die. The probes shell out (`lms ps`, `pmset`, `sysctl`) and read
sqlite, so the handler runs them off the event loop.

## What is deliberately NOT here

`gateway/memory_status.py` is not wired to real host memory. `/api/status` is an
unauthenticated public endpoint, so publishing live host memory there would leak
machine detail to any caller. The resources endpoint above is the authenticated
answer to the same question, which is why it is not on the public route.

The 22-agent hierarchy in consent-protocol is **still Gemini-bound**. The manifests
already declare a provider and the code ignores it, calling
`build_managed_runtime_client("gemini")` as one shared client. Moving the hierarchy
on-device needs a provider abstraction, not a config change. Say this plainly in
any status: the stack is not end-to-end on-device.
