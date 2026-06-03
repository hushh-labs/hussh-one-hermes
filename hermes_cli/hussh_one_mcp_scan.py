#!/usr/bin/env python3
"""hussh 🤫 One — MCP connector auto-detection & onboarding scanner.

Scans the machine for MCP server definitions already configured in other AI
tooling (Codex CLI, Claude Desktop/Code, Cursor, VS Code) and reports which of
them are NOT yet registered in this Hermes install's config.yaml. With
``--install`` it merges the MISSING ones into Hermes (never overwriting or
duplicating existing servers), then verifies each newly added server.

Design discipline (mirrors the board automation guardrails):
  * dry-run by default — never mutates without ``--install``;
  * additive only — existing Hermes ``mcp_servers`` are never overwritten;
  * secret-safe — tokens are redacted in all printed output;
  * idempotent — re-runs are quiet once everything is present.

Scan sources (read-only):
  * ~/.codex/config.toml                          [mcp_servers.*]
  * ~/.claude.json                                mcpServers
  * ~/Library/Application Support/Claude/claude_desktop_config.json
  * ~/.cursor/mcp.json                            mcpServers
  * VS Code settings.json                         mcp / mcpServers blocks
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HOME = Path.home()

# tomllib is stdlib on 3.11+; fall back to tomli if available.
try:
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None


_SECRET_RE = re.compile(
    r"(token|secret|api[-_]?key|password|client[-_]?secret)", re.IGNORECASE
)


def redact(value: Any) -> Any:
    """Redact secret-looking values for safe printing."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_RE.search(str(k)):
                out[k] = "***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Scrub tokens embedded in URLs and bearer/header strings.
        scrubbed = re.sub(r"(token=)[^&\s\"']+", r"\1***", value)
        scrubbed = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9_\-.:=]+", r"\1***", scrubbed)
        scrubbed = re.sub(r"(?i)(HCT:)[A-Za-z0-9_\-.:=+/]+", r"\1***", scrubbed)
        if scrubbed != value:
            return scrubbed
        # Bare token-shaped strings (long alnum-ish) get partially masked.
        if len(value) > 16 and re.match(r"^[A-Za-z0-9_\-.]+$", value):
            return value[:4] + "…" + value[-2:]
        return value
    return value


def _redact_url(url: str) -> str:
    return re.sub(r"(token=)[^&\s]+", r"\1***", url)


# --- Source parsers ---------------------------------------------------------

def from_codex() -> dict[str, dict]:
    path = HOME / ".codex" / "config.toml"
    if not path.exists() or tomllib is None:
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not parse {path}: {exc}", file=sys.stderr)
        return {}
    return {name: cfg for name, cfg in (data.get("mcp_servers") or {}).items()}


def _from_json_mcp(path: Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not parse {path}: {exc}", file=sys.stderr)
        return {}
    return dict(data.get(key) or {})


def from_claude() -> dict[str, dict]:
    servers: dict[str, dict] = {}
    servers.update(_from_json_mcp(HOME / ".claude.json", "mcpServers"))
    servers.update(_from_json_mcp(
        HOME / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json",
        "mcpServers",
    ))
    return servers


def from_cursor() -> dict[str, dict]:
    return _from_json_mcp(HOME / ".cursor" / "mcp.json", "mcpServers")


def from_vscode() -> dict[str, dict]:
    candidates = [
        HOME / "Library" / "Application Support" / "Code" / "User" / "settings.json",
        HOME / ".config" / "Code" / "User" / "settings.json",
    ]
    servers: dict[str, dict] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        block = data.get("mcp") or {}
        servers.update(block.get("servers") or {})
        servers.update(data.get("mcpServers") or {})
    return servers


SOURCES = {
    "codex": from_codex,
    "claude": from_claude,
    "cursor": from_cursor,
    "vscode": from_vscode,
}


# --- Hermes side ------------------------------------------------------------

def hermes_existing_servers() -> dict[str, dict]:
    """Read current mcp_servers from Hermes config via the CLI (JSON-safe)."""
    cfg = HOME / ".hermes" / "config.yaml"
    if not cfg.exists():
        return {}
    try:
        import yaml  # PyYAML ships with Hermes
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        return dict(data.get("mcp_servers") or {})
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read Hermes config: {exc}", file=sys.stderr)
        return {}


def runnable(cfg: dict) -> bool:
    """Best-effort check that the server's command is available on PATH."""
    cmd = cfg.get("command")
    if not cmd:
        return True  # URL-based (HTTP) server, nothing to check locally
    return shutil.which(cmd) is not None


def install_server(name: str, cfg: dict) -> bool:
    """Register a server in Hermes via `hermes mcp add`. Returns success."""
    args = ["hermes", "mcp", "add", name]
    if cfg.get("url"):
        args += ["--url", cfg["url"]]
    else:
        args += ["--command", cfg.get("command", "")]
        for a in cfg.get("args", []) or []:
            args += ["--arg", str(a)]
    for k, v in (cfg.get("env") or {}).items():
        args += ["--env", f"{k}={v}"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"    ✗ install failed for {name}: {proc.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        print("    ✗ `hermes` CLI not found on PATH")
        return False


def verify_server(name: str) -> str:
    try:
        proc = subprocess.run(["hermes", "mcp", "test", name],
                              capture_output=True, text=True)
        out = proc.stdout
        if "✓ Connected" in out:
            tools = re.search(r"Tools discovered:\s*(\d+)", out)
            return f"✓ healthy ({tools.group(1) if tools else '?'} tools)"
        return "⚠ could not confirm health"
    except Exception:  # noqa: BLE001
        return "⚠ verify skipped"


def main() -> int:
    ap = argparse.ArgumentParser(description="hussh-one MCP onboarding scanner")
    ap.add_argument("--install", action="store_true",
                    help="merge MISSING servers into Hermes (additive only)")
    ap.add_argument("--verify", action="store_true",
                    help="run `hermes mcp test` on newly added servers")
    args = ap.parse_args()

    print("🤫 hussh 🤫 One — MCP connector onboarding scan")
    print(f"Mode: {'INSTALL (additive)' if args.install else 'DRY-RUN (report only)'}\n")

    existing = hermes_existing_servers()
    print(f"Hermes already has {len(existing)} MCP server(s): "
          f"{', '.join(sorted(existing)) or '(none)'}\n")

    discovered: dict[str, tuple[str, dict]] = {}
    for src_name, fn in SOURCES.items():
        found = fn()
        if found:
            print(f"Found {len(found)} server(s) in {src_name}: {', '.join(sorted(found))}")
        for name, cfg in found.items():
            # First source to define a name wins; don't clobber across tools.
            discovered.setdefault(name, (src_name, cfg))
    print()

    missing = {n: v for n, v in discovered.items() if n not in existing}
    if not missing:
        print("✓ Nothing to onboard — every discovered connector is already in Hermes.")
        return 0

    print(f"=== {len(missing)} connector(s) NOT yet in Hermes ===")
    added, skipped = [], []
    for name, (src, cfg) in sorted(missing.items()):
        run_ok = runnable(cfg)
        run_note = "" if run_ok else "  (⚠ command not on PATH)"
        print(f"\n• {name}  [from {src}]{run_note}")
        print(f"    config: {json.dumps(redact(cfg))[:200]}")
        if not args.install:
            continue
        if not run_ok and not cfg.get("url"):
            print("    ⏭ skipped: command unavailable on this machine")
            skipped.append(name)
            continue
        if install_server(name, cfg):
            note = verify_server(name) if args.verify else "added"
            print(f"    ✓ {note}")
            added.append(name)

    print("\n--- SCAN COMPLETE ---")
    if args.install:
        print(f"Added: {len(added)} ({', '.join(added) or 'none'})")
        if skipped:
            print(f"Skipped (unavailable): {', '.join(skipped)}")
    else:
        print(f"{len(missing)} connector(s) would be added. Re-run with --install "
              f"--verify to onboard them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
