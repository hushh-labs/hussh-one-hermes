#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Hussh One — Self-Evolving Health & Harness Index.

A single command that AUTO-DISCOVERS and probes every active "harness" that
keeps 🤫 Hussh One (this Hermes fork) and Hermes itself running, then writes a
documented index (Markdown + JSON) so drift is caught early.

It is "self-evolving" because it does NOT hardcode a fixed list of things to
check — it discovers them at runtime from the live system:

  * launchd / systemd services       (ai.hermes.gateway, ai.openwebui.hermes, ...)
  * gateway platform listeners       (WhatsApp bridge :3000, API server :8642, ...)
  * cron jobs                        (~/.hermes/cron/jobs.json — status + last error)
  * provider profiles                (esp. the Vertex Claude gcp_sdk adapter)
  * skills                           (~/.hermes/skills + bundled skills/)
  * plugins                          (plugins/ + ~/.hermes/plugins)
  * upstream fork drift              (behind/ahead NousResearch/hermes-agent)
  * the bundled bash doctor          (scripts/hussh-one-doctor.sh, if present)

Exit code: 0 if no FAIL-level findings, 1 otherwise. WARN never fails the run.

Usage:
  python3 scripts/hussh-one-health-index.py                 # human report -> stdout
  python3 scripts/hussh-one-health-index.py --json          # machine JSON -> stdout
  python3 scripts/hussh-one-health-index.py --write         # also write index files
  python3 scripts/hussh-one-health-index.py --quiet         # only FAIL/WARN lines

Output files (with --write):
  ~/.hermes/health/hussh-one-health-index.md
  ~/.hermes/health/hussh-one-health-index.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

# Each finding: {"harness": str, "name": str, "status": ok|warn|fail|info,
#                "detail": str, "evolving": bool}
_findings: list[dict] = []


def add(harness: str, name: str, status: str, detail: str = "", evolving: bool = False) -> None:
    _findings.append(
        {
            "harness": harness,
            "name": name,
            "status": status,
            "detail": detail,
            "evolving": evolving,
        }
    )


# ---------------------------------------------------------------------------
# probes (each is independent + defensive — a probe crash must not abort the run)
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (loopback)
            return r.status, r.read(4096).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_services() -> None:
    """Discover launchd (macOS) / systemd (Linux) Hermes-related services."""
    harness = "services"
    if sys.platform == "darwin" and shutil.which("launchctl"):
        try:
            out = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception as e:  # noqa: BLE001
            add(harness, "launchctl", WARN, f"launchctl list failed: {e}")
            return
        rows = [
            ln.split("\t")
            for ln in out.splitlines()
            if ("hermes" in ln.lower() or "openwebui" in ln.lower() or "hussh" in ln.lower())
        ]
        if not rows:
            add(harness, "launchd", WARN, "no hermes/openwebui launchd services found", evolving=True)
        for cols in rows:
            label = cols[-1].strip()
            pid = cols[0].strip()
            status = cols[1].strip() if len(cols) > 1 else "?"
            if pid not in ("-", "") and pid.isdigit():
                add(harness, label, OK, f"running pid={pid}", evolving=True)
            elif status not in ("0", "-", ""):
                add(harness, label, FAIL, f"not running (last exit={status})", evolving=True)
            else:
                add(harness, label, WARN, f"loaded but idle (pid={pid})", evolving=True)
    elif shutil.which("systemctl"):
        try:
            out = subprocess.run(
                ["systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception as e:  # noqa: BLE001
            add(harness, "systemctl", WARN, f"systemctl failed: {e}")
            return
        found = False
        for ln in out.splitlines():
            if "hermes" not in ln.lower():
                continue
            found = True
            parts = ln.split()
            unit = parts[0] if parts else "?"
            active = "active" in ln and "running" in ln
            add(harness, unit, OK if active else FAIL, ln.strip(), evolving=True)
        if not found:
            add(harness, "systemd", WARN, "no hermes systemd --user units found", evolving=True)
    else:
        add(harness, "service-manager", INFO, "no launchctl/systemctl on this host")


def probe_listeners() -> None:
    """Probe known gateway listeners; report each that is up/down."""
    harness = "listeners"
    # WhatsApp Baileys bridge
    if _port_open("127.0.0.1", 3000):
        code, body = _http_get("http://127.0.0.1:3000/health")
        if code == 200 and ('"connected"' in body or '"status":"connected"' in body):
            add(harness, "whatsapp-bridge:3000", OK, "health=connected")
        elif code == 200:
            add(harness, "whatsapp-bridge:3000", WARN, f"reachable but not connected: {body[:120]}")
        else:
            add(harness, "whatsapp-bridge:3000", WARN, f"port open but /health failed: {body[:120]}")
    else:
        add(harness, "whatsapp-bridge:3000", WARN, "not listening (ok if WhatsApp disabled)")
    # OpenAI-compatible API server
    if _port_open("127.0.0.1", 8642):
        code, body = _http_get("http://127.0.0.1:8642/health")
        add(harness, "api-server:8642", OK if code == 200 else WARN, f"http={code} {body[:80]}")
    else:
        add(harness, "api-server:8642", INFO, "not listening (ok if API server disabled)")
    # Open WebUI
    for p in (8090, 8080):
        if _port_open("127.0.0.1", p):
            add(harness, f"open-webui:{p}", OK, "listening")
            break


def probe_cron() -> None:
    """Read cron jobs and surface any whose LAST run errored (exec, not delivery)."""
    harness = "cron"
    jobs_path = HERMES_HOME / "cron" / "jobs.json"
    if not jobs_path.exists():
        add(harness, "jobs.json", INFO, f"no cron store at {jobs_path}")
        return
    try:
        data = json.loads(jobs_path.read_text())
    except Exception as e:  # noqa: BLE001
        add(harness, "jobs.json", FAIL, f"unparseable: {e}")
        return
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    if not jobs:
        add(harness, "jobs", INFO, "no cron jobs configured")
        return
    for j in jobs:
        name = j.get("name") or j.get("job_id") or "?"
        if not j.get("enabled", True):
            add(harness, name, INFO, "disabled", evolving=True)
            continue
        status = (j.get("last_status") or "").lower()
        derr = j.get("last_delivery_error") or ""
        if status == "error" and not derr:
            # genuine execution error (not just a delivery/transport failure)
            add(harness, name, FAIL, "last run failed (execution error)", evolving=True)
        elif status == "error" and derr:
            add(harness, name, WARN, f"ran ok but delivery failed: {derr[:80]}", evolving=True)
        elif status in ("ok", ""):
            add(harness, name, OK, f"last_status={status or 'pending'}", evolving=True)
        else:
            add(harness, name, WARN, f"last_status={status}", evolving=True)


def probe_vertex_profile() -> None:
    """The Vertex Claude gcp_sdk adapter is load-bearing for this fork."""
    harness = "providers"
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from providers import get_provider_profile  # type: ignore

        p = get_provider_profile("google-vertex-claude")
        if p is None:
            add(harness, "google-vertex-claude", FAIL, "profile not registered")
            return
        problems = []
        if getattr(p, "auth_type", None) != "gcp_sdk":
            problems.append(f"auth_type={getattr(p, 'auth_type', None)} (want gcp_sdk)")
        models = set(getattr(p, "fallback_models", None) or ())
        if not ({"claude-opus-4-8", "claude-sonnet-4-6"} & models):
            problems.append("no claude-opus/sonnet in fallback_models")
        if problems:
            add(harness, "google-vertex-claude", WARN, "; ".join(problems))
        else:
            add(harness, "google-vertex-claude", OK, "gcp_sdk + claude models registered")
    except Exception as e:  # noqa: BLE001
        add(harness, "google-vertex-claude", FAIL, f"import/probe failed: {e}")


def probe_vertex_auth() -> None:
    """Application Default Credentials must mint a token for Vertex to work."""
    harness = "providers"
    gcloud = shutil.which("gcloud") or str(Path.home() / "google-cloud-sdk/bin/gcloud")
    if not Path(gcloud).exists():
        add(harness, "gcloud-adc", INFO, "gcloud not found; skipping ADC token check")
        return
    try:
        r = subprocess.run(
            [gcloud, "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0 and len(r.stdout.strip()) > 40:
            add(harness, "gcloud-adc", OK, "ADC access token mints OK")
        else:
            add(harness, "gcloud-adc", WARN, f"ADC token failed: {(r.stderr or '').strip()[:100]}")
    except Exception as e:  # noqa: BLE001
        add(harness, "gcloud-adc", WARN, f"ADC probe error: {e}")


def _count_dir(path: Path, pattern: str = "*") -> int:
    try:
        return sum(1 for _ in path.glob(pattern))
    except Exception:  # noqa: BLE001
        return 0


def probe_skills_plugins() -> None:
    harness = "extensions"
    user_skills = HERMES_HOME / "skills"
    repo_skills = REPO_ROOT / "skills"
    n_user = _count_dir(user_skills, "**/SKILL.md")
    n_repo = _count_dir(repo_skills, "**/SKILL.md")
    add(harness, "skills", OK if (n_user + n_repo) else WARN,
        f"{n_user} user + {n_repo} bundled SKILL.md files", evolving=True)
    repo_plugins = REPO_ROOT / "plugins"
    n_plugins = _count_dir(repo_plugins, "*/")
    add(harness, "plugins", OK if n_plugins else INFO,
        f"{n_plugins} plugin dirs", evolving=True)


def probe_session_bloat() -> None:
    """The WhatsApp session dir bloats with lid-mapping/pre-key files and can
    cause the Baileys AwaitingInitialSync 408 flap. Surface it before it bites."""
    harness = "whatsapp"
    sess = HERMES_HOME / "whatsapp" / "session"
    if not sess.exists():
        add(harness, "session-dir", INFO, "no WhatsApp session dir")
        return
    n = _count_dir(sess)
    if n > 5000:
        add(harness, "session-dir", FAIL,
            f"{n} files (>5000) — prune lid-mapping/pre-key/sender-key; risks 408 flap")
    elif n > 1500:
        add(harness, "session-dir", WARN, f"{n} files — watch for bloat (prune if it grows)")
    else:
        add(harness, "session-dir", OK, f"{n} files (healthy)")


def probe_upstream_drift() -> None:
    harness = "fork"
    if not shutil.which("git"):
        return
    def git(*a: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *a], capture_output=True, text=True, timeout=30
        ).stdout.strip()

    remotes = git("remote")
    if "upstream" not in remotes.split():
        add(harness, "upstream-remote", WARN, "no 'upstream' remote configured")
        return
    subprocess.run(["git", "-C", str(REPO_ROOT), "fetch", "upstream", "--quiet"],
                   capture_output=True, timeout=120)
    behind = git("rev-list", "--count", "HEAD..upstream/main")
    ahead = git("rev-list", "--count", "upstream/main..HEAD")
    behind_n = int(behind) if behind.isdigit() else -1
    if behind_n < 0:
        add(harness, "upstream-drift", WARN, "could not compute drift")
    elif behind_n == 0:
        add(harness, "upstream-drift", OK, f"current with upstream (ahead {ahead})")
    elif behind_n > 800:
        add(harness, "upstream-drift", WARN,
            f"{behind_n} commits behind upstream/main (ahead {ahead}) — schedule a sync")
    else:
        add(harness, "upstream-drift", INFO, f"{behind_n} behind / {ahead} ahead upstream")


def run_bash_doctor(quiet: bool) -> None:
    harness = "doctor"
    doctor = REPO_ROOT / "scripts" / "hussh-one-doctor.sh"
    if not doctor.exists():
        return
    try:
        r = subprocess.run(
            ["bash", str(doctor)], capture_output=True, text=True, timeout=120,
            env={**os.environ, "REQUIRE_SERVICES": "0"},
        )
        status = OK if r.returncode == 0 else FAIL
        tail = "\n".join((r.stdout or "").splitlines()[-3:])
        add(harness, "hussh-one-doctor.sh", status, f"exit={r.returncode}; {tail[:160]}")
    except Exception as e:  # noqa: BLE001
        add(harness, "hussh-one-doctor.sh", WARN, f"could not run: {e}")


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", INFO: "ℹ️ "}


def render_markdown() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {s: sum(1 for f in _findings if f["status"] == s) for s in (OK, WARN, FAIL, INFO)}
    lines = [
        "# 🤫 Hussh One — Self-Evolving Health & Harness Index",
        "",
        f"_Generated {now} · {counts[OK]} ok · {counts[WARN]} warn · "
        f"{counts[FAIL]} fail · {counts[INFO]} info_",
        "",
        "This index is auto-discovered at runtime (services, listeners, cron, "
        "providers, skills/plugins, fork drift) — it evolves with the system "
        "rather than hardcoding a fixed checklist.",
        "",
    ]
    harnesses: dict[str, list[dict]] = {}
    for f in _findings:
        harnesses.setdefault(f["harness"], []).append(f)
    for h in sorted(harnesses):
        lines.append(f"## {h}")
        lines.append("")
        for f in harnesses[h]:
            ev = " _(discovered)_" if f.get("evolving") else ""
            d = f" — {f['detail']}" if f["detail"] else ""
            lines.append(f"- {_ICON[f['status']]} **{f['name']}**{d}{ev}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hussh One self-evolving health index")
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    ap.add_argument("--write", action="store_true", help="write index files under ~/.hermes/health")
    ap.add_argument("--quiet", action="store_true", help="only print WARN/FAIL lines")
    ap.add_argument("--skip-doctor", action="store_true", help="don't shell out to hussh-one-doctor.sh")
    ap.add_argument("--skip-fetch", action="store_true", help="don't git-fetch upstream (faster)")
    args = ap.parse_args()

    probes = [
        probe_services,
        probe_listeners,
        probe_cron,
        probe_vertex_profile,
        probe_vertex_auth,
        probe_skills_plugins,
        probe_session_bloat,
    ]
    for p in probes:
        try:
            p()
        except Exception as e:  # noqa: BLE001
            add("probe", getattr(p, "__name__", "?"), WARN, f"probe crashed: {e}")
    if not args.skip_fetch:
        try:
            probe_upstream_drift()
        except Exception as e:  # noqa: BLE001
            add("fork", "upstream-drift", WARN, f"probe crashed: {e}")
    if not args.skip_doctor:
        run_bash_doctor(args.quiet)

    has_fail = any(f["status"] == FAIL for f in _findings)

    if args.json:
        print(json.dumps({"generated": time.time(), "findings": _findings,
                          "ok": not has_fail}, indent=2))
    else:
        md = render_markdown()
        if args.quiet:
            for f in _findings:
                if f["status"] in (WARN, FAIL):
                    print(f"{_ICON[f['status']]} [{f['harness']}] {f['name']}: {f['detail']}")
        else:
            print(md)

    if args.write:
        out = HERMES_HOME / "health"
        out.mkdir(parents=True, exist_ok=True)
        (out / "hussh-one-health-index.md").write_text(render_markdown())
        (out / "hussh-one-health-index.json").write_text(
            json.dumps({"generated": time.time(), "findings": _findings, "ok": not has_fail}, indent=2)
        )
        if not args.json and not args.quiet:
            print(f"\nWrote index to {out}/hussh-one-health-index.{{md,json}}")

    return 1 if has_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
