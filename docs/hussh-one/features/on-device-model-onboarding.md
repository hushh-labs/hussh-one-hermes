# Onboarding an on-device model

The durable deliverable is this procedure, not any one model comparison.
Sovereign and local models improve monthly; picking today's winner is worth
little if answering the same question in three months means rebuilding the
apparatus. Run this against any model id and it ends in a routing decision
backed by measurements that stay comparable across generations.

Two incidents forced it. A local model wrote a Python-style `#` comment into
`bridge_helpers.js` and took WhatsApp down for about 42 hours. And asked to
resolve one real merge conflict, `gemma-4-26b-a4b-qat` chose the right side
semantically and still produced structurally invalid output. Neither needed a
judge to catch. Both needed a harness that was looking.

## The one rule

**Measure it on this machine. Do not read it, do not infer it from the name.**

Every claim below was wrong at least once when it was assumed:

| Assumed | Measured |
| --- | --- |
| `nemotron-3-nano-omni` needs ~8 GB (parsed from its name) | 26.10 GB, a 3x under-estimate that makes eviction clear far too little |
| `gemma-4-26b-a4b-qat` is a 26B dense model | MoE, 128 experts, top-8, about 4B active |
| `arch` distinguishes the models | LM Studio reports `gemma4` for both the dense 31b and the MoE 26b-a4b |
| `reasoning_effort` bounds reasoning | Inert on this build. See below |
| `tool_choice: "required"` forces a tool call | Accepted and ignored; prose comes back with `finish_reason: "stop"` |
| The machine has ~64 GB | 137.4 GB total, 68.7 GB available |

Architecture comes from `expert_count` (GGUF) or `num_experts` (MLX), never from
the model id.

### The context window must be pinned in LM Studio's config, not only in the harness

Found 2026-09-01 on a founder prompt: the exam and the product were loading the
same models at different context windows. The exam path pins explicitly
(`lms load <model> -c <ctx> -y`, then reads `loaded_context_length` back from
`/api/v0/models` and refuses a clamped load), and live queries confirmed
262144. But the gateway's cold-load path (`ensure_lmstudio_model_loaded`)
deliberately omits `context_length` unless a caller passes one, and no caller
does (`run_agent.py` reads `_config_context_length`, which nothing sets). A
cold load therefore inherits LM Studio's per-model default config at
`~/.lmstudio/.internal/user-concrete-model-default-config/`, and those
defaults said: MoE 64,000, qwen 128,000, `gemma-4-12b` no file at all (global
default). Every exam number would have described a window the product never
served.

The fix is in LM Studio's own config, per the founder's call, so every load
path agrees without a code-side lock: all three candidate models' default
`llm.load.contextLength` now reads 262144, equal to the tested window and to
the server-reported `max_context_length`. Two standing rules fall out: **a
model's LM Studio default context must equal the context it was examined at**,
and **any load that matters must read the context back from the server**
rather than trusting the number it asked for.

**Correction of the correction (superseded 2026-09-02; the paragraph that
used to sit here claimed the two shipping models were "immune to the lever"
by an MLX rope-scaling limitation -- that was wrong):** the real mechanism is
session-stickiness in LM Studio, documented under "restart_app" in
`host.py`: a model already loaded once at some context inside one running LM
Studio process cannot be moved to another context by any flag or config file
until the app restarts, and the earlier "impossible" tests were all
contaminated by that. Verified the other way round afterwards: the `lmstudio`
Python SDK loaded the MoE at 32,768 and then at 262,144 on demand, read back
from the server. The 96k-vs-128k question is still best treated as a
prompt-side compaction-trigger question (next section), but for the honest
reason -- the corpus never exceeds 96k -- not because a smaller window is
unreachable.

## Reasoning levels

This is the part that most recently cost a run, so it comes before the ladder.

**`reasoning_effort` does nothing on LM Studio's `/v1/chat/completions`.**
Measured on this build, same model, same prompt:

| Setting | `gemma-4-26b-a4b-qat` | `gemma-4-31b-qat` (dense) |
| --- | --- | --- |
| `reasoning_effort: none` | 1484 | 345 |
| `reasoning_effort: low` | 1484 | 345 |
| `reasoning_effort: minimal` | 1484 | 345 |
| `reasoning_effort: high` | 1484 | 345 |
| `chat_template_kwargs: {enable_thinking: false}` | 1484 | 345 |
| system prompt, no `<\|think\|>` token | 2468 | 586 |
| `<\|think\|>` + "use minimal reasoning" | 1617 | 257 |

Byte-identical across every API-side control. LM Studio documents
`reasoning.effort` only for `/v1/responses` on one specific model, and its bug
tracker carries an open report that the parameter is ignored on
`/v1/chat/completions` while the GUI setting under Inference > Custom Fields
wins. `lms load` exposes no reasoning option either.

Three consequences, all of which the harness now encodes:

1. **A zero reasoning count is not suppression.** It means that prompt did not
   happen to reason. `profile.reasoning_effort_honored` measures the difference
   by sending two identical calls that differ only in effort.
2. **The budget absorbs reasoning; it cannot turn it down.** `max_tokens` is the
   only real bound. When the knob is inert the recommended budget takes a floor
   set from the worst measured case.
3. **Adding a system prompt increases reasoning.** On both models the plain
   system prompt roughly doubled it. Only the Gemma `<|think|>` token with an
   explicit brevity instruction reduced it, and that token is Gemma-specific, so
   it belongs in the per-model profile and never in a shared suite prompt.

Google's published budget guidance, for a starting point that then gets
measured: 0 for factual recall, 256-512 for code generation, 1024-2048 for math
and logic, 2048-4096 for architecture decisions. On this fleet the observed
merge-conflict cost was far higher than the code-generation guidance, which is
the reason it is measured per model rather than taken from the table.

## First execution: the merge suite, 2026-08-30

Five models, 20 real merge conflicts recovered from this repo's own history, all
pinned at **262144 context** (comparability confirmed: one context across every
rung), 12000-token budget, drained to empty between rungs.

| Model | Arch | Format | Usable / 20 | Truncated | Ref match | Median |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen/qwen3.8-27b` | dense 27B | mlx 4bit | **12** | **0** | **7** | **24s** |
| `gemma-4-31b-qat` | dense 31B | gguf Q4_0 | 10 | **0** | **7** | 104s |
| `gemma-4-26b-a4b-qat` | **MoE**, ~4B active | mlx 4bit | 6 | 9 | 4 | 65s |
| `gemma-4-12b` | dense 12B | mlx 8bit | 6 | 3 | 4 | 113s |
| `gemma-4-12b-qat` | dense 12B | gguf Q4_0 | 1 | **16** | 1 | 261s |

"Usable" is `deterministically_ok` out of the 20 offered: markers gone, splices
back and parses, nothing duplicated.

> **Read the truncation column with care; it is not what it looked like.**
> This run counted a truncated turn as unusable. Hermes compacts and continues
> when a turn hits `max_tokens`, so in a real loop that is a normal event rather
> than a failure, and a one-shot probe simply cannot see the continuation. The
> MoE's 9 and the 12b-qat's 16 are **compaction events**, not evidence those
> models cannot do the work. `summarize()` now reports `compacted` apart from
> `timed_out` for exactly this reason. Treat the ranking below as provisional
> for every row with a non-zero truncation count.

**Provisional routing for the `merge` suite: `qwen/qwen3.8-27b`.** It leads on
validity, ties the 31b on reference match, truncated zero times so its number is
unaffected by the caveat above, and is 4.3x faster than the 31b.

Three findings, one of them since revised:

1. ~~Dense models did not truncate; the MoE did, on 9 of 20.~~ **Revised.** The
   dense/MoE truncation gap is real but does not mean what this originally
   claimed. Truncation is compaction, so the gap says the MoE writes more before
   yielding, not that it fails more often.
2. **Quantization mattered more than parameter count.** Same 12B dense
   architecture: the 8-bit MLX build produced 6 usable answers with a mean of
   4875 reasoning tokens, while the Q4_0 QAT build produced 1 and averaged 9982
   reasoning tokens against a 12000 cap. Confounded with the runtime (mlx vs
   gguf) and not separated here, so treat it as "this build" rather than
   "4-bit".
3. **No model is good enough to merge unsupervised.** `broken-structure` ran at
   30-40% across the whole ladder, and that is exactly the failure class that
   took the WhatsApp bridge down for 42 hours. The routing answer names the
   least-bad model, not a safe one.

## The exam that matters: replayed session turns

The merge suite grades one upstream chore. `hermes puppy replay` asks the
question the product asks, on the owner's own history: the agent's real
conversation cut just before it acted, the real tool catalog, and *what do you
do next*.

400 such cases exist across 22 sessions, sampled round-robin so one long session
cannot crowd out the rest. The next-action mix matches the measured workload:
terminal 210, read_file 49, search_files 44, execute_code 48. Median prompt is
about 71k tokens, so long-context behaviour is measured rather than simulated.

**Two numbers, reported apart and never added.** *Agreement* is whether the
model picked the tool a frontier model picked, which is imitation fidelity and
not correctness. *Structural* is whether the output would have worked, which
does not depend on the reference at all.

First result, `gemma-4-26b-a4b-qat`, 25 turns:

| | rate |
| --- | --- |
| Agreement | 0.50 |
| Structural | 0.875 |
| Shell commands that parsed | 14 / 14 |
| Argument sets validating against schema | 23 / 23 |
| Invented parameters | 0 |
| Unrequested destructive commands | 0 |

The shape of that result is the product argument. **The model is wrong about
what to do roughly half the time and almost never wrong about how to say it**,
and where it is, a deterministic gate catches it. A parser can be pushed toward
certainty because it is a parser; a model cannot. So the claim that survives
contact with a customer is *Puppy One never ships broken output*, not *the model
is always right*.

## Model selection, corrected 2026-09-01: fair conditions, honest overlap

The founder challenged the earlier qwen result and the audit proved the
challenge right: the exam had been stripping every tool description (qwen
depends on them; gemma does not), sending qwen a live think-less instruction
through the `reasoning_effort` template variable while gemma received a
think-more token, over-triggering the bounded-scan oracle, and grading qwen on
a wall-cap-truncated 31-of-45 case set. All four defects are fixed; the
corrected run below uses identical cases, real descriptions, and per-family
maximum thinking (`xhigh` for qwen, `<|think|>` for gemma).

| Model | Structural | 95% CI | Agreement | Median | Reasoning mean |
| --- | --- | --- | --- | --- | --- |
| `gemma-4-26b-a4b-qat` (MoE) | 0.976 | [0.874, 0.996] | 0.512 | 41.5s | 435 |
| `gemma-4-12b` (8-bit) | 0.929 | [0.810, 0.975] | 0.595 | 96.5s | 205 |
| `qwen/qwen3.8-27b` | 0.810 | [0.667, 0.900] | 0.524 | 152.5s | 473 |

What the correction changed, and what it did not:

- **qwen's structural score rose from the unfair 0.714 to 0.810**, and its
  remaining structural failures are a single oracle: `paths_grounded`, eight
  times. Under fair conditions its failure mode is specific (referencing paths
  not established in the conversation), not general brokenness.
- **No pair of models is separated at 95% on structural validity.** On roughly
  42 graded cases each, the honest statement is that the three cannot be ranked
  on structural validity, and the report says so instead of inventing an order.
- **Latency and agreement still tier them.** The MoE is decisively fastest
  (41.5s median); the 12b leads agreement (0.595). qwen leads no column in this
  table and is slowest, so the two-tier gemma recommendation stands, now for
  stated reasons that survive the audit rather than a ranking that did not.
  (qwen does lead the judged goal-progress number in the next section.)

The prior section is retained below as the record of what was published and why
it was wrong.

## The third number, 2026-08-31: goal progress, judged

Structural validity and agreement are proxies. The founder's exact critique
("were the tests goal oriented, validating that a goal was really achieved")
named what neither measures: whether the chosen action advanced the goal the
user actually had. The `goal_progress` suite closes that gap. Every fair-run
turn from all three models went into one blinded queue: request tail, the
frontier continuation labelled "NOT ground truth", the model's action with its
prose, no model names anywhere. Grading went through the review-queue
discipline: planted swapped-action controls, byte-equal positive controls, a
closed five-rule vocabulary (wrong-object, dead-end, redundant,
destructive-detour, stalls), citations verified verbatim at write time,
`unsure` counted against, seal and identity map held outside the run
directory.

The first grading attempt VOIDED, and the void is part of the record: the
judge (a frontier session) passed two planted swaps. One was a fair control
missed through inconsistent leniency (the identical action had been ruled
wrong on another request). One was a builder defect: a same-domain donor (a
`skill_view` of a skill the request itself listed, planted on the
skills-curation request) is on-path by semantics, the c006 class of unwinnable
control one level up. The builder now refuses same-family and shared-entity
donors and picks control bases by a content-seeded shuffle, with regression
tests; the regrade carried every per-row content judgment, flipped the one
recalibrated row, and passed all six controls.

| Model | Goal progress | 95% CI | Off-path rules |
| --- | --- | --- | --- |
| `qwen/qwen3.8-27b` | 0.929 (39/42) | [0.810, 0.975] | 2 wrong-object, 1 dead-end |
| `gemma-4-12b` (8-bit) | 0.905 (38/42) | [0.779, 0.962] | 2 dead-end, 1 stalls, 1 wrong-object |
| `gemma-4-26b-a4b-qat` (MoE) | 0.854 (35/41) | [0.716, 0.931] | 3 wrong-object, 1 stalls, 1 dead-end, 1 redundant |

No pair is separated at 95%, the intervals overlap heavily, and this number is
never added to the other two. What it reframes even so:

- **The goal ranking inverts the structural ranking.** qwen, last on
  structural validity, is first on judged goal progress; the MoE, structurally
  pristine, wanders off-goal most. A model can format perfect tool calls at
  the wrong object, and a model can cite an unestablished path while driving
  at exactly the right goal.
- **qwen's deficit is the teachable kind.** Its structural failures are one
  rule (`paths_grounded`: cite only what the context established), which is
  precisely what a playbook loop teaches. Its goal comprehension, the hard
  thing to teach, leads. The MoE's commonest fault (acting on the wrong object
  under a clear request) is the hard kind.
- **The off-path modes are few and named.** Across 125 graded rows there were
  13 off-path actions in four modes. The commonest was answering a
  service-connectivity question with git repo metadata (five of the six
  wrong-object verdicts); the starkest was asking the user to provide a file
  the context said to search for (stalls).

Same-session disclosure: the queue author and the judge were the same session,
so the contract's independent-judge requirement is not met by this run, and
after a void names control ids a rebuild's control pass is wiring proof, not
an independent diligence test. The rates measure per-row content judgment of
125 real rows. Independent confirmation is a fresh judge session on a fresh
rebuild, and the machinery for that now exists.

## Final model pick and the context-budget question, 2026-09-01

After the goal-progress result the founder made the call: **ship two models,
`gemma-4-26b-a4b-qat` (MoE) and `qwen/qwen3.8-27b` (dense). `gemma-4-12b` is
dropped.** It led only on agreement, the weakest of the three signals by this
harness's own design, and led nothing once goal progress existed as a number.

The founder then asked the harder question directly: find the right context
budget for these two models to "function at scale fully", somewhere between
96k and 128k, informed by what the community recommends, set through LM
Studio's config rather than a code-side lock. Two things had to be
established before that question could even be answered, and both changed the
shape of the answer.

**First, load-time context is not a lever on these two models at all** -- see
the correction above. Both are pinned to 262144 by the engine itself,
immune to the CLI flag, the config file, and their combination. There is no
"load qwen at 96k" to test. Whatever the right budget is, it has to be
enforced on the prompt, not the load.

**Second, community research says quality tracks well under the advertised
maximum, and our own real usage never gets close to either number being
debated.** The RULER benchmark and Chroma's 2025 "context rot" study (18
frontier models tested) both establish that reliable quality tracks roughly
50-70% of a model's *advertised* ceiling, not the ceiling itself -- degradation
starts well before the hard limit, and a 200k-window model can already be
rotting by 50k. Applied as a generic heuristic to a 262144-max model, that
puts the "effective" zone at roughly 130k-183k tokens; this is a general
finding across other model families, not a number measured on these exact two.
Separately, on real NIAH-style long-context evals the Qwen3 family holds up
more stably from 8k to 128k than the Gemma 3 family, which shows more visible
degradation over the same range -- a real analog, not a claim about the exact
Gemma 4 / Qwen 3.8 curves, which are this harness's own data to produce.

Against that, the **actual replay corpus says the debate is currently moot**:
of the 45 real session turns used throughout this selection, every single one
already fits under 96k (max observed prompt: 95,205 tokens; median 51,012;
p90 64,504). 96k and 128k are numerically indistinguishable on this corpus --
neither would ever trigger a truncation, so a same-corpus A/B between them
would trigger zero and report a manufactured "no difference" that reflects
the corpus, not the models. Running one would burn real inference time to
learn nothing; it is not proposed for that reason.

**What the evidence supports doing:** treat 96k-128k as a **compaction
trigger threshold**, not a load parameter -- the point past which the harness
switches from "send everything" to a recency-window or summarising strategy,
so a session that keeps growing (the agentic, long-running case Puppy One is
built for) never has to reach the rot zone in the first place, on a model
that cannot be loaded smaller to protect itself. The recency-window mechanics
already exist (`arm64k.py` in the working scratchpad, built and run once
against a 64k budget before being redirected toward the direct-reload
question above) and generalise to any threshold. Testing 96k vs 128k as that
trigger is honestly **not yet answerable** with this corpus -- it needs
either naturally longer real sessions as they accumulate, or deliberately
constructed long-horizon cases, since a threshold neither real session has
ever reached cannot be shown to matter by measurement, only asserted.

One more lever surfaced but not pulled: the LM Studio / MLX default here runs
an unquantized KV cache (`kvCacheQuantization.enabled: false` in both
models' configs). Community guidance for Apple Silicon treats 8-bit KV cache
as the standard default, roughly doubling the usable budget at fixed memory.
Turning it on is a legitimate way to make more of the native 262144 usable
without changing anything about the load-context question above, and would
need its own measurement before being adopted, since it is a quality/memory
trade, not a free win.

## Superseded: the 2026-08-31 selection and its defects



45 replayed session turns per model (median 51,012 tokens, max 95,205), drained
between rungs, pinned at 262144, budget 16000, timeout 900s.

| Model | Structural | 95% CI | Agreement | 95% CI | Median |
| --- | --- | --- | --- | --- | --- |
| `gemma-4-12b` (8-bit MLX) | **0.952** | [0.842, 0.987] | **0.619** | [0.468, 0.750] | 87.2s |
| `gemma-4-26b-a4b-qat` (MoE) | **0.952** | [0.842, 0.987] | 0.452 | [0.312, 0.601] | **35.1s** |
| `qwen/qwen3.8-27b` (dense 27B) | 0.714 | [0.529, 0.848] | 0.464 | [0.295, 0.642] | 101.9s |

**Zero timeouts and zero truncations for all three models.** The 7 indeterminate
turns in the earlier 50-case run were the harness clock at exactly 420.0s, and
they are gone at a 900s timeout. The budget was never the constraint.

### The recommendation, and how it changed

**`qwen/qwen3.8-27b` is out**, and this page previously recommended it. It came
last on structural validity in **both** runs, on different case samples of
different difficulty, while also being the slowest. Its merge-suite win did not
generalise to the work Hermes actually does.

**Accuracy tier: `gemma-4-12b` (8-bit MLX).** Ties for best structural validity
and leads clearly on agreement.

**Speed tier: `gemma-4-26b-a4b-qat` (MoE).** Identical structural validity and
**2.5x faster**.

### What separates them, honestly

- **Structural: identical.** 0.952 both. Not a tiebreak in either direction.
- **Agreement: the 12b leads by 17 points**, but the intervals overlap
  ([0.468, 0.750] against [0.312, 0.601]), so at 95% this is a lean and not a
  separation. It is the strongest signal available and it is not conclusive.
- **Latency: decisive.** 35.1s against 87.2s is far outside run-to-run noise.

So the tiering rests on a decisive latency difference plus a non-significant
agreement lean. That is enough to choose two tiers; it is not enough to claim
one model is better than the other overall, and the report says so.

### A sampling flaw in the first attempt, and why it is recorded

The first run of this comparison sorted the case list so an earlier phase could
select large prompts, then took the first 30 — which were the **30 smallest**.
Median 25,365 tokens against a representative 71,000: the easiest third of the
corpus, scored as though it were the corpus. The within-run comparison was still
fair, since all three models got identical cases, but every absolute number was
optimistic.

It is recorded because the flaw was in the orchestration rather than in the
harness, and because the corrected run is the reason the result can be trusted:
the ranking **replicated on harder cases**, which is much stronger evidence than
either run alone.

## First judged learning round, 2026-08-31: the machinery works, the effect is unresolved

One full round with a frontier session as the reflector, on the speed-tier
model (`gemma-4-26b-a4b-qat`), over 36 real session turns (18 train, 18 held
out): generate, verify with deterministic oracles, judge the failures, curate
tactics, re-measure held out.

The judge reviewed 12 evidence items and wrote **5 tactics, refusing 2 misses
as defensible alternatives** (git state via terminal; skill_view versus a
directory listing). That refusal is the step a deterministic oracle-to-tactic
lookup cannot perform, and the reason the reflector must read the pairing of
case and diagnosis rather than the oracle name: a shuffled-evidence control is
degenerate by construction against a lookup reflector.

The strongest pattern in the evidence: **skills-first**. Four of eleven
agreement misses were board, governance, or PKM tasks where the reference
consulted the matching skill and the small model went to session search, file
search, web search, or nothing.

Result on the 18 held-out turns:

| signal | before | after | delta |
| --- | --- | --- | --- |
| structural | 0.778 | 0.833 | +0.056 (one case) |
| agreement | 0.563 | 0.438 | -0.125 (two cases) |

**No learning claim is made from this.** At n=18 both deltas are inside the
noise a single flipped case produces, and the two signals moved in opposite
directions. What the round establishes is that the machinery runs end to end
honestly; what it does not establish is that the playbook helps. Two follow-ups
are implied: a held-out split near 100 cases so a real effect can be resolved,
and the context-tax hypothesis, because the playbook adds ~1,400 characters of
instructions and a measured property of these models is that a plain system
prompt alone inflates reasoning by ~50%. The tactics were deliberately not
persisted to the live playbook pending a resolvable measurement, and the
`replay` suite is not among the suites the live plugin injects.

## A latency measurement that turned out to be the prompt cache

An earlier diagnostic appeared to show a 10.8x speedup from raising `max_tokens`
alone (405.0s to 37.5s, byte-identical output). It was wrong. The large budget
had run *second* on every prompt, so a warm prompt cache was perfectly
confounded with the larger budget.

Controlled, with each budget running cold on a fresh prompt:

```
small budget, cold: mean  54.1s
large budget, cold: mean 101.0s
```

**The budget does not drive latency; the earlier gap was the cache.** Latency on
this fleet is real, and `max_tokens` is not the lever. Two arms of two runs each
with high within-arm variance is enough to kill the 10.8x claim and not enough
to assert the larger budget is slower.

## The earlier latency note, superseded

On `gemma-4-31b-qat`, the same prompt with a larger output budget returned
**byte-identical output** (551 reasoning tokens, 26 answer tokens) in a fraction
of the time:

| prompt tokens | `max_tokens` | elapsed |
| --- | --- | --- |
| 51,012 | 12,000 | 405.0s |
| 51,012 | 40,000 | 37.5s |

A 10.8x gap with byte-identical output looks like a decisive finding and was an
artifact of running the two arms in a fixed order, with the large budget always
second on a warm cache. Kept here because the retraction is the useful part: this
harness already refuses to compare models at different context lengths, and the
same discipline was missing one level up, in the order the arms themselves ran.

Also confirmed and now fixed: 7 of 50 turns in the first 50-case replay were
recorded as indeterminate at exactly 420.0s, the harness timeout to the decimal.
Those were **timeouts, not truncations**, and were reported as a budget problem
for a while before anyone checked. At a 900s timeout they are gone entirely.

## The procedure

### 1. Size it from the host

`catalog_sizes()` reads `lms ls`. The estimator that parses parameter counts out
of the model id is a fallback for a model that is not on disk yet, never the
primary source.

### 2. Profile it

`probe_capabilities(model)` measures, per feature *combination*:

- tool calling, by inspecting what came back rather than whether it errored
- structured output
- reasoning suppression alone, and again combined with `json_schema`, because
  the combination is where it breaks (0 tokens alone, 241 with a schema, same
  model, same instruction)
- whether `reasoning_effort` is honoured at all

Unknown parameters are dropped with HTTP 200 on this server, so no probe can ask
"did this error". Every one inspects the response.

The profile is the comparability key. Two runs whose profiles differ were not
asked the same question.

### 3. Walk the ladder

`walk()` drains to empty before every rung and **verifies the drain**, because
`unload_model` returning True means the request was accepted, not that the
weights are gone. A rung that cannot be drained is skipped rather than measured
on a dirty machine.

Order is counterbalanced per rep. A fixed order means one model is always
measured cold and another always after forty minutes under load, and that
difference is indistinguishable from a difference between the models.

Pre-load available memory is recorded per rung. When the spread exceeds
tolerance the run is declared not comparable rather than ranked with a caveat,
because the caveat is the part that gets dropped when the number is quoted.

### 4. Grade per suite, never averaged

A mean across suites hides "great at tool calls, unusable for code".

| Suite | Primary grade | What it cannot catch |
| --- | --- | --- |
| `merge` | four deterministic checks: markers gone, splices and parses, no duplicated context, side matches the reference | a semantically wrong merge that still parses; that subset goes to the judge |
| `code_edit` | on-disk oracle, including `.count(...) == 1` for duplication | whether the edit is *good*, only whether it satisfies the assertion |
| `pkm` | the existing `score_tool_call` | already built and already measured |

Rule vocabularies are scoped per suite. A merge judge citing `kept-wrong-side`
inside a PKM run is still rejected, and a genuinely invented rule is still caught
everywhere.

### 5. Route

Rank by validity rate first, latency second. Latency never buys a ranking:
`gemma-4-e2b` was fastest on the PKM ladder and produced zero usable saves.

The output lands as per-task config, which Hermes already supports through
`auxiliary.<task>.provider/model`. No new plumbing.

## Reporting rules

State these or the number misleads:

- **Truncation is indeterminate, not wrong.** `finish_reason: "length"` means the
  budget ran out mid-answer. Counting it against correctness reports a harness
  under-budget as a model failure. This has already happened once: at a 1600
  budget, `gemma-4-26b-a4b-qat` returned truncated on 12 merge cases out of 12.
- **The reference is *a* correct answer, not *the* correct answer.** A model that
  resolves differently and correctly scores as a miss until the judge rescues it.
  `reference_match` and judge results are two numbers and are never added.
- **The corpus states its own blind spots.** The merge corpus carries no
  keep-ours case, so a model that silently discards fork behaviour scores clean
  on it, and for an upstream sync that is the expensive failure.
- **Name what was dropped.** Top-N, sampling and no-retry all read as "covered
  everything" unless the count of what was skipped is printed beside the result.
- **The auditor is never local.** `assert_auditor_is_not_local` must be wired
  into the driver. A model grading its own class of output is not a check.

## Adding a model to the ladder

1. Confirm it is on disk and read its measured size.
2. Read `expert_count` / `num_experts` and record dense or MoE.
3. Pin the variant if more than one is on disk (`qwen/qwen3.8-27b` has two).
4. Run `probe_capabilities`, and read `reasoning_effort_honored` before setting
   any budget by hand.
5. Add it to the ladder and run at least two reps so run-to-run variance is
   measurable. If the top two models are within noise, report the suite as
   inconclusive rather than ranking them.

## Independent judge, 2026-09-02: the contract met, and the number to quote

Every earlier goal-progress round disclosed the same limitation: the session
that wrote the queue also graded it. This round closes that. A fresh sealed
queue (both shipping models, 89 rows, 6 planted controls) was graded by a
separate Claude Fable 5.1 session that never opened the seal or the identity
map; the orchestrator ingested afterwards.

| Model | Goal progress | 95% CI | n | Off-path rules |
| --- | --- | --- | --- | --- |
| `google/gemma-4-26b-a4b-qat` (MLX) | 0.927 | [0.806, 0.975] | 41 | 1 stalls, 1 dead-end, 1 redundant |
| `qwen/qwen3.8-27b` | 0.952 | [0.842, 0.987] | 42 | 2 dead-end |

Valid run: all six controls caught, zero `unsure`, zero writer rejections.
No pair separated. The independent judge marked 9 rows off-path where the
same-session rounds marked 17 on identical content; the recurring modes are
the same ones (answering "is hermes connected" with git repo metadata,
writing the request itself into memory, asking the user for an audio file
the context said to search for). This is the goal-progress number to quote;
the earlier same-session rounds remain in the record as history.

## The edge that was missing, 2026-09-02: the learning half never reached the model

Auditing "does the harness work for the model" end to end found that it did
not, for three independent reasons, none of them visible from any test:

1. **The playbook plugin was never switched on.** Plugins are opt-in on this
   fork (`plugins.enabled` in `~/.hermes/config.yaml`), and `puppy-playbook`
   was not in the list. `hermes plugins list` showed it as `not enabled`, and
   the gateway log had never mentioned it. Every tactic the loop ever wrote
   was unread. Fixed on the founder's machine (config allow-list) and
   recorded here so the next machine's bootstrap checks it: `hermes plugins
   list | grep puppy-playbook` must say `enabled`.
2. **The replay suite was not a live suite.** The plugin injected
   `file_edit`, `terminal` and `tool_select` playbooks and ignored `replay`,
   the one suite graded on the owner's real turns and the only one the
   learning loop runs now. `replay` is now first in `LIVE_SUITES`.
3. **The independent judge's verdicts went nowhere.** `learnable_failures`
   admitted structural failures only, correctly refusing agreement misses.
   But an off-path verdict from a blinded, control-checked judge, with a rule
   and a citation quoting the model's own output, is truth about that turn,
   not imitation. `hermes puppy goal-progress report` now writes those rows
   to `~/.hermes/puppy-playbooks/<model slug>/judged_failures.jsonl`
   (appended, keyed on case id + rule, never in the repository because the
   citations quote sessions), and `hermes puppy loop --run` attaches them to
   their cases as `judge:<rule>` evidence for the reflector. The shuffled
   control receives the same rows detached from their cases, so the negative
   control stays fair. Judged verdicts are not re-measured inside a round (a
   round has no judge); their effect shows in the next goal-progress round.

Also from the same audit: `goal-progress report` now appends one row per
model to the evolution ledger (`~/.hermes/evolution-ledger.jsonl`) in the
`judge_queue` row shape, probe mode `goal_progress/replay/blinded-judge/v1`,
so `compare_runs` can say whether the next model generation moved the judged
number. Until this change no goal-progress rate had ever been recorded
anywhere a later run could compare against. The 2026-09-02 independent round
above is the first two rows, and its five judged off-path turns (three for
the MoE, two for qwen) are the first judged evidence on file.

## The corpus must be frozen, 2026-09-02: the exam read a directory that changes

The first replay-suite learning pair with a frontier reflector (Fable 5.1
through a file handoff) produced a matched arm with 19 train / 26 held out and
a control arm with 26 train / 19 held out. The split is hashed on case ids and
cannot flip like that on one case set, so the two arms had run on two
different sets. Two causes, both invisible to every test:

- the exam extracts from the live `~/.hermes/sessions` directory, and one
  dump that existed at the first launch was gone at the second;
- the cron case-id fix (`case_prefix`, 26-character cron prefixes instead of
  the colliding 7) landed between the two launches and renumbered every cron
  case, moving them across the hash split.

The matched arm's numbers from that pair (held-out structural 0.846 to 0.885,
two accepted tactics about grounding paths) are therefore a pilot, not a
result; the tactics themselves were kept because they are correct advice,
but no learning claim rests on them.

What changed so it cannot recur: `hermes puppy freeze` copies every request
dump to `$HERMES_HOME/puppy-corpus/<date>/` with a manifest, `replay` and
`loop` take `--dumps <dir>` to read that copy instead of the live directory,
and a round result now records `train_ids`, `held_out_ids` and `corpus`, so a
comparison whose arms disagree on those lists is visibly void.

### Only active sessions count

The rerun's baseline then offered exactly two learnable failures, both an
ungrounded `/pr-train` path, both from cron sessions of the two PR-train jobs
the founder had disabled on purpose. A disabled job's turns are not goals for
the model, and teaching the model to run a workflow nobody runs is the
opposite of the harness's job. The policy is now code
(`build.session_is_active`): interactive sessions always count, a cron
session only while its job is enabled in `~/.hermes/cron/jobs.json` (a
missing jobs file means no cron session counts). It applies in three places:
`freeze` leaves those dumps out and records the active set in the manifest
(so a frozen corpus reads the same way later, whatever happens to the jobs
file), `extract_cases` skips them when reading live, and `goal-progress
report` drops their rows from the rate (probe mode bumped to
`goal_progress/replay/blinded-judge/active-sessions/v2`; the v1 rows in the
ledger counted every graded row and are not comparable). Both arms of the
learning pair were rerun on the active-only frozen corpus.

## Learning rounds on the active corpus, 2026-09-02: what the loop can and cannot see

Three replay-suite rounds ran on the frozen active-only corpus (45 cases,
28 train / 17 held out, identical ids in every arm, empty playbook at the
start of every arm, Fable 5.1 as reflector through a file handoff, the
model at a verified 262,144).

| Round | Evidence offered to the reflector | Matched arm (held-out structural) | Control arm |
| --- | --- | --- | --- |
| structural only | 1 failure (`arguments_valid`, a missing required `pattern`) | 0.824 to 0.765, 2 tactics | degenerate: one failure, nothing to shuffle, refused to rule |
| structural + judged | the same failure plus 2 independently judged off-path verdicts (a stall, a dead end) | 0.824 to 0.824, 3 tactics | non-degenerate, 0.824 to 0.824, 3 tactics |

Two conclusions, neither of them a learning claim. First, at 0.976
structural validity the MoE gives a structural-only loop almost nothing to
learn from: one failure in 28 training cases, and a control that cannot
distinguish its arms. Second, the judged verdicts do reach the reflector
now, and the tactics they produce are about behaviour (query the live
service when asked whether it is connected; locate an attachment before
asking for it), which held-out structural validity cannot see by
construction. Both arms moving by exactly 0.0 is the metric saying so, not
the loop failing. The measurement that can see those tactics is a judged
with/without pair: `hermes puppy replay --playbook` on the same frozen
cases, both artifact sets in one blinded queue, graded by a session that
did not write it. That pair is the next number to record here.

## A cron job on the on-device model: the Auto-Dream nine-night failure, 2026-09-02

The nightly Auto-Dream job failed nine nights running with "Context length
exceeded (146,707 tokens). Cannot compress further." while the model it ran
on was loaded at 262,144. The config-map fix (the default model's
`context_length` entry) was real but not the cause. The request was:

| Part | Size | Why |
| --- | --- | --- |
| `auto_dream.py` stdout, one user message | 572K chars, about 188K real tokens | 7 days of logs (400K-char cap that kept the OLDEST turns), all of `MEMORY.md`, `procedures.md`, and 149K chars of raw `index.json` |
| tool schemas | 232 tools, about 75K real tokens | the job had no `enabled_toolsets`, so it carried 213 MCP tools it never uses |

LM Studio rejects a prompt longer than the loaded context ("the number of
tokens to keep from the initial prompt is greater than the context length"),
Hermes classifies that as a context overflow, and a single-message prompt has
no middle to summarise, so the run dies. Hermes's own estimate (chars / 4)
undercounts mixed prose and JSON by about a third against the real
tokenizer, which is why 146,707 "estimated" tokens overflowed a 262,144
window.

Fix, on the founder's machine (the script is not in this repository): the
dump has a budget of about 290K chars (140K of logs kept NEWEST first, 40K
each for the two memory files with a note saying where the rest is, and a
one-line-per-entry compact view of the index when the raw JSON is over 40K),
and the job is scoped to `enabled_toolsets: [terminal, file, no_mcp]` like
the other cron jobs. Measured after: 293K chars, about 96K real tokens, plus
a dozen native tools. General rule for any cron job on the device: a
monolith prompt must fit on its own, so budget the script output and scope
the toolsets; the compactor cannot help a one-message request.

Live result: a manual run right after the fix (`hermes cron run
2e5aee0849fb`) succeeded, 03:15:34 to 03:25:09, six native tools in the
request (verified in LM Studio's server log, which is also where to check
the tool count of any cron request; it truncates message contents), while a
learning-loop arm was sharing the same model. The failure streak reset from
nine to zero.

### The success that did nothing, and the one that destroyed data

That first successful run made zero tool calls: one 297K-character user
message in, a 720-character brief out. The memory layers had last been
written on Aug 24. A tool-use contract was added to the job prompt (write
the four layers before composing the brief; a brief with no tool calls is a
failed run) and the job run again. The second run did the work, and did it
with `write_file`: it read all four files, then replaced `MEMORY.md` (72K),
`procedures.md` (90K) and the dream journal (82K) with a few hundred bytes
each. `index.json` survived only because its `patch` hunk failed to apply.

Recovered within the hour from two sources the run itself had produced: the
`read_file` results stored in the session (exact, line-numbered; complete
for `MEMORY.md` and the journal, cut at 500 lines for `procedures.md`) and
the pre-run `auto_dream.py` dump captured while measuring the request
(complete, with invisible characters stripped). Tonight's new sections were
re-appended after the originals. Two guards now stand: `auto_dream.py`
snapshots the five memory files and the episodes directory into
`memory/.auto-dream-backups/<stamp>/` before every run (last 14 kept), and
the contract forbids `write_file` on an existing file (patch-only appends,
read first, never fall back to a whole-file write). The general lesson for
any on-device cron job that edits the owner's files: a small model treats
"write or patch" as "write", so give it one safe verb, and take a backup
before it runs.

## The monthly refresh: what to actually run when a new model drops

On-device models turn over roughly monthly, per the founder. Everything above
this section was learned by hand, mostly through scratch scripts written and
discarded during this selection. That is the failure this section exists to
prevent: the next model refresh should be a checklist against real commands,
not a re-derivation.

1. **Download the model in LM Studio, then verify its default config matches
   what you intend to test it at.** `~/.lmstudio/.internal/user-concrete-model-default-config/<publisher>/<model>.json`,
   field `load.contextLength`. Do not trust the GUI's displayed setting --
   verify with a readback (`hermes puppy replay <model> --limit 1` prints
   `context: N (verified by readback)`, or query `/api/v0/models` directly for
   `loaded_context_length`).

2. **If context pinning fails even after the CLI reports a restart,** this
   model may share the specific defect found 2026-09-01 on `qwen/qwen3.8-27b`
   and `gemma-4-26b-a4b-qat`: an MLX conversion that will not load below its
   own advertised maximum by any mechanism (CLI flag, config file, or their
   combination), only resolved by `host.ensure_context`'s restart-and-retry.
   If it fails even with that, check whether a GGUF build of the same base
   model exists in the catalog (`compatibility_type` in `/api/v0/models`) --
   GGUF conversions were not observed to have this limitation on this fleet.

2b. **A second downloaded variant (e.g. a GGUF build alongside an MLX one,
   sharing a single catalog id) IS loadable programmatically -- but only over
   the websocket SDK channel, the same one the GUI uses.** Everything else
   refuses it, and was tested exhaustively before this was found: `lms load`
   with the `@quant` suffix, the full indexed identifier, the concrete file
   path, and every alias the model index itself registers, plus the same
   keys against REST `POST /api/v1/models/load` -- all "model not found".
   The index cache (`~/.lmstudio/.internal/model-index-cache.json`) registers
   per-artifact aliases (`autoIdentifiers`) such as `gemma-4-26b-a4b-it-qat`
   for the GGUF file, and the `lmstudio` Python SDK resolves those:

   ```python
   import lmstudio as lms
   client = lms.Client("localhost:1234")
   model = client.llm.load_new_instance(
       "gemma-4-26b-a4b-it-qat",           # artifact alias from the index cache
       config=lms.LlmLoadModelConfig(context_length=262144),
   )
   ```

   Pass the config explicitly: an SDK load without one lands at a small
   default (32768 observed), not the model's maximum. The instance then
   serves under that alias as its API id -- run the exam against it with
   `hermes puppy replay <alias> --assume-loaded`, which trusts the resident
   instance and never loads or evicts anything itself.

3. **Freeze the corpus, then run the replay exam with artifacts, once per
   candidate model, every run against the same frozen directory:**

   ```
   hermes puppy freeze                      # -> $HERMES_HOME/puppy-corpus/<date>
   hermes puppy replay <model> --limit 45 \
     --dumps $HERMES_HOME/puppy-corpus/<date> \
     --artifacts /path/corrected_<slug>.jsonl \
     --out /path/summary_<slug>.json
   ```

   Never compare two runs that read different `--dumps` directories: the
   live sessions directory changes under a run (see "The corpus must be
   frozen" above). `freeze` leaves out the sessions of disabled cron jobs;
   if a job was disabled on purpose, its turns are not goals for the model.

   This is the exam that matters: real session turns, cut just before the
   agent acted. `--artifacts` writes the per-case detail a surprising result
   needs to be auditable at all -- the gap that pushed every model comparison
   before this fix into throwaway scripts.

4. **Build and grade the goal-progress queue, in a DIFFERENT session than the
   one that ran step 3:**

   ```
   hermes puppy goal-progress queue \
     --artifacts /path/corrected_*.jsonl \
     --out /path/run --seal /path/secrets/seal.json \
     --identity /path/secrets/identity.json
   ```

   Grade every row through `verdict_cli`, per the judging contract, without
   opening the seal or identity files. Then:

   ```
   hermes puppy goal-progress report \
     --out /path/run --seal /path/secrets/seal.json \
     --identity /path/secrets/identity.json --judge "<who graded this>"
   ```

   A void result means a planted control was missed; no rate survives that,
   and the fix is to re-grade more carefully, not to re-run the queue. A
   valid report also appends one ledger row per model and writes the judged
   off-path turns beside each model's playbook (paths are printed under
   `ledger` and `judged_failures_written`); nothing more to do for either.

4b. **Then let the model learn from it**, one round per shipping model,
   matched arm and shuffled control, with a reflector that is not on-device:

   ```
   hermes puppy loop <model> --run --judge <frontier model>
   hermes puppy loop <model> --run --judge <frontier model> --control
   ```

   The round reads the judged failures written in step 4 and the structural
   failures of its own baseline pass. Report held-out structural before and
   after for both arms; a matched gain the shuffled arm also shows is noise.
   Before trusting any of this on a new machine, `hermes plugins list` must
   show `puppy-playbook` as `enabled`, or the playbook is written and never
   read (the 2026-09-02 finding above).

5. **Report all three numbers, never summed**: structural validity (from the
   replay summary), agreement with the reference (`reference_match`, an
   imitation measure, not a truth measure), and judged goal progress. State
   Wilson confidence intervals and say "not separated" when they overlap
   rather than inventing a ranking.

6. **Only replace a shipping model if it wins decisively**, per the same bar
   this selection used: no pair of `google/gemma-4-26b-a4b-qat` and
   `qwen/qwen3.8-27b` was separated at 95% on structural validity, and the
   decision still rested on defensible secondary signals (latency, which
   model's failures are teachable). A new candidate needs the same kind of
   stated reason, not just a higher point estimate.

## Sources

- LM Studio API changelog: <https://lmstudio.ai/docs/developer/api-changelog>
- `reasoning_effort` ignored via API:
  <https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/988>
- Gemma 4 thinking control:
  <https://github.com/ggml-org/llama.cpp/discussions/21338>
