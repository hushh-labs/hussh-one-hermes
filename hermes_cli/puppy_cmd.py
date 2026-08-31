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

import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1

# Every model on this ladder reaches 262144 except gemma-4-e2b at 131072, and a
# ladder is only as wide as its narrowest rung.
DEFAULT_LADDER = (
    "google/gemma-4-26b-a4b-qat",
    "qwen/qwen3.8-27b",
    "google/gemma-4-31b-qat",
    "google/gemma-4-12b-qat",
    "google/gemma-4-12b",
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
    print("\nA loop round needs a graded corpus and a reflector; run it through")
    print("hermes_cli.hussh_one_routing.loop.run_round with both injected.")
    print("`--show` prints what this model has learned so far.")
    return EXIT_OK


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

    routing = sub.add_parser("routing", help="What the ledger supports recommending")
    routing.add_argument("--ledger", help="Ledger path (default: $HERMES_HOME)")

    parser.set_defaults(func=cmd_puppy)
