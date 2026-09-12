"""Board reports are deterministic and safe to deliver without model rewriting."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hussh-one-cron" / "board_sync.py"
SPEC = importlib.util.spec_from_file_location("board_sync_report", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_board_report_has_whatsapp_header_and_bounded_sections():
    source = """
## 🏁 Completion & Delivery
- ✅ 2 items updated
## ⚡ Active Work & Backlog
- #6719 -> would set In progress
## 🔍 Board Hygiene & Audit
- ⚠️ 3 closed issues drift
## 🧭 Hierarchy & Taxonomy
- 🧭 Hierarchy set on 1 item(s)
## 📅 Date Alignment
- Start date set: 1
## 📊 Board Statistics (read-only)
**Total tasks in hushh-labs/hushh-research:** 890
"""
    report = MODULE.format_report(source)
    assert report.startswith("*🤫 Hussh One · Board Sync*\n======================================")
    assert len(report) < 1800
    assert "##" not in report
    assert "Total tasks in hushh-labs/hushh-research: 890" in report


def test_board_report_does_not_cut_a_ticket_mid_line():
    source = "## ⚡ Active Work & Backlog\n" + "- #6719 " + ("important work " * 200)
    report = MODULE.format_report(source)
    assert len(report) < MODULE.MAX_REPORT_CHARS
    assert not report.endswith("-")
    assert report.endswith("…") or report.endswith("made.")


def test_board_report_failure_is_owner_readable():
    report = MODULE.format_report("", dry_run=True)
    assert "status is unverified" in report
    assert "Mode: dry-run" in report


def test_board_failure_is_bounded_and_single_line():
    report = MODULE.format_failure(RuntimeError("provider failed\n" + ("detail " * 200)))
    assert len(report) < MODULE.MAX_REPORT_CHARS
    assert "RuntimeError" in report
    assert "\n\n" not in report.rsplit("Error:", 1)[-1]
