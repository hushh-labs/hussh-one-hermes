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
