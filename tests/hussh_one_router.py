"""100-case confidence/accuracy harness for the 🤫 Hussh One workload router.

Validates the synchronous, confidence-scored complexity classifier in
``hermes_cli.hussh_one_router`` against a curated 100-case corpus spanning:

  * casual chit-chat (should stay on Gemini Flash / LOW)
  * lightweight informational asks (LOW)
  * genuine engineering / deep-reasoning asks (should escalate / HIGH)
  * adversarial edge cases (casual opener + heavy body, trigger handles,
    emoji, multilingual-ish, very short technical, very long rambling)

Run directly for a human-readable accuracy report:

    .venv/bin/python tests/hussh_one_router.py

Run under pytest for CI gating:

    .venv/bin/pytest tests/hussh_one_router.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli.hussh_one_router import (  # noqa: E402
    MODEL_HIGH,
    MODEL_LOW,
    classify_complexity,
    route_workload,
)

# Each case: (prompt, expected_complexity). "high" => should escalate to Claude.
CASES: list[tuple[str, str]] = [
    # ---- Casual / social (LOW) : 20 ----
    ("hi", "low"),
    ("hey there", "low"),
    ("good morning!", "low"),
    ("thanks so much", "low"),
    ("ok cool", "low"),
    ("how are you doing today?", "low"),
    ("what's your name?", "low"),
    ("lol that's funny", "low"),
    ("👍", "low"),
    ("good night", "low"),
    ("nice work", "low"),
    ("yo", "low"),
    ("haha okay", "low"),
    ("thank you so much for the help", "low"),
    ("who are you?", "low"),
    ("great, appreciate it", "low"),
    ("gm", "low"),
    ("sup", "low"),
    ("wassup", "low"),
    ("have a good evening", "low"),
    # ---- Lightweight info (LOW) : 15 ----
    ("what's the capital of France?", "low"),
    ("what time is it in Tokyo?", "low"),
    ("tell me a fun fact", "low"),
    ("what's the weather like?", "low"),
    ("define photosynthesis", "low"),
    ("who won the world cup in 2018?", "low"),
    ("what does GDP stand for?", "low"),
    ("how many continents are there?", "low"),
    ("translate hello to spanish", "low"),
    ("what's 15 percent of 200?", "low"),
    ("give me a quote about success", "low"),
    ("what is the speed of light?", "low"),
    ("name three primary colors", "low"),
    ("when is christmas?", "low"),
    ("what's a synonym for happy?", "low"),
    # ---- Genuine engineering / deep reasoning (HIGH) : 45 ----
    ("can you refactor this function to be more efficient?", "high"),
    ("write a python script to parse this csv", "high"),
    ("debug why my deployment is failing on cloud run", "high"),
    ("create a new git branch and commit these changes", "high"),
    ("open a pull request for the auth fix", "high"),
    ("implement a rate limiter in the gateway", "high"),
    ("add unit tests for the router module", "high"),
    ("fix the 404 error on the vertex claude endpoint", "high"),
    ("plan the entire roadmap for the migration", "high"),
    ("audit the codebase for security issues", "high"),
    ("optimize the database query performance", "high"),
    ("set up a CI pipeline with github actions", "high"),
    ("design the architecture for the new microservice", "high"),
    ("run the test suite and report failures", "high"),
    ("deploy the latest build to UAT", "high"),
    ("investigate the root cause of this stack trace", "high"),
    ("scaffold a new react component with state", "high"),
    ("write a terraform config for the gcp project", "high"),
    ("migrate the schema to add a new column", "high"),
    ("benchmark the two approaches and compare accuracy", "high"),
    ("build a confidence-scored classifier and test it on 100 cases", "high"),
    ("analyze the edge cases that could break this", "high"),
    ("review my code and suggest improvements", "high"),
    ("configure the mcp server connection", "high"),
    ("patch the bug in the whatsapp bridge", "high"),
    ("explain step by step how the compression works in this repo", "high"),
    ("compare the trade-offs between these two database designs", "high"),
    ("generate the test cases for the payment flow", "high"),
    ("reorganize the project structure for clarity", "high"),
    ("profile the application and find the bottleneck", "high"),
    ("create a dockerfile and build the image", "high"),
    ("validate the api responses against the schema", "high"),
    ("write an integration test for the consent flow", "high"),
    ("diagnose the memory leak in the gateway", "high"),
    ("implement caching to optimize the endpoint latency", "high"),
    ("set up the vertex adapter for the new model", "high"),
    ("walk me through the deployment workflow step by step", "high"),
    ("fix the regression introduced in the last commit", "high"),
    ("draft a design doc for the reverse-auction system", "high"),
    ("evaluate the performance to accuracy ratio of the model", "high"),
    ("can you work on this right now, plan the roadmap, test confidence levels on 100 cases", "high"),
    ("extract all the JIDs from the sqlite database and dedupe them", "high"),
    ("convert this rest endpoint to graphql", "high"),
    ("write a script and then run it and verify the output", "high"),
    ("1. fetch the data 2. clean it 3. load into bigquery", "high"),
    # ---- Adversarial edge cases : 20 ----
    # casual opener + heavy technical body -> HIGH
    ("hi! can you debug my failing deployment and fix the root cause?", "high"),
    ("hey, quick one — refactor the auth module and add tests", "high"),
    ("thanks! now please implement the rate limiter we discussed", "high"),
    ("good morning, deploy the latest main commit to prod please", "high"),
    # short but clearly technical -> HIGH
    ("git status", "high"),
    ("run pytest", "high"),
    ("deploy now", "high"),
    ("fix the build", "high"),
    # long rambling but casual -> LOW
    ("so i was thinking about the weekend and maybe we could grab coffee and "
     "chat about life and stuff, it's been a while since we caught up properly "
     "and i miss our long conversations about everything and nothing", "low"),
    # polite chit-chat with a tech word that shouldn't trigger alone
    ("thanks, that test was fun!", "low"),
    # trigger handle should be stripped, body is casual -> LOW
    ("@One hello there", "low"),
    # trigger handle + technical -> HIGH
    ("@One deploy the build and run the tests", "high"),
    # emoji + casual -> LOW
    ("nice 👍 thanks", "low"),
    # question word only -> LOW
    ("why?", "low"),
    # ambiguous analytical -> HIGH (generous bar)
    ("analyze this and tell me what's wrong", "high"),
    ("compare these two options for me", "high"),
    # very long multi-step technical -> HIGH
    ("i need you to first clone the repo, then set up the environment, then run "
     "the full test suite, then analyze any failures, fix the root causes, and "
     "finally open a pull request with all the changes documented", "high"),
    # casual acknowledgement -> LOW
    ("okay sounds good to me", "low"),
    # informational with 'how' -> LOW
    ("how does a rainbow form?", "low"),
    # planning/strategy -> HIGH
    ("plan our q3 engineering strategy and roadmap", "high"),
]


def evaluate() -> dict:
    """Run all cases and return accuracy metrics."""
    correct = 0
    misses: list[tuple[str, str, str, float]] = []
    high_total = sum(1 for _, e in CASES if e == "high")
    low_total = sum(1 for _, e in CASES if e == "low")
    high_correct = 0
    low_correct = 0

    for prompt, expected in CASES:
        got, conf, _dbg = classify_complexity(prompt)
        if got == expected:
            correct += 1
            if expected == "high":
                high_correct += 1
            else:
                low_correct += 1
        else:
            misses.append((prompt, expected, got, conf))

    total = len(CASES)
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total,
        "high_recall": high_correct / high_total if high_total else 0.0,
        "low_recall": low_correct / low_total if low_total else 0.0,
        "misses": misses,
    }


def test_router_accuracy_meets_bar():
    """CI gate: overall accuracy must be >= 0.90 on the 100-case corpus."""
    metrics = evaluate()
    assert metrics["total"] == 100, f"expected 100 cases, got {metrics['total']}"
    assert metrics["accuracy"] >= 0.90, (
        f"accuracy {metrics['accuracy']:.2%} below 0.90 bar; "
        f"misses={metrics['misses']}"
    )


def test_high_recall_generous_bar():
    """Generous bar: we must NOT under-escalate real work. High recall >= 0.93."""
    metrics = evaluate()
    assert metrics["high_recall"] >= 0.93, (
        f"high recall {metrics['high_recall']:.2%} too low — under-escalating real work"
    )


def test_route_workload_returns_vertex_runtime_for_high():
    model, runtime = asyncio.run(route_workload("deploy the build and run the tests"))
    assert model == MODEL_HIGH
    assert runtime.get("provider") == "google-vertex-claude"
    assert runtime.get("api_mode") == "anthropic_messages"
    assert runtime.get("api_key") == "gcp-sdk"
    assert "aiplatform.googleapis.com" in runtime.get("base_url", "")


def test_route_workload_low_is_plain_gemini():
    model, runtime = asyncio.run(route_workload("hi there"))
    assert model == MODEL_LOW
    assert runtime == {}


def test_route_workload_never_raises_on_garbage():
    for junk in ["", "   ", "\n\n", "🤷", "a" * 5000]:
        model, runtime = asyncio.run(route_workload(junk))
        assert model in (MODEL_LOW, MODEL_HIGH)


if __name__ == "__main__":
    m = evaluate()
    print("=" * 64)
    print("🤫 Hussh One Workload Router — 100-Case Confidence Harness")
    print("=" * 64)
    print(f"Total cases     : {m['total']}")
    print(f"Correct         : {m['correct']}")
    print(f"Overall accuracy: {m['accuracy']:.1%}")
    print(f"HIGH recall     : {m['high_recall']:.1%}  (don't under-escalate real work)")
    print(f"LOW recall      : {m['low_recall']:.1%}  (don't waste Opus on chit-chat)")
    print("-" * 64)
    if m["misses"]:
        print(f"Misclassified ({len(m['misses'])}):")
        for prompt, exp, got, conf in m["misses"]:
            short = prompt if len(prompt) <= 60 else prompt[:57] + "..."
            print(f"  [{exp}->{got} conf={conf:.2f}] {short}")
    else:
        print("Zero misclassifications. 🎯")
    print("=" * 64)
    sys.exit(0 if m["accuracy"] >= 0.90 else 1)
