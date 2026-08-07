# Hushh One — Xtreme Compute Burst
### A technical white paper: personal supercomputing for Apple Silicon, with bring-your-own-cloud burst

**Status:** White paper — for solution engineers, forward-deployed engineers, and partners
**Last updated:** 2026-06-18 · **Audience:** technical · **Classification:** shareable

---

## 1. Abstract

Apple Silicon turned the Mac into a personal supercomputer — a unified-memory machine that can
run real AI and creative workloads on-device, privately, with no cloud round-trip. But every
device has a ceiling. When a workload outgrows the Mac — a larger model, a longer render, a
bigger batch — the user today faces a cliff: rewrite for the cloud, become a GCP engineer,
stand up infrastructure, and hand their data to someone else's account.

**Xtreme Compute Burst removes the cliff.** The user's One agent runs work on-device by default
("One Puppy"), continuously senses headroom, and — the moment the Mac can't perform — transparently
**bursts** the workload to the user's *own* cloud project, runs it on accelerator hardware, brings
the result home, and tears the cloud resources down. The user plugs in their cloud key once; they
never write a line of infrastructure, never learn a console, and never lose control of their keys
or their data. It feels like the Mac simply got bigger when it needed to.

This paper describes the architecture, the placement model, the security/privacy posture, the
agent-registry integration (Gemini Enterprise Agent Platform via the A2A protocol), and the
operational discipline behind it.

## 2. Design tenets

These are non-negotiable. Every decision below traces back to one of them.

1. **On-device first.** Local is the default and the fast path. The cloud is the exception, invoked
   only when the device can't deliver — never as a data grab.
2. **The user owns the compute and the data (BYOC).** Bursts run in the *user's* GCP project, on the
   *user's* bill, under the *user's* key. Hushh owns none of the compute and never persists the key.
3. **Zero expertise required.** Plug in a key once. One handles provisioning, accelerator selection,
   execution, result return, and teardown. No console, no YAML, no quotas to reason about.
4. **It just works, and it just got faster.** The product moment is invisible mechanics and a visible
   result: the spinning beachball becomes "done." (The Steve Jobs bar — see the macOS experience spec.)
5. **Secure and private by default.** Keys live in the Secure Enclave / Keychain, travel only over
   TLS, are used in memory and discarded. Workload data runs in the user's own cloud and is never
   retained by Hushh. (See the BYOC security & privacy spec.)
6. **Cost can never run away.** Every burst instance is torn down on completion, failure, and timeout.
   Orphaned-instance rate is an SLO with a hard ceiling. (See the SLO & observability spec.)
7. **Operate it like Google operates services.** SLIs/SLOs, error budgets, golden signals, structured
   events, traces, and a reconciliation sweep — not hope.

## 3. The experience, end to end

1. **Install** the One app on a Mac (App Store / signed notarized build). One detects the device
   profile (cores, unified memory, free disk, thermal headroom, network).
2. **Connect your cloud** — one screen, plain English: "Paste your Google Cloud key so One can borrow
   supercomputers when your Mac needs them. Your key stays on this Mac." Stored in Keychain.
3. **Work normally.** One runs workloads on-device. It watches memory pressure, swap, thermals, and
   sustained saturation.
4. **The burst moment.** When a workload won't fit or the Mac is throttling, One decides to burst,
   provisions an accelerator instance in the user's project, runs the containerized workload, streams
   live progress, returns the result, and deletes the instance. The user sees "One borrowed a
   supercomputer to finish this faster," then the result.
5. **Recovery is invisible.** If the app closes or the network drops, the job keeps running; One
   re-attaches and finalizes. Nothing is lost; nothing is left running.

## 4. System architecture

```
   ┌─────────────────────────────┐         ┌──────────────────────────────┐
   │  Mac — One Puppy (native)   │  TLS    │  One Control Plane (Cloud Run)│
   │  • device telemetry         │ ───────▶│  • A2A agent card (.well-known)│
   │  • on-device execution      │  Bearer │  • POST /api/one/burst (stream)│
   │  • Keychain GCP key vault   │  + BYOC │  • GET  /api/one/burst/[id]     │
   │  • burst trigger + handshake│◀─────── │  • POST .../[id]/puppy-result   │
   └─────────────────────────────┘  NDJSON │  • placement engine             │
                                            │  • ComputeBurstProvider (GCP)   │
                                            └───────────────┬─────────────────┘
                                                            │ Compute Engine REST
                                                            │ (user's BYOC token)
                                                            ▼
                                            ┌──────────────────────────────┐
                                            │  User's OWN GCP project        │
                                            │  • GPU instance (COS + startup)│
                                            │  • runs the user's container   │
                                            │  • result via guest attributes │
                                            │  • torn down after the job     │
                                            └──────────────────────────────┘
```

**Components**

| Component | Where | Responsibility |
|---|---|---|
| One Puppy agent | macOS (native) | Telemetry, on-device run, burst trigger, Keychain key vault, result reporting. *(Spec: one-puppy-macos-agent.md)* |
| Control plane | Cloud Run (`one`, `hushone-app`, us-central1) | Auth, placement decision, provider orchestration, streaming, recovery, persistence. |
| Placement engine | `src/lib/burst/placement.ts` | Decide on-device vs cloud; pick accelerator. *(Spec: placement-autoscale.md)* |
| Provider abstraction | `src/lib/burst/` `ComputeBurstProvider` | Pluggable cloud backend; GCP implemented, Azure/AWS/Neo next. |
| GCP provider | `src/lib/burst/providers/gcp.ts` | Provision GPU instance, run container, read result, tear down. |
| Persistence | Cloud SQL / Prisma `BurstJob` | Durable job state + timings; never credentials. |
| Discovery | `GET /.well-known/agent.json` | A2A Agent Card for the Gemini Enterprise Agent Platform. *(Spec: agent-registry-and-card.md)* |

## 5. The burst lifecycle (control plane)

1. **Authenticate & validate.** Firebase Bearer session (`verifyOneRequest`) identifies *who*; the
   request `byoc` block carries *whose cloud*. The `JobSpec` (image, command, env, accelerator kind +
   count, resource estimate, device profile) is validated.
2. **Decide placement.** `decidePlacement(estimate, device, kind)` → `puppy` or `gcp` (§6).
3. **Puppy path.** Persist the decision and return a handshake; the device runs locally and reports
   the outcome to `POST /api/one/burst/[id]/puppy-result`.
4. **Cloud path.** Resolve BYOC creds → provision a Compute Engine instance with `guestAccelerators`,
   `onHostMaintenance:"TERMINATE"`, a Container-Optimized OS image, and a startup-script that runs the
   workload container and publishes status/exit-code/result to the instance's **guest attributes**.
5. **Stream.** NDJSON frames (`start`/`progress`/`done`/`error`/`pending`) with a heartbeat keep the
   client live while the control plane polls instance status.
6. **Finalize + teardown.** On completion/failure/deadline, the instance is **always** deleted
   (idempotent; a 404 is success) and the `BurstJob` row is settled with timings.
7. **Recovery.** A dropped stream resumes at `GET /api/one/burst/[id]`; a job stuck "running" past a
   safety window is self-healed to failed and any instance reconciled away.

## 6. Placement: the One Puppy decision

On Apple Silicon, GPU memory and host RAM are one **unified** pool, so the binding requirement is
`max(vramGb, unifiedMemoryGb)` against the device's unified memory. A workload stays **local** when it
fits under an 80% safety budget of both unified memory and free disk; otherwise it **bursts**. TPU
workloads and offline/over-pressure conditions always burst. Beyond the static fit-check, One promotes
an in-flight workload to a burst when live device-pressure signals (memory pressure, swap, thermal
throttling, sustained saturation, ETA blow-out) cross thresholds — with hysteresis so it never flaps.
The formal model, sizing tables, and cost guardrails are in the placement & autoscale spec.

## 7. Security & privacy (summary)

- **Keys never persist.** The BYOC service-account key is used in memory to mint a short-lived token
  and is never written to disk or DB. The `BurstJob` row stores only `projectId`, `region`,
  `credsSource`. On device, the key lives in the Keychain (Secure Enclave where available).
- **Two-credential model.** A request needs both a Hushh session (who) and a BYOC cloud credential
  (whose cloud) — declared in the A2A card's `security` as a logical AND.
- **Least privilege.** The BYOC SA needs only instance create/get/delete + zone-operation read + image
  pull; a dedicated, single-project, custom-role SA is recommended.
- **Data stays with the user.** Workload payloads run in the user's own project; Hushh retains no
  workload content. Blast radius of a leaked key is bounded to the user's own project.
- **No lingering data.** Guaranteed teardown means no instance (and no data on it) outlives the job.

Full trust boundaries, STRIDE model, data-at-rest inventory, IAM, and the hardening roadmap
(Secret Manager + KMS, Workload Identity Federation, VPC-SC) are in the BYOC security & privacy spec.

## 8. Agent-registry integration

One is published to the **Gemini Enterprise Agent Platform** (formerly Vertex AI Agent Builder; with
Agent Engine + the Cloud API Registry). Discovery uses the **A2A protocol Agent Card** served at
`GET /.well-known/agent.json` (RFC 8615): identity, skills (`burst-compute`, `placement-advice`),
endpoint, streaming capability, and the two required security schemes. The same capability is exported
as a Gemini function-calling tool and an OpenAPI 3.1 contract so it can be imported as a managed tool.
Packaging and submission steps are in the agent-registry & card spec and the FDE playbook.

## 9. Reliability & operations

The control plane is operated to explicit SLOs: submit availability, time-to-first-progress, provision
latency, burst success rate, **orphaned-instance rate (hard ceiling near zero)**, teardown success
rate, and cost-per-successful-burst. Every lifecycle step emits a structured `one.burst.*` event (never
secrets, never workload content) and a trace span; a reconciliation sweep lists `hushh-burst`-labeled
instances and deletes any without a live job. Details: SLO & observability spec.

## 10. Verification & current status

- **Implemented & tested (this repo):** placement engine, BYOC credential resolution + token-client
  caching, **GPU path (Compute Engine) and TPU path (Cloud TPU API)**, mock provider, streaming submit +
  recovery routes, the A2A agent-card endpoint, the Puppy result callback, the in-app "2-minute GCP
  setup" validation flow, the static registry artifacts, `BurstJob` persistence — **150+ burst tests,
  high line coverage** (see the test plan).
- **Native + roadmap (next to build/harden):** the native macOS One Puppy agent (spec'd, SwiftPM
  scaffold under `macos/`); Secret-Manager-backed credential storage + Workload Identity Federation;
  Azure/AWS/Neo-cloud providers.
- **Requires customer inputs to exercise live:** a GCP project with Compute Engine API + GPU quota, a
  least-privilege SA key, a pullable container image (see the FDE playbook).

## 11. Roadmap

| Phase | Scope |
|---|---|
| GA v1 | GCP **GPU + TPU** burst + One Puppy placement + agent-registry listing + in-app GCP setup (this work). |
| v1.1 | Secret-Manager/KMS credential vault; Workload Identity Federation (keyless BYOC). |
| v1.2 | Accelerator auto-sizing from learned cost/perf; TPU topology selection. |
| v2 | Azure, AWS, and Neo-cloud providers via `ComputeBurstProvider`; cross-cloud price/perf routing. |

## Related documents
- Feature overview: docs/xtreme-compute-burst.md
- Test plan & verification: docs/xtreme-compute-burst-test-plan.md
- Agent registry & A2A card: docs/specs/agent-registry-and-card.md
- macOS One Puppy agent: docs/specs/one-puppy-macos-agent.md
- macOS experience (UX bar): docs/specs/macos-experience.md
- Placement & autoscale: docs/specs/placement-autoscale.md
- BYOC security & privacy: docs/specs/byoc-security-privacy.md
- SLO & observability: docs/specs/slo-observability.md
- Forward-deployed engineer playbook: docs/runbooks/forward-deployed-engineer-playbook.md
- API contract: docs/specs/burst-control-plane.openapi.yaml
