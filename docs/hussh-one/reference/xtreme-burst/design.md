# Hussh One — Xtreme Compute Burst (GCP + "One Puppy")

> **Customer onboarding:** [docs/customer/getting-started.md](./customer/getting-started.md) ·
> **Provision your cloud (script/Terraform):** [provisioning/README.md](../provisioning/README.md) ·
> **Engineering docs (white paper, specs, OpenAPI, FDE runbook):** [docs/specs/README.md](./specs/README.md).

The flagship **Xtreme Compute Burst** agent runs a heavy workload wherever it runs best:
on the user's local Mac (**"One Puppy"** — a personal supercomputer) when it fits, or burst
to the cloud (**GCP** in v1) when it doesn't. It is **BYOC** (bring-your-own-cloud): the burst
runs in the *customer's* GCP project, using credentials they supply, and the customer's
service-account key is **never persisted**.

This is an MVP. The GPU burst path is real and runnable; the on-device Puppy execution and
the non-GCP clouds are documented contracts / next steps (see the end of this doc).

## Flow

```
POST /api/one/burst
  → validate JobSpec (image, accelerator, resource estimate, device profile)
  → decidePlacement(estimate, device, acceleratorKind)
       ├─ "puppy" → record decision, return a handshake (Mac agent runs it locally)
       └─ "gcp"   → resolve BYOC creds → provision GPU instance → stream NDJSON progress
                     → on completion/failure/deadline: TEAR DOWN the instance + persist
GET /api/one/burst/[id]   → recover/resume a dropped stream (owner-scoped)
```

### Placement — the "One Puppy" tier
`src/lib/burst/placement.ts` decides where a job runs (pure, fully tested):
- Puppy **offline** → burst to GCP.
- **TPU** workload → burst to GCP (Apple Silicon has no TPU).
- GPU job whose memory + disk fit under **80%** of the Mac's unified memory / free disk → **Puppy**.
- Otherwise → burst to GCP, naming the binding constraint.

`DEFAULT_PUPPY_PROFILE` models a maxed Mac Studio (M3 Ultra, 192 GB unified). The client sends a
partial `deviceProfile` snapshot of the actual Mac; gaps fill from the reference profile.

### Cloud burst — GCP
`src/lib/burst/providers/gcp.ts` (real path) provisions a Compute Engine instance with an attached
GPU (`guestAccelerators`, `onHostMaintenance: TERMINATE`), runs the workload container via a
startup-script, reads the result back from the instance's **guest attributes**, and **deletes the
instance** to control cost (idempotent — a 404 on delete is treated as already-gone). Teardown runs
on completion, failure, **and** the soft deadline, so a burst instance is never orphaned.

Why Compute Engine (not Cloud Batch / Vertex): one well-understood REST surface, explicit teardown
for cost control, and it mirrors the team's existing `services/*/scripts/gcp-vm` provisioning. The
`ComputeBurstProvider` interface isolates this choice so Cloud Batch is a drop-in later.

## BYOC credentials
`src/lib/burst/credentials.ts` resolves creds with this precedence:
1. **Per-request** service-account JSON (`byoc` block on the POST body) — the BYOC happy path. Held
   in memory only; **never written to the DB** (the `BurstJob` row stores `projectId`, `region`, and
   `credsSource` for audit — never the key).
2. `BYOC_GCP_SERVICE_ACCOUNT_JSON` env (dogfood / single-tenant).
3. Application Default Credentials (the Cloud Run runtime SA).

`google-auth-library` is used **only** to mint a short-lived `cloud-platform` access token; every
Compute Engine call is native `fetch`.

**Production hardening (not built in the MVP):** store an encrypted credential *reference* in Secret
Manager (KMS envelope) keyed per user and resolve it by ref; this also lets the recovery route resume
a per-request burst (see limitation below).

## Environment variables
| Var | Purpose | Default |
|---|---|---|
| `ONE_ENABLE_MOCK_BURST` | Force the mock provider (no GCP) | unset (false) |
| `BYOC_GCP_SERVICE_ACCOUNT_JSON` | **Secret** — fallback BYOC SA JSON | unset → ADC |
| `BYOC_GCP_PROJECT_ID` | Default burst project | unset |
| `BYOC_GCP_REGION` | Default region | `us-central1` |
| `ONE_BURST_DEFAULT_MACHINE_TYPE` | Host machine type | `n1-standard-8` |
| `ONE_BURST_DEFAULT_GPU_TYPE` | GPU accelerator type | `nvidia-tesla-t4` |
| `ONE_BURST_TPU_RESULT_BUCKET` | **Required for TPU** — GCS bucket the TPU node writes results to | unset → TPU 503 |
| `ONE_BURST_DEFAULT_TPU_TYPE` | TPU accelerator type | `v5litepod-8` |
| `ONE_BURST_TPU_RUNTIME` | TPU runtime version | `tpu-ubuntu2204-base` |
| `ONE_BURST_TEARDOWN` | `false` leaves the instance up (debug) | `true` |
| `ONE_BURST_TIMEOUT_MS` / `ONE_BURST_STATUS_TIMEOUT_MS` | REST call timeouts | 60000 / 25000 |
| `ONE_BURST_RETRIES` | Transient-error retries | 2 |
| `ONE_BURST_MOCK_DURATION_MS` | Simulated run length (mock) | 1200 |

Auth reuses the app's `ONE_ENABLE_DEV_AUTH` / `DEV_TOKEN`.

## Verify it end-to-end

**Mock mode (no GCP creds):**
```bash
export ONE_ENABLE_MOCK_BURST=true ONE_ENABLE_DEV_AUTH=true
npm run dev
# Estimate exceeds the 192GB Puppy → bursts to the mock cloud (NDJSON: start → progress → done)
curl -N -X POST localhost:3000/api/one/burst \
  -H "Authorization: Bearer DEV_TOKEN" -H "Content-Type: application/json" \
  -d '{"image":"busybox","acceleratorKind":"gpu","acceleratorCount":1,
       "estimate":{"vramGb":300,"unifiedMemoryGb":300,"vcpus":16,"diskGb":100,"estimatedMinutes":30},
       "deviceProfile":{"online":true,"unifiedMemoryGb":192}}'
# Drop the estimate under ~153GB → JSON {"placement":"puppy", ...}
# Then recover by id:
curl localhost:3000/api/one/burst/<burstJobId> -H "Authorization: Bearer DEV_TOKEN"
```

**Tests:** `npm run test` (see `src/lib/burst/**` and `src/app/api/one/burst/**`), `npm run typecheck`.

**Real GCP path — what you must provide:**
- A GCP project with the **Compute Engine API enabled** and **GPU quota** in the target region/zone.
- A **service-account JSON** with `roles/compute.instanceAdmin.v1` (create/get/delete instances).
- A **container image** the VM can pull (public, or Artifact Registry with VM SA access).
- Apply the migration: `npm run db:deploy` (adds the `BurstJob` table).
- Set `ONE_ENABLE_MOCK_BURST=false` and `BYOC_GCP_*` (or pass a per-request `byoc` block), then POST
  the same shape as above. The instance is provisioned, runs the container, and is torn down.

## Limitations / next steps
- **TPU** — implemented on the real path via the **Cloud TPU API** (node create → run → delete), with
  the result returned through a GCS object. Requires `ONE_BURST_TPU_RESULT_BUCKET`; without it a TPU
  burst returns a clear **503**. Fully simulated in mock mode. (GPU uses Compute Engine guest attributes.)
- **Recovery of a per-request BYOC burst** — creds aren't stored, so `GET /api/one/burst/[id]` can
  resume a poll only with env/ADC creds; a per-request burst reports `running` until its own stream
  finalizes it. The Secret-Manager credential-ref hardening above removes this limit.
- **One Puppy native agent** — the Mac-side execution + result callback
  (`POST /api/one/burst/[id]/puppy-result`) is a defined contract, not built here.
- **Other clouds** — Azure / AWS / Neo clouds plug in via `ComputeBurstProvider`; only GCP is implemented.
