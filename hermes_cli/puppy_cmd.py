# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""``hermes puppy`` -- run the on-device exam, the learning loop, and routing.

Everything under ``hussh_one_routing`` was written, tested, and reachable only
from throwaway scripts. That is not a cosmetic gap. The worst comparability bug
in this harness so far -- a ladder that ran one model at 262,144 context and
another at 16,384, invisible in the output -- came from a scratch file that
simply never thought about context. A command with defaults is how that stops
happening twice.

Four subcommands:

    hermes puppy exam     build or refresh the frozen corpus from real sessions
    hermes puppy ladder   run a suite across models and write the ledger
    hermes puppy loop     one learning round: generate, verify, reflect, curate
    hermes puppy routing  what the ledger says each task should use
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1

# The two models Puppy One ships, decided 2026-09-01 after the goal-progress
# result: the MoE leads structural validity and latency, the qwen dense model
# leads judged goal progress, and no third model has led anything since.
# `gemma-4-12b` was cut -- it led only agreement, the weakest of the three
# signals by this harness's own design. Kept as one tuple, not scattered
# literals, so a monthly model refresh changes candidates in exactly one
# place; add a third entry here (e.g. a GGUF build of the same base model)
# to widen the ladder without touching any command below.
DEFAULT_LADDER = (
    "google/gemma-4-26b-a4b-qat",
    "qwen/qwen3.8-27b",
)


def _exam_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "puppy-exam"


def cmd_puppy(args) -> int:
    action = getattr(args, "puppy_command", None) or "routing"
    handler = {
        "exam": _cmd_exam,
        "ladder": _cmd_ladder,
        "loop": _cmd_loop,
        "replay": _cmd_replay,
        "goal-progress": _cmd_goal_progress,
        "routing": _cmd_routing,
    }.get(action)
    if handler is None:
        print(f"unknown puppy subcommand: {action}", file=sys.stderr)
        return EXIT_ERROR
    return handler(args)


def _cmd_exam(args) -> int:
    """Freeze the corpus from this owner's own session dumps.

    Kept local under ``$HERMES_HOME/puppy-exam`` rather than committed. It is
    this owner's data on this owner's device, which is the premise of an
    on-device agent, and it is also far too large and too specific to be
    anyone's fixture but theirs.
    """
    from hermes_cli.hussh_one_routing.exam import build as B

    out = _exam_dir()
    out.mkdir(parents=True, exist_ok=True)

    counts, truncated, sizes = {}, 0, []
    seen = set()
    for dump in B.iter_dumps():
        body = B.request_body(dump)
        if not body:
            continue
        sizes.append(B.wire_size(body))
        for message in body.get("messages") or []:
            for name, call_args, _cid in B.iter_tool_calls(message):
                key = B.fingerprint(name, call_args)
                if key in seen:
                    continue
                seen.add(key)
                if B.is_truncated(call_args):
                    truncated += 1
                    continue
                counts[name] = counts.get(name, 0) + 1

    ordered = sorted(sizes)

    def percentile(fraction):
        return ordered[int(len(ordered) * fraction)] if ordered else 0

    manifest = {
        "dumps": len(sizes),
        "unique_calls": sum(counts.values()),
        "quarantined_truncated": truncated,
        "by_tool": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "wire_chars": {
            "median": percentile(0.5),
            "p90": percentile(0.9),
            "max": ordered[-1] if ordered else 0,
        },
        "tokens_estimated": {
            "median": B.estimate_tokens("x" * percentile(0.5)),
            "p90": B.estimate_tokens("x" * percentile(0.9)),
            "max": B.estimate_tokens("x" * (ordered[-1] if ordered else 0)),
        },
        "caveats": [
            "Every dump is a failed request, so this corpus over-represents "
            "hard cases.",
            "Token counts use a measured 3.05 chars/token for this corpus; the "
            "generic 4.0 undercounts it by 31%.",
            "Truncated calls are the dumper's history compactor, not model "
            "output, and are excluded from scoring.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"corpus written to {out}")
    print(f"  {manifest['dumps']} dumps, {manifest['unique_calls']} unique calls, "
          f"{truncated} quarantined")
    for tool, count in list(manifest["by_tool"].items())[:8]:
        print(f"    {count:>5}  {tool}")
    tokens = manifest["tokens_estimated"]
    print(f"  tokens: median {tokens['median']:,}  p90 {tokens['p90']:,}  "
          f"max {tokens['max']:,}")
    return EXIT_OK


def _cmd_ladder(args) -> int:
    """Run one suite across the ladder at a pinned context, then record it."""
    from hermes_cli.hussh_one_routing import corpus_build as C
    from hermes_cli.hussh_one_routing import host as H
    from hermes_cli.hussh_one_routing import run_suite as R

    models = list(args.models) if args.models else list(DEFAULT_LADDER)
    corpus = Path(args.corpus) if args.corpus else None
    if not corpus or not corpus.exists():
        print("a frozen corpus is required; run `hermes puppy exam` first",
              file=sys.stderr)
        return EXIT_ERROR

    entries, manifest = C.load(corpus)
    cases = [c for c in C.to_cases(entries) if c.reference_side][: args.limit]
    originals = {
        c.case_id: e.conflicted_text for c, e in zip(cases, entries)
    }
    pinned = args.context or H.common_max_context(models)
    print(f"{len(models)} models, {len(cases)} cases, context {pinned}")

    report = R.run(
        models=models,
        cases=cases,
        originals=originals,
        reps=args.reps,
        context_length=pinned,
        judge_model=args.judge,
        destination=Path(args.out) if args.out else None,
        on_progress=print,
    )
    print("\nranking (validity first, latency second):")
    for row in report["ranking"]:
        print(f"  {row['model']:<30} valid={row['valid_rate']} "
              f"graded={row['graded']} indet={row['indeterminate']} "
              f"median={row['median_s']}s")
    print(f"\ncomparable: {report['comparability'].get('comparable')}")
    print(f"ledger rows written: {len(report.get('ledger') or [])}")
    return EXIT_OK


def _cmd_loop(args) -> int:
    """One learning round against a model, or the shuffled negative control."""
    from hermes_cli.hussh_one_routing import playbook as pb

    book = pb.load(args.model, args.suite)
    print(f"{args.model} / {args.suite}")
    print(f"  round {book.round_number}, {len(book.active_bullets)} active tactics")
    if args.show:
        print()
        print(pb.render_markdown(book))
        return EXIT_OK
    if not args.run:
        print("\n`--show` prints the playbook; `--run` executes one round on")
        print("real session turns. A round needs a reflector, which must not be")
        print("an on-device model.")
        return EXIT_OK

    from hermes_cli.hussh_one_routing import host as H
    from hermes_cli.hussh_one_routing import loop as L
    from hermes_cli.hussh_one_routing import loop_replay as LR
    from hermes_cli.hussh_one_routing import reasoning as RZ
    from hermes_cli.hussh_one_routing import reflector as RF
    from hermes_cli.hussh_one_routing.exam import replay as RP

    cases = RP.extract_cases(max_cases=args.limit)
    if not cases:
        print("no replay cases found", file=sys.stderr)
        return EXIT_ERROR

    pinned = args.context or H.common_max_context([args.model])
    if not pinned:
        print(f"could not determine a context length for {args.model}",
              file=sys.stderr)
        return EXIT_ERROR
    loaded = H.ensure_context(
        args.model, pinned,
        unload=H.unload, resident=H.resident, load=H.load_at_context,
        restart=None if args.no_restart else H.restart_app,
    )
    if loaded != pinned:
        print(f"asked for context {pinned:,}, server holds {loaded!r}; "
              "refusing to run a learning round whose window is not what it "
              "claims", file=sys.stderr)
        return EXIT_ERROR
    print(f"  context: {loaded:,} (verified by readback)")

    profile = RZ.ReasoningProfile(
        model=args.model, family=RZ.family_of(args.model),
        mode=RZ.MAX, prefix=RZ.control_for(args.model, RZ.MAX),
    )
    answer = LR.make_answerer(
        model=args.model,
        max_tokens=args.max_tokens or profile.max_tokens,
        timeout=args.timeout,
        reasoning_prefix=profile.prefix,
        reasoning_effort=RZ.effort_for(args.model, RZ.MAX),
    )
    try:
        reflect = RF.make_reflector(
            model=args.judge, suite=LR.SUITE_ID, ask=None if args.judge else _no_judge
        )
    except Exception as exc:  # noqa: BLE001
        print(f"reflector refused: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # The shuffled arm detaches each diagnosis from the case that produced it.
    # If the playbook improves as much on mismatched evidence, the loop is
    # fitting noise and the matched arm's gain means nothing either.
    if args.control:
        inner = reflect

        def reflect(failures, text):  # noqa: F811
            return inner(L.shuffled_control(failures), text)

    print(f"\n{len(cases)} real session turns | "
          f"{'SHUFFLED CONTROL' if args.control else 'matched evidence'}")
    # Verdicts from an independent judge (`hermes puppy goal-progress report`)
    # are the one non-structural signal the loop may learn from. No file on
    # disk means none, not an error. The shuffled arm detaches these from
    # their cases exactly like structural evidence, so the control stays fair.
    judged = LR.load_judged(args.model)
    if judged:
        print(f"  judged off-path verdicts on file: "
              f"{sum(len(rows) for rows in judged.values())}")

    # The replay suite's own scorer and failure filter, not run_round's
    # generic defaults: the generic `score` counts disagreement with the
    # reference as a failure, which once reported a 0.952-structural model
    # as 0.357 held-out. The signal the loop is measured on must be the
    # signal it is allowed to learn from (run_round's docstring says so; this
    # call site simply never passed them).
    result, book = L.run_round(
        model=args.model + ("::control" if args.control else ""),
        suite=LR.SUITE_ID,
        cases=cases,
        answer=answer,
        reflect=reflect,
        score_fn=LR.score,
        failures_fn=functools.partial(LR.learnable_failures, judged=judged),
        on_progress=print,
    )
    print("\n=== round ===")
    print(json.dumps(result.to_dict(), indent=2)[:1600])
    if args.out:
        Path(args.out).write_text(json.dumps(result.to_dict(), indent=2),
                                  encoding="utf-8")
    return EXIT_OK


def _no_judge(prompt: str) -> str:
    """Used when no judge is configured: propose nothing rather than guess."""
    return ""


def compact_case(case, compressor, *, replay_module=None):
    """Run one replay case's history through the REAL Hermes compactor.

    This is the long-horizon probe the corpus itself cannot provide: every
    real case fits under 96k, so a full-context exam never exercises what a
    long-running session actually lives with -- production compaction
    (``agent.context_compressor.ContextCompressor``: prune old tool results,
    protect the head, keep a recent tail, LLM-summarize the middle).
    Compacting a case with that exact machinery, then asking for the next
    action, measures the model under the conditions Hermes naturally creates
    once a session outgrows its threshold.

    Fairness detail carried over from the earlier recency-window arm:
    ``known_paths`` is recomputed from the COMPACTED view, because grounding
    is judged against what the model could actually see, and a summary
    legitimately drops paths the full history contained.

    Returns ``(new_case, meta)``; the caller sends ``new_case`` and records
    ``meta`` (before/after tokens, wall time, whether the summariser fell
    back to a deterministic drop) beside the verdict, so a quality delta can
    be attributed to compaction rather than hidden inside it.
    """
    import copy
    import dataclasses
    import time as _time

    RP = replay_module
    if RP is None:
        from hermes_cli.hussh_one_routing.exam import replay as RP  # noqa: N806

    # Measure before/after on the SAME basis (the messages body). case.tokens
    # is the whole request body including the tool catalog, and comparing it
    # against a messages-only 'after' overstated every shrink -- found when a
    # first run reported 55k -> 12k on a case the compactor had not touched.
    from hermes_cli.hussh_one_routing.exam.build import estimate_tokens

    before_tokens = estimate_tokens(json.dumps(case.messages))
    started = _time.time()
    compressed = compressor.compress(
        copy.deepcopy(case.messages),
        current_tokens=case.tokens,
        force=True,
    )
    elapsed = round(_time.time() - started, 1)

    # tokens is a property derived from wire_chars, so the compacted size
    # flows through the same estimator every other case uses.
    new_case = dataclasses.replace(
        case,
        messages=compressed,
        wire_chars=len(json.dumps(compressed)),
        known_paths=RP._known_paths(compressed),
    )
    meta = {
        "compacted": True,
        "compaction_s": elapsed,
        "tokens_before": before_tokens,
        "tokens_after": estimate_tokens(json.dumps(compressed)),
        "messages_before": len(case.messages),
        "messages_after": len(compressed),
        "summary_fallback_used": bool(
            getattr(compressor, "_last_summary_fallback_used", False)
        ),
        "summary_error": str(
            getattr(compressor, "_last_summary_error", None) or ""
        )[:200],
    }
    return new_case, meta


def _cmd_replay(args) -> int:
    """Ask a model to take the next action on real moments from real sessions.

    The exam that matters. Everything else grades a chore; this asks the
    question the product asks, on the owner's own work, at the context length
    that work actually arrives at.

    Context is pinned and verified, never left to whatever happened to be
    loaded. Two bugs found 2026-09-01 by direct measurement are fixed here
    because this is the command a monthly model refresh actually runs:

      * ``reasoning_effort`` used to be hard-coded to ``"low"`` for every
        model. That string is inert for gemma but LIVE for qwen through LM
        Studio's chat template -- "low" was silently injecting a think-less
        instruction into every qwen run this command ever produced. Computed
        per model now, via :func:`reasoning.effort_for`.
      * Nothing pinned or verified context at all; whatever was already
        loaded (or whatever a JIT load's own default happened to be) is what
        ran. Pinned via :func:`host.ensure_context`, which restarts LM Studio
        once and retries if a plain reload does not take effect -- found to
        be necessary, not optional, on this build. Re-verified after every
        turn: a live production gateway shares this LM Studio instance, and a
        turn graded after it swapped models would look identical to a normal
        one without the check.
    """
    import time

    from hermes_cli.hussh_one_routing import host as H
    from hermes_cli.hussh_one_routing import reasoning as RZ
    from hermes_cli.hussh_one_routing.exam import replay as RP
    from hermes_cli.hussh_one_routing.request import complete

    cases = RP.extract_cases(max_cases=args.limit)
    if not cases:
        print("no replay cases found in the session dumps", file=sys.stderr)
        return EXIT_ERROR

    sizes = sorted(c.tokens for c in cases)
    print(f"{len(cases)} cases from {len({c.session_id for c in cases})} sessions")
    print(f"  next-action mix: " + ", ".join(
        f"{name} {count}" for name, count in _top_tools(cases)
    ))
    print(f"  prompt tokens: median {sizes[len(sizes)//2]:,} max {sizes[-1]:,}")
    print(f"  catalog size: max {max(c.catalog_size for c in cases)}")

    if args.assume_loaded:
        # For a model this harness cannot load itself: found 2026-09-01 with
        # a GGUF variant that shares its catalog id with an already-resident
        # MLX build. LM Studio assigns the second one a disambiguating
        # ":N" suffix, but that suffix is a display label for an EXISTING
        # instance, not a loadable target -- a fresh `lms load "<id>:N"`
        # from empty returns "model not found", confirmed by direct test.
        # Only the GUI's own variant picker can create that instance, and
        # only for the one load action, not as a persistent default. Calling
        # ensure_context here would drain the very instance this mode exists
        # to test before ever using it. So: read-only. Trust what is already
        # loaded, or refuse -- never try to load or evict anything.
        loaded = H.loaded_context(args.model)
        if not loaded:
            print(f"{args.model} is not currently loaded, and --assume-loaded "
                  "refuses to load or evict anything; load it first (the GUI "
                  "variant picker, for a model this harness cannot load "
                  "itself)", file=sys.stderr)
            return EXIT_ERROR
        if args.context and loaded != args.context:
            print(f"asked for context {args.context:,}, {args.model} is "
                  f"already loaded at {loaded:,}; --assume-loaded will not "
                  "reload it to match", file=sys.stderr)
            return EXIT_ERROR
        pinned = loaded
        print(f"  context: {pinned:,} (already loaded; trusted as-is, "
              "not managed by this run)")
    else:
        pinned = args.context or H.common_max_context([args.model])
        if not pinned:
            print(f"could not determine a context length for {args.model}",
                  file=sys.stderr)
            return EXIT_ERROR
        loaded = H.ensure_context(
            args.model, pinned,
            unload=H.unload, resident=H.resident, load=H.load_at_context,
            restart=None if args.no_restart else H.restart_app,
        )
        if loaded != pinned:
            print(f"asked for context {pinned:,}, server holds {loaded!r}; "
                  "refusing to run a benchmark whose window is not what it "
                  "claims" + ("" if args.no_restart else
                              " (even after a restart)"), file=sys.stderr)
            return EXIT_ERROR
        print(f"  context: {loaded:,} (verified by readback)")

    profile = RZ.ReasoningProfile(
        model=args.model, family=RZ.family_of(args.model),
        mode=RZ.MAX, prefix=RZ.control_for(args.model, RZ.MAX),
    )
    budget = args.max_tokens or profile.max_tokens
    effort = RZ.effort_for(args.model, RZ.MAX)
    print(f"  thinking: {profile.mode} | budget {budget} | effort {effort}")

    compressor = None
    if args.compact_threshold:
        # The REAL production compactor, with the model under test as its own
        # summariser -- matching the on-device posture, where compression runs
        # locally rather than on a cloud aux. force=True per the in-repo
        # compaction-eval precedent, so cooldown state never skips a case.
        from agent.context_compressor import ContextCompressor

        compressor = ContextCompressor(
            model=args.model,
            summary_model_override=args.model,
            base_url="http://127.0.0.1:1234/v1",
            api_key="lm-studio",
            config_context_length=pinned,
            quiet_mode=True,
        )
        exceeding = sum(1 for c in cases if c.tokens > args.compact_threshold)
        print(f"  compaction: REAL Hermes compactor at threshold "
              f"{args.compact_threshold:,} tokens; {exceeding}/{len(cases)} "
              "cases exceed it and will be compacted")
    print()

    def last_user(case, limit=2000):
        for message in reversed(case.messages):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return message["content"][-limit:]
        return ""

    verdicts = []
    artifact_rows = []
    interfered = False
    for index, case in enumerate(cases, 1):
        if interfered:
            verdict = RP.grade(case, chosen=None, arguments=None)
            verdict.indeterminate = "environment_interference"
            verdicts.append(verdict)
            continue

        compaction_meta = {"compacted": False}
        if compressor is not None and case.tokens > args.compact_threshold:
            try:
                case, compaction_meta = compact_case(
                    case, compressor, replay_module=RP
                )
                print(f"[{index}/{len(cases)}] compacted "
                      f"{compaction_meta['tokens_before']:,} -> "
                      f"{compaction_meta['tokens_after']:,} tokens in "
                      f"{compaction_meta['compaction_s']}s"
                      + (" (summary FELL BACK to deterministic drop)"
                         if compaction_meta["summary_fallback_used"] else ""))
            except Exception as exc:  # noqa: BLE001
                # A compactor crash is a harness fault, never a model verdict.
                verdict = RP.grade(case, chosen=None, arguments=None)
                verdict.indeterminate = f"compaction_error: {exc}"[:120]
                verdicts.append(verdict)
                print(f"[{index}/{len(cases)}] COMPACTION ERROR: {exc}",
                      file=sys.stderr)
                continue

        messages = profile.apply(case.messages)
        started = time.time()
        turn = complete(
            model=args.model,
            messages=messages,
            max_tokens=budget,
            reasoning_effort=effort,
            tools=RP.tools_payload(case) or None,
            timeout=args.timeout,
        )
        elapsed = time.time() - started

        chosen = arguments = None
        if turn.indeterminate:
            verdict = RP.grade(case, chosen=None, arguments=None)
            verdict.indeterminate = (
                "timeout" if turn.timed_out
                else "truncated" if turn.truncated else (turn.error or "error")
            )
            print(f"[{index}/{len(cases)}] INDETERMINATE ({verdict.indeterminate}) "
                  f"{elapsed:.0f}s  {case.tokens:,}tok")
            verdicts.append(verdict)
        else:
            chosen, arguments = _first_call(turn)
            verdict = RP.grade(case, chosen=chosen, arguments=arguments)
            verdict.elapsed_s = round(elapsed, 1)
            verdicts.append(verdict)
            mark = "=" if verdict.label_match else "~"
            broken = [o.name for o in verdict.failures]
            print(f"[{index}/{len(cases)}] {mark} chose {chosen or '(none)'} "
                  f"want {case.expected_tool} {elapsed:.0f}s {case.tokens:,}tok"
                  + (f"  FAIL {','.join(broken[:3])}" if broken else ""))

        if args.artifacts:
            # Every field goal_progress.build_rows and a future audit both
            # need. This is the exact gap that pushed every prior model
            # comparison this session into throwaway scripts: this command's
            # own --out only ever wrote an aggregate summary, discarding the
            # per-case detail a surprising result needs to be checked at all.
            artifact_rows.append({
                "case_id": case.case_id,
                "prompt_tokens": case.tokens,
                "catalog_size": case.catalog_size,
                "user_request_tail": last_user(case),
                "reference_tool": case.expected_tool,
                "reference_args": case.expected_args,
                "chosen_tool": chosen,
                "chosen_args": arguments,
                "assistant_text": (turn.content or "")[:1500],
                "reasoning_tokens": turn.reasoning_tokens,
                "completion_tokens": turn.completion_tokens,
                "finish_reason": turn.finish_reason,
                "indeterminate": verdict.indeterminate,
                "label_match": verdict.label_match,
                "oracles": [
                    {"name": o.name, "outcome": o.outcome, "detail": o.detail}
                    for o in verdict.outcomes
                ],
                "elapsed_s": round(elapsed, 1),
                **compaction_meta,
            })

        try:
            current = H.loaded_context(args.model)
        except Exception:  # noqa: BLE001
            current = None
        if current != pinned:
            interfered = True
            print(f"  !! context now reads {current!r}, pinned at {pinned:,}; "
                  "another process reloaded this model mid-run -- remaining "
                  "cases marked indeterminate rather than graded on a window "
                  "that no longer holds", file=sys.stderr)

    summary = RP.summarize(verdicts)
    summary["context_length"] = pinned
    summary["reasoning_effort"] = effort
    summary["environment_interference"] = interfered
    if compressor is not None:
        compacted = [r for r in artifact_rows if r.get("compacted")]
        summary["compact_threshold"] = args.compact_threshold
        summary["cases_compacted"] = len(compacted)
        if compacted:
            times = sorted(r["compaction_s"] for r in compacted)
            summary["compaction_median_s"] = times[len(times) // 2]
            summary["summary_fallbacks"] = sum(
                1 for r in compacted if r.get("summary_fallback_used")
            )
    print("\n=== summary ===")
    print(json.dumps(summary, indent=2)[:2200])
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if args.artifacts:
        Path(args.artifacts).write_text(
            "".join(json.dumps(r) + "\n" for r in artifact_rows), encoding="utf-8"
        )
        print(f"\nper-case artifacts ({len(artifact_rows)} rows): {args.artifacts}")
    return EXIT_OK


def _cmd_goal_progress(args) -> int:
    """The judged third number: did the chosen action advance the goal.

    Two phases, because the queue author and the grading session must not be
    the same session -- see the judging contract. ``queue`` turns replay
    artifacts (from ``hermes puppy replay --artifacts``) into one blinded,
    sealed queue across every model. A separate session grades it through
    ``verdict_cli``. ``report`` then ingests the graded queue and prints
    per-model rates, voiding the whole run if a planted control was missed.
    """
    from hermes_cli.hussh_one_routing.exam import goal_progress as GP

    action = getattr(args, "goal_progress_command", None)
    if action == "queue":
        artifacts = [Path(a) for a in args.artifacts]
        missing = [str(a) for a in artifacts if not a.exists()]
        if missing:
            print(f"artifact file(s) not found: {', '.join(missing)}",
                  file=sys.stderr)
            return EXIT_ERROR
        run = GP.write_goal_queue(
            artifact_files=artifacts,
            out_dir=Path(args.out),
            seal_path=Path(args.seal),
            identity_path=Path(args.identity),
        )
        print(f"queue: {run.queue_path}")
        print(f"rows : {run.row_count} (controls {run.control_count})")
        print(f"\nGrade every row in a DIFFERENT session via verdict_cli, "
              f"never opening {args.seal} or {args.identity} until grading "
              "is complete, then run `hermes puppy goal-progress report`.")
        return EXIT_OK

    if action == "report":
        result = GP.report(
            out_dir=Path(args.out), seal_path=Path(args.seal),
            identity_path=Path(args.identity), judge_label=args.judge,
        )
        # A rate nobody can compare to the next model's is a number, not a
        # trend: every report lands in the evolution ledger, void or not.
        ledger = GP.append_to_ledger(
            result, ledger_path=getattr(args, "ledger", None)
        )
        result["ledger"] = {"path": ledger["path"], "rows": len(ledger["rows"])}
        # And the judged off-path turns go beside the model's playbook, where
        # the learning loop reads them as evidence.
        result["judged_failures_written"] = GP.write_judged_failures(result)
        print(json.dumps(result, indent=2))
        return EXIT_OK

    print("goal-progress needs a subcommand: queue or report", file=sys.stderr)
    return EXIT_ERROR


def _top_tools(cases, limit: int = 6):
    counts: dict = {}
    for case in cases:
        counts[case.expected_tool] = counts.get(case.expected_tool, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _first_call(turn):
    """The tool the model chose, and its arguments, from an OpenAI-shaped turn."""
    calls = getattr(turn, "tool_calls", None) or []
    if not calls:
        return None, None
    function = (calls[0] or {}).get("function") or {}
    name = function.get("name")
    raw = function.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            raw = {}
    return name, (raw if isinstance(raw, dict) else {})


def _cmd_routing(args) -> int:
    """What the ledger actually supports recommending."""
    from hermes_cli.hussh_one_pkm.judge_queue import (
        compare_runs,
        default_ledger_path,
        read_ledger,
    )

    path = Path(args.ledger) if args.ledger else default_ledger_path()
    if not path.exists():
        print(f"no ledger at {path}; run `hermes puppy ladder` first",
              file=sys.stderr)
        return EXIT_ERROR

    rows = read_ledger(path)
    print(f"{len(rows)} runs recorded at {path}\n")

    by_model: dict = {}
    for row in rows:
        if row.get("void"):
            continue
        model = row.get("answerer_model", "?")
        board = row.get("scoreboard") or {}
        accuracy = board.get("accuracy")
        if accuracy is None:
            continue
        suite = (row.get("benchmark") or {}).get("suite", "?")
        by_model.setdefault((suite, model), []).append(accuracy)

    if not by_model:
        print("no non-void runs with an accuracy; nothing can be recommended.")
        return EXIT_OK

    suites: dict = {}
    for (suite, model), scores in by_model.items():
        suites.setdefault(suite, []).append((sum(scores) / len(scores), model, len(scores)))

    for suite, entries in sorted(suites.items()):
        entries.sort(reverse=True)
        print(f"{suite}:")
        for accuracy, model, runs in entries:
            print(f"   {accuracy:.3f}  {model}  ({runs} run{'s' if runs != 1 else ''})")
        best, runner_up = entries[0], entries[1] if len(entries) > 1 else None
        if runner_up and abs(best[0] - runner_up[0]) < 0.05:
            # Two models within noise is not a ranking, and printing one anyway
            # is how a coin flip becomes a routing decision.
            print(f"   -> inconclusive: {best[1]} and {runner_up[1]} are within "
                  "noise on this evidence")
        else:
            print(f"   -> {best[1]}")
        comparison = compare_runs(path, model=best[1])
        if not comparison.get("comparable"):
            print(f"   !  runs not comparable: {comparison.get('reason', '')[:88]}")
        print()
    return EXIT_OK


def build_puppy_parser(subparsers) -> None:
    """Register ``hermes puppy`` and its subcommands."""
    parser = subparsers.add_parser(
        "puppy",
        help="On-device model exam, learning loop and routing",
        description=(
            "Measure what on-device models can actually do on this owner's own "
            "work, teach them from their graded failures, and route each task "
            "to whichever model earned it."
        ),
    )
    sub = parser.add_subparsers(dest="puppy_command")

    sub.add_parser("exam", help="Freeze the exam corpus from real session dumps")

    ladder = sub.add_parser("ladder", help="Run a suite across models, then record it")
    ladder.add_argument("--models", nargs="*", help="Model ids (default: the full ladder)")
    ladder.add_argument("--corpus", help="Frozen corpus JSON")
    ladder.add_argument("--limit", type=int, default=20, help="Cases per model")
    ladder.add_argument("--reps", type=int, default=1, help="Repetitions per model")
    ladder.add_argument("--context", type=int, help="Pinned context (default: ladder max)")
    ladder.add_argument("--judge", help="Judge model; must not be on-device")
    ladder.add_argument("--out", help="Write the full report here")

    loop = sub.add_parser("loop", help="Inspect or run a learning round")
    loop.add_argument("model", help="Model id")
    loop.add_argument("--suite", default="file_edit", help="Suite name")
    loop.add_argument("--show", action="store_true", help="Print the playbook")
    loop.add_argument("--run", action="store_true",
                      help="Execute one round on real session turns")
    loop.add_argument("--control", action="store_true",
                      help="Shuffle the evidence: the loop's negative control")
    loop.add_argument("--limit", type=int, default=25, help="Cases per round")
    loop.add_argument("--judge", help="Reflector model; must not be on-device")
    loop.add_argument("--max-tokens", type=int, dest="max_tokens")
    loop.add_argument("--timeout", type=float, default=900.0)
    loop.add_argument("--out", help="Write the round result here")
    loop.add_argument("--context", type=int,
                      help="Pinned context (default: this model's max)")
    loop.add_argument("--no-restart", action="store_true",
                      help="Refuse a context mismatch rather than "
                           "restarting LM Studio to try to clear it")

    replay = sub.add_parser(
        "replay",
        help="Replay real session turns and grade the next action",
    )
    replay.add_argument("model", help="On-device model id")
    replay.add_argument("--limit", type=int, default=30, help="Cases to replay")
    replay.add_argument("--max-tokens", type=int, dest="max_tokens",
                        help="Generation budget (default: measured)")
    replay.add_argument("--timeout", type=float, default=600.0)
    replay.add_argument("--out", help="Write the summary here")
    replay.add_argument("--artifacts",
                        help="Write one JSON line per case here (needed for "
                             "goal-progress queue and for auditing a "
                             "surprising result after the fact)")
    replay.add_argument("--context", type=int,
                        help="Pinned context (default: this model's max)")
    replay.add_argument("--no-restart", action="store_true",
                        help="Refuse a context mismatch rather than "
                             "restarting LM Studio to try to clear it")
    replay.add_argument("--assume-loaded", action="store_true",
                        help="Trust whatever is already loaded under this "
                             "id; never load, evict, or restart. For a "
                             "model this harness cannot load itself (e.g. "
                             "a non-default catalog variant reachable only "
                             "through the LM Studio GUI's own picker)")
    replay.add_argument("--compact-threshold", type=int,
                        dest="compact_threshold",
                        help="The long-horizon probe: cases above this many "
                             "tokens are first compacted by the REAL Hermes "
                             "compactor (prune tool results, protect head, "
                             "keep tail, LLM-summarize the middle, with the "
                             "model under test as its own summariser), "
                             "measuring the model under the conditions a "
                             "long-running session naturally creates")

    goal_progress = sub.add_parser(
        "goal-progress",
        help="The judged third number: did the action advance the goal",
    )
    gp_sub = goal_progress.add_subparsers(dest="goal_progress_command")

    gp_queue = gp_sub.add_parser(
        "queue", help="Build one blinded queue from replay artifacts"
    )
    gp_queue.add_argument("--artifacts", nargs="+", required=True,
                          help="One or more replay --artifacts files, "
                               "one per model")
    gp_queue.add_argument("--out", required=True, help="Queue directory")
    gp_queue.add_argument("--seal", required=True,
                          help="Seal path, OUTSIDE the queue directory")
    gp_queue.add_argument("--identity", required=True,
                          help="Identity map path, OUTSIDE the queue "
                               "directory")

    gp_report = gp_sub.add_parser(
        "report", help="Ingest a graded queue and report per-model rates"
    )
    gp_report.add_argument("--out", required=True, help="Queue directory")
    gp_report.add_argument("--seal", required=True, help="Seal path")
    gp_report.add_argument("--identity", required=True, help="Identity map path")
    gp_report.add_argument("--judge", required=True,
                           help="Label identifying who graded this queue")
    gp_report.add_argument("--ledger",
                           help="Evolution ledger path (default: the shared "
                                "ledger under HERMES_HOME)")

    routing = sub.add_parser("routing", help="What the ledger supports recommending")
    routing.add_argument("--ledger", help="Ledger path (default: $HERMES_HOME)")

    parser.set_defaults(func=cmd_puppy)
