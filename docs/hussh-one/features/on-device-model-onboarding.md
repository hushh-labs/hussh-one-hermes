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

## Model selection, 2026-08-31: replicated across two runs

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

## Sources

- LM Studio API changelog: <https://lmstudio.ai/docs/developer/api-changelog>
- `reasoning_effort` ignored via API:
  <https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/988>
- Gemma 4 thinking control:
  <https://github.com/ggml-org/llama.cpp/discussions/21338>
