# Hussh One Deployment

Use this flow for a fresh machine that should run the Hussh One variant by default.

```bash
git clone https://github.com/hushh-labs/hussh-one-hermes.git
cd hussh-one-hermes
git remote add upstream https://github.com/NousResearch/hermes-agent.git

scripts/hussh-one-bootstrap.sh --manager auto --start
scripts/hussh-one-doctor.sh --require-services
```

The bootstrap creates or updates `.venv`, installs Hermes dependencies, builds the TUI/dashboard assets when Node is available, sets the Hussh One config defaults, checks Google Application Default Credentials without printing tokens, and reports whether WhatsApp pairing is still needed on this machine.

## Supervisor Policy

`scripts/hussh-one-supervisor.sh` owns dashboard and gateway lifecycle through one manager:

- macOS: `launchd`
- Linux host: user `systemd`
- s6/container: existing container supervisor services when present
- fallback: `screen`

The dashboard service runs `hermes dashboard --tui --no-open` on `127.0.0.1:9119`. The gateway/WhatsApp bridge keeps its health endpoint on `127.0.0.1:3000/health`. The supervisor refuses mixed screen/service-manager state unless `--clean-conflicts` is passed.

## Daily Commands

```bash
scripts/hussh-one-supervisor.sh status
scripts/hussh-one-supervisor.sh restart
scripts/hussh-one-doctor.sh --require-services
scripts/hussh-one-guard.sh
```

Before merging official Hermes updates:

```bash
git fetch upstream main --tags
git switch main
git branch "backup/hussh-one-before-upstream-$(date +%Y%m%d-%H%M%S)"
git merge --no-ff upstream/main
scripts/hussh-one-guard.sh
```

Keep secrets in `$HERMES_HOME/.env` or your shell. `.env.example` only documents non-secret Vertex selectors such as `GOOGLE_CLOUD_PROJECT`, `GCP_PROJECT`, and `GOOGLE_CLOUD_LOCATION`.

---

## Developer Onboarding & Multi-Agent Integration Reference

To ensure that other developers and automated agents (such as Salesforce or MuleSoft integrations) can configure and query Gemini models (`gemini-3.5-flash` / `gemini-2.5-flash`) out-of-the-box, developers must understand the boundary and endpoint routing for Google's dual-platform architecture:

### 1. Developer Platform (Google AI Studio)
*   **Platform / Provider:** `gemini`
*   **Model ID:** `gemini-3.5-flash` (or `gemini-1.5-flash`)
*   **Canonical Endpoint Base URL:** `https://generativelanguage.googleapis.com/v1beta`
*   **Authentication:** Single, static API key (`AIzaSy...`). Passed as the query parameter `?key=...` or in request headers.
*   **Best For:** Fast local prototyping, standalone agent scripts, and lightweight third-party connectors.

### 2. Enterprise Cloud Platform (Google Cloud Vertex AI)
*   **Platform / Provider:** `google-vertex` (Gemini) / `google-vertex-claude` (Claude)
*   **Model ID:** `publishers/google/models/gemini-3.5-flash`
*   **Canonical Endpoint Base URL:**
    `https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{region}/publishers/google`
*   **Authentication:** GCP OAuth 2.0 Bearer tokens generated dynamically via **Application Default Credentials (ADC)** or GCP Service Account JSON keys.
*   **Best For:** Enterprise-grade security, production deployment pipelines, corporate auditing, VPC network constraints, and robust multi-agent platforms.

### 3. Local / Offline Mode (Air-Gapped Edge Compute)
When running in an air-gapped local environment (`DB_OFFLINE=1` or local-first configuration), the workload router replaces online APIs with highly optimized local open-weights models running over **Ollama** or a local **LM Studio** server:
*   **General / Light Tasks (Low Complexity):** **`Gemma 4 26B`** (e.g., `gemma-4-26b-a4b-it`). Acting as the fast local baseline, it provides exceptional natural language capabilities and fast turnaround for non-technical queries.
*   **Coding / Complex Tasks (High Complexity):** **`Qwen 3.6 35B`** (e.g., `qwen3.6-35b-instruct` / `qwen2.5-coder:32b`). Tunneling as the local "Claude Opus," it provides state-of-the-art code generation, precise file editing, system terminal capabilities, and robust local tool execution.

*Note: By setting `GOOGLE_GENAI_USE_VERTEXAI=true` and configuring your active project ID, our system's built-in adapters dynamically map all internal requests to the Vertex AI enterprise API, ensuring other downstream agents can boot and authenticate seamlessly.*
