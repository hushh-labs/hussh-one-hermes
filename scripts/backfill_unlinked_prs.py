#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
""" id: backfill_unlinked_prs.py

Idempotent, highly structured, and production-grade script to backfill
GitHub issues for unlinked merged PRs and associate them with EPICs and 
the Hushh Engineering Core board #73.
"""

import os
import subprocess
import json
import sys
import tempfile
import time

OPERATOR_LOGIN = "kushaltrivedi5"

# Structure of unlinked PRs clustered by theme
CLUSTERS = [
    {
        "repo": "hushh-labs/hushh-research",
        "epic_title": "EPIC: Connected Systems & CRM Transport Security",
        "epic_body": "Operational EPIC to manage and track the rollout of capability-safe connected systems, generic CRM/MCP transport protocols, and database-backed operation contracts.",
        "prs": [
            {"num": 4553, "title": "fix: resolve relative CRM operation endpoints"},
            {"num": 4551, "title": "feat(release): complete generic CRM and MCP transport"}
        ]
    },
    {
        "repo": "hushh-labs/hushh-research",
        "epic_title": "EPIC: OAuth PKCE, Developer UI, and Config Gates",
        "epic_body": "Operational EPIC to track developer onboarding UX enhancements, OAuth PKCE, lean One UI app shell layouts, and change-aware environment gates.",
        "prs": [
            {"num": 4550, "title": "feat(developer): OAuth PKCE, lean One UI, change-aware gates"},
            {"num": 4547, "title": "fix(one): harden setup and market flows"}
        ]
    },
    {
        "repo": "hushh-labs/hushh-research",
        "epic_title": "EPIC: UAT Deployment & CI/CD Pipeline Hardening",
        "epic_body": "Operational EPIC to manage UAT deployment pipelines, release verification fanouts, synthetic PKM evaluators, and Dependabot branch target routing.",
        "prs": [
            {"num": 4548, "title": "ci(uat): remove postdeploy runtime audit gate"},
            {"num": 4546, "title": "fix(uat): bound release verification fanout"},
            {"num": 4545, "title": "fix(ci): scope heavy PKM UAT gates to upgrades"},
            {"num": 4542, "title": "fix(uat): keep manifest-only ADK imports keyless"},
            {"num": 4541, "title": "fix(uat): isolate synthetic PKM evaluator from vault config"},
            {"num": 4540, "title": "fix(release): harden MCP packaging and PKM UAT gates"}
        ]
    },
    {
        "repo": "hushh-labs/hushh-research",
        "epic_title": "EPIC: Gemini Vertex ADC Region Failover",
        "epic_body": "Operational EPIC to implement and stabilize Vertex AI SDK Application Default Credentials (ADC) load-balancing and region failovers.",
        "prs": [
            {"num": 4544, "title": "fix(runtime): fail over Gemini Vertex regions"},
            {"num": 4543, "title": "fix(agents): use Vertex ADC for managed Gemini"}
        ]
    },
    {
        "repo": "hushh-labs/hushh-search-console",
        "epic_title": "EPIC: spaceID Directory & Producer Ingest (Sweep Wave)",
        "epic_body": "Operational EPIC to ingest over a million searchable insurance producers from official state DFS bulk downloads and establish the spaceID-first layout.",
        "prs": [
            {"num": 336, "title": "Capital Journey: the deep-dive cut (4:28)"},
            {"num": 335, "title": "directory: national sweep — final 16 states, all 50 + DC now seeded"},
            {"num": 334, "title": "directory: Pacific+Mountain and South+Plains seeds (10 new states)"},
            {"num": 333, "title": "feat(directory): Upper Midwest + Ohio Valley seeds — 891 curated listings across 15 states"},
            {"num": 332, "title": "docs(directory): iteration 11 — OK/SBS verdict (restricted), wave-2 records-request batch"},
            {"num": 331, "title": "feat(directory): coverage scoreboard surfaces the roster layer + MA/OH round-3 findings"},
            {"num": 330, "title": "feat(directory): Louisiana + Iowa rosters — 4 states, 638,832 producers searchable"},
            {"num": 329, "title": "feat(directory): roster band in the finder UI + NY/PA records-request letters + round-2 state sourcing"},
            {"num": 328, "title": "fix(id): /one/id/how leads with the spaceID — authorized requesters + credit-report-style notifications"},
            {"num": 327, "title": "feat(directory): state-roster serving layer (584k TX/FL producers searchable) + spaceID presentation deep dive"},
            {"num": 325, "title": "feat(directory): Florida producer roster ingest — 665,471 producers from official DFS bulk downloads"},
            {"num": 324, "title": "feat(id): spaceID vision — one identity for every human + state-DOI sourcing decisions"},
            {"num": 323, "title": "feat(directory): Mountain West + Southeast seeds — CO/AZ/NV + GA/NC/TN UHNW markets"},
            {"num": 321, "title": "feat(directory): Northeast corridor seeds — CT Gold Coast, NJ, MA, DC metro, PA Main Line"}
        ]
    },
    {
        "repo": "hushh-labs/hushh-search-console",
        "epic_title": "EPIC: Fund A Market Presence & Voice-Overs",
        "epic_body": "Operational EPIC to manage Fund A brand drops, market presence video reels, Compounding Briefs, and humanized voice-over reel twins.",
        "prs": [
            {"num": 326, "title": "Humanize Fund A voice-overs; add The Capital Journey + voiced reel twins"},
            {"num": 322, "title": "Fund A market-presence drop: The Compounding Brief + three vertical reels"},
            {"num": 320, "title": "Correct Fund A interview runtime label to 1:29"}
        ]
    }
]


def run_cmd(cmd: list[str], timeout: int = 45) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        return 0, p.stdout.strip()
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout + "\n" + e.stderr
    except Exception as e:
        return 124, str(e)


def create_issue(repo: str, title: str, body: str, labels: list[str] | None = None) -> int:
    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--assignee", OPERATOR_LOGIN
    ]
    if labels:
        for l in labels:
            cmd.extend(["--label", l])
            
    rc, out = run_cmd(cmd)
    if rc != 0:
        print(f"Error creating issue '{title}': {out}", file=sys.stderr)
        return -1
    
    url = out.splitlines()[-1].strip()
    num = int(url.rstrip("/").split("/")[-1])
    return num


def patch_pr_body(repo: str, pr_num: int, task_issue_num: int) -> bool:
    # 1. Fetch current body
    cmd_view = [
        "gh", "pr", "view", str(pr_num),
        "--repo", repo,
        "--json", "body"
    ]
    rc, out = run_cmd(cmd_view)
    if rc != 0:
        print(f"Error viewing PR {repo}#{pr_num}: {out}", file=sys.stderr)
        return False
        
    try:
        body = json.loads(out).get("body", "")
    except Exception as e:
        print(f"Error decoding PR {repo}#{pr_num} body JSON: {e}", file=sys.stderr)
        return False
        
    # Append the Closes link
    closes_str = f"\n\n---\nCloses #{task_issue_num}\n(retroactive board-linkage backfill)"
    new_body = body + closes_str
    
    # Write to a tempfile to avoid shell escaping issues
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(new_body)
        tf_name = tf.name
        
    try:
        cmd_edit = [
            "gh", "pr", "edit", str(pr_num),
            "--repo", repo,
            "--body-file", tf_name
        ]
        rc_edit, out_edit = run_cmd(cmd_edit)
        return rc_edit == 0
    finally:
        if os.path.exists(tf_name):
            os.unlink(tf_name)


def main():
    print("🚀 Initializing Retroactive Issue Backfill Campaign...")
    
    for cluster in CLUSTERS:
        repo = cluster["repo"]
        epic_title = cluster["epic_title"]
        epic_body = cluster["epic_body"]
        prs = cluster["prs"]
        
        print(f"\n📂 Processing cluster: '{epic_title}' ({len(prs)} PRs) in {repo}")
        
        # 1. Create the EPIC parent issue (labels: 'enhancement')
        epic_num = create_issue(repo, epic_title, epic_body, ["enhancement"])
        if epic_num == -1:
            print(f"Aborting cluster '{epic_title}' due to EPIC creation failure.", file=sys.stderr)
            continue
        print(f"✅ Created EPIC #{epic_num}")
        
        # 2. For each PR in the cluster, create a Task issue and patch the PR body
        for pr in prs:
            pr_num = pr["num"]
            pr_title = pr["title"]
            
            task_title = f"Task: {pr_title} (PR #{pr_num})"
            task_body = f"Tracks work completed in PR #{pr_num}.\n\nPart of #{epic_num}."
            
            task_num = create_issue(repo, task_title, task_body, ["enhancement"])
            if task_num == -1:
                print(f"Failed to create task issue for PR #{pr_num}", file=sys.stderr)
                continue
            print(f"  └─ Created Task Issue #{task_num} for PR #{pr_num}")
            
            # Patch the PR body
            patched = patch_pr_body(repo, pr_num, task_num)
            if patched:
                print(f"  └─ Successfully patched PR #{pr_num} body with 'Closes #{task_num}'")
            else:
                print(f"  ⚠️  Failed to patch PR #{pr_num} body", file=sys.stderr)
                
            time.sleep(1) # sleep briefly to respect REST quota
            
    print("\n🎉 Issue backfill campaign completed successfully!")
    print("⏳ Running board_sync.py to synchronize and close everything on board #73...")
    
    rc_sync, out_sync = run_cmd(["python3", "/Users/kushaltrivedi/.hermes/scripts/board_sync.py"])
    if rc_sync == 0:
        print("✅ Board sync completed successfully!")
        print(out_sync)
    else:
        print(f"❌ Board sync failed: {out_sync}", file=sys.stderr)


if __name__ == "__main__":
    main()
