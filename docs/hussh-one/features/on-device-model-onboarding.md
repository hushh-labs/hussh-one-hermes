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

## A latency measurement that is not yet trustworthy

On `gemma-4-31b-qat`, the same prompt with a larger output budget returned
**byte-identical output** (551 reasoning tokens, 26 answer tokens) in a fraction
of the time:

| prompt tokens | `max_tokens` | elapsed |
| --- | --- | --- |
| 51,012 | 12,000 | 405.0s |
| 51,012 | 40,000 | 37.5s |

If that holds, every latency number on this page is inflated by the harness's
own configuration. **It is not yet established**, because the large budget ran
second on each prompt and a warm prompt cache is therefore perfectly confounded
with the larger budget. This is the same class of error as comparing a model at
262144 context against one at 16384: the measurement is real, the attribution is
unproven. The controlled version runs the large budget cold on a fresh prompt.

Related and already confirmed: 7 of 50 turns in the first 50-case replay were
recorded as indeterminate at exactly 420.0s, which was the harness timeout to
the decimal. Those were **timeouts, not truncations**, and they were reported as
a budget problem for a while before anyone checked.

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
