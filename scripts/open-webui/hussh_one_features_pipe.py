"""
title: 🤫 Hussh One — Features
author: hussh-one
version: 1.0.0
required_open_webui_version: 0.5.0
description: A selectable "model" that renders the Hussh One feature catalog in the chat body. Pick it from the model dropdown to view all shipped features, surfaces, and contracts.

This is an Open WebUI **Pipe Function**. It registers one selectable entry in the
model picker ("🤫 Hussh One — Features"); opening it and sending any message (or
none) renders the feature catalog as markdown in the main chat body — the native
Open WebUI equivalent of a "Features page in the sidebar that loads the main
body component".

Upgrade-safe: lives in Open WebUI's function DB, not the prebuilt frontend
bundle, so a `pip install -U open-webui` won't wipe it. Source of truth for the
content is docs/hussh-one/features in the hussh-one-hermes repo; keep them in sync.
"""

from typing import List, Dict


FEATURES_MARKDOWN = """\
# 🤫 Hussh One — Features

> **hussh** = **Hu**man **S**ecure **S**ocket **H**ost. Overlay on Hermes Agent — a single, secure personal agent present across every surface. Every feature has a module, a config knob, a test, and a doc page.

## Three first-class surfaces
| Surface | What it is |
|---------|-----------|
| **TUI / Dashboard** | `hermes --tui` + the embedded real TUI in the web dashboard |
| **WhatsApp** | Branded, owner-gated personal agent with capsules |
| **Open WebUI** | This browser chat, over the OpenAI-compatible API server |

*All three run the same agent, router, and models.*

## 📱 WhatsApp Layer
- **Stacked brand header** — 3-line header (brand · model [A/S] · divider) on every send
- **Owner-only triggering** — injection-proof gating; strict `@One` tagging in groups and DMs
- **Multi-device (LID) auth** — authorizes your linked devices via JID/LID
- **Social-group capsules** — sandboxed: isolated memory, read-only toolset, no lateral sends
- **Anti-DOS rate limit** — non-owner capsule triggering with configurable rate caps
- **Clean output** — no reasoning/logs/jargon; bold-only; autopilot approvals
- **Local data & recovery** — WhatsApp history/media retrieval, message edit/recovery

## 🖥️ CLI / Web / API
- **CLI/TUI + Dashboard theming** — the `hussh-one` skin across terminal and web
- **Natural-language model switching** — "switch to opus 4.8" (deterministic, injection-safe)
- **Open WebUI browser chat variant** — full web chat over the OpenAI-compatible API server; 1 agent call per message
- **TUI model popover sync** — picker opens reflecting the live session model + active provider/model

## 🔒 Reliability
- **Session-model persistence & resume** — sessions keep their model across refresh / `--resume` / cold restart
- **Vertex-Claude pinning** — Claude always routes through GCP Vertex (ADC), never Anthropic-direct
- **Dashboard crash resilience (OOM-safe)** — compaction tuning + supervisor RSS soft-cap → clean restart, never SIGKILL
- **Open WebUI optimization** — title/tag generation off by default → 1 agent call per message

## 🚦 Deterministic contracts (A–K)
| | Invariant |
|---|---|
| **A** | Group routing safeguard |
| **B** | Zero-width unicode leakage |
| **C** | Upstream update guard |
| **D** | Dashboard real-TUI (not forked chat) |
| **E** | NL model switching (deterministic, injection-safe) |
| **F** | Capsule sandbox |
| **G** | Branding & header |
| **H** | Session-model resume |
| **I** | Dashboard crash resilience |
| **J** | TUI model popover sync |
| **K** | Open WebUI surface |

## 🟣 Built on Hermes Agent
- **Closed learning loop** — curated memory, autonomous skills, FTS5 cross-session recall
- **60+ tools** — file, terminal (6 backends), web/browser, media gen, orchestration
- **20+ platforms** — one gateway: CLI, Telegram, Discord, Slack, WhatsApp, and more
- **Multi-provider** — Nous Portal, OpenRouter, Vertex, Anthropic, Gemini, local + plugins

---
*Source of truth: `docs/hussh-one/features` in the hussh-one-hermes repo. Pick this entry from the model dropdown anytime to view the catalog.*
"""


class Pipe:
    """A selectable Open WebUI 'model' that renders the Hussh One feature catalog."""

    def __init__(self):
        # No valves needed — content is static and self-contained.
        self.id = "hussh_one_features"
        self.name = "🤫 Hussh One — "

    def pipes(self) -> List[Dict[str, str]]:
        # One selectable entry; OWU prefixes self.name → "🤫 Hussh One — Features".
        return [{"id": "features", "name": "Features"}]

    def pipe(self, body: dict) -> str:
        # Whatever the user sends, return the catalog. Rendered as markdown in
        # the main chat body — the "page loads in the body" behavior.
        return FEATURES_MARKDOWN
