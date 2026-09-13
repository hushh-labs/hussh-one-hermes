# Xtreme Compute Burst — Test Plan & Verification

This document is the map for verifying the Xtreme Compute Burst feature (see
`docs/xtreme-compute-burst.md` for the feature itself). It enumerates **every test**, what
each one proves, the **user journeys** (happy paths + edge cases) they cover, the
**performance / memory-leak audit** and the fixes it produced, and the **coverage** result
with an honest account of what is intentionally left uncovered.

## 1. How to run

```bash
# All burst tests
npm run test:burst

# Burst tests with a per-file coverage report (text)
npm run test:burst:coverage

# Whole repo (burst tests included)
npm run test
npm run typecheck      # tsc --noEmit — 0 errors
npx eslint src/lib/burst src/app/api/one/burst src/lib/db/burst-store.ts   # 0 errors
```

## 2. Results at a glance

- **95 tests across 11 files, all passing.** (Whole-repo suite: 260 passing.)
- `tsc --noEmit`: **0 errors**. ESLint on the burst tree: **0 errors / 0 warnings**.
- Coverage of the burst code (v8):

| Area | Stmts | Branch | Funcs | Lines |
|---|---|---|---|---|
| `src/lib/burst/*` (types/placement/credentials/client/factory/lru) | 98.9% | 95.8% | 100% | 100% |
| `src/lib/burst/providers/*` (gcp, mock) | 96.8% | 87.2% | 89.7% | 99.1% |
| `src/lib/db/burst-store.ts` | 89.5% | 80.4% | 100% | 97.6% |
| `src/app/api/one/burst/*` (POST + recovery routes) | ~90% | ~75% | ~67% | ~94% |
| **Total** | **94.0%** | **82.4%** | **85.4%** | **97.3%** |

The residual uncovered lines are catalogued in §6 — they are timer callbacks, defensive
null-guards, and process-fatal branches, each with a stated reason.

## 3. Performance & memory-leak audit

The code was reviewed for the things that bite at scale (a process serving many tenants,
long-lived, polling tight loops). Findings and the fixes now in the code:

| # | Risk | Fix | Proven by |
|---|---|---|---|
| 1 | **Token re-mint per poll.** `mintAccessToken` built a *new* `JWT`/`GoogleAuth` on every call, so a 5s status-poll loop re-signed a JWT (or re-read ADC) every tick — pure CPU/IO waste across millions of polls. | Cache the auth **client** per credential and reuse it; the library then serves the in-memory token and only refreshes near expiry. (`credentials.ts`) | `credentials.test.ts`: "builds the JWT once and reuses it across calls", "builds the ADC client once and reuses it". |
| 2 | **Unbounded auth-client cache.** A naïve per-tenant cache would retain a `JWT` per distinct BYOC service account forever → unbounded heap growth. | Back it with a fixed-capacity **`BoundedLru`** (default 256, `ONE_BURST_AUTH_CACHE_SIZE`); cold tenants evict, memory stays flat. | `lru.test.ts`: "never grows beyond maxSize", LRU eviction order. |
| 3 | **Unbounded mock-job table.** The mock provider kept every job in a `Map`; an abandoned job (never polled to completion / torn down) leaked forever. | Same `BoundedLru` (default 1024, `ONE_BURST_MOCK_MAX_JOBS`). | `mock.test.ts`: "bounds the in-flight job table (abandoned jobs cannot leak)". |
| 4 | **Leaked abort timer.** `callGcp` set an `AbortController` timeout but never cleared it on the success path → a dangling timer per request (also flagged by lint). | `clearTimeout` in a `finally`. (`providers/gcp.ts`) | Lint clean; exercised by every `gcp.test.ts` call. |
| 5 | **Leaked heartbeat interval.** The streaming route's 7s heartbeat `setInterval` had to be cleared on *every* exit (done / error / deadline / client-disconnect) or it would keep firing on a closed controller. | `finish()` clears it in a `finally`; `cancel()` clears it on client disconnect; `send()` is a no-op once `closed`. (`route.ts`) | `route.test.ts`: "stops cleanly when the client disconnects mid-stream". |
| 6 | **Orphaned cloud instance = runaway cost.** A burst VM must never outlive its job. | `teardownBurst` runs on **completion, failure, and the soft-deadline handoff**, is idempotent (404 = already-gone), and never throws (so cleanup can't mask the real error). | `route.test.ts` (teardown on completion / failure / deadline), `gcp.test.ts` (404 idempotent), `client.test.ts` (never throws). |

Design choices that keep it efficient: native `fetch` for all REST (no heavyweight client),
the startup-script self-deletes the VM (no controller polling cost after handoff), guest
attributes carry results (no extra service/ingress), and the credential key material is held
only in memory (no encrypt/decrypt per request).

## 4. Test inventory — what every test proves

### `src/lib/burst/lru.test.ts` — the memory-bound primitive (6)
- stores/retrieves; **never grows beyond `maxSize`** (10k inserts → size 100); LRU evicts
  oldest first; re-set refreshes recency; delete/clear; rejects capacity < 1.

### `src/lib/burst/credentials.test.ts` — BYOC resolution + token caching (11)
- `resolveGcpCreds` precedence: per-request SA → env SA → ADC; request `projectId/region`
  override; region defaults to `us-central1`; **400** on malformed JSON; **400** on missing
  fields; **503** when no project is determinable.
- `mintAccessToken`: **client built once and reused** for SA and for ADC (the perf fix);
  **502** when the client yields no token.

### `src/lib/burst/placement.test.ts` — the "One Puppy" decision (9)
- Local when memory+disk fit under the 0.8 safety budget (boundary); burst when accelerator
  **memory** exceeds budget; burst when **disk** exceeds budget; **TPU always bursts**; burst
  when **offline**; burst on **unknown/degenerate** estimate; **unified memory** (not just
  vram) is the binding constraint on Apple Silicon; headroom is reported; kind defaults to gpu.

### `src/lib/burst/provider-factory.test.ts` — provider selection (5)
- `mockBurstEnabled` tracks the env flag; gcp by default; mock when asked; **mock forced** when
  `ONE_ENABLE_MOCK_BURST=true` even for "gcp"; unknown id → **400**.

### `src/lib/burst/providers/mock.test.ts` — the simulator (4)
- provisioning → running(%) → completed over the simulated duration; `fail://` → failed with
  exit 1; unknown job after teardown (no dangling state); **bounded job table**.

### `src/lib/burst/providers/gcp.test.ts` — the real GCP path (19)
- **provision**: builds `instances.insert` with `guestAccelerators` (type+count),
  `onHostMaintenance:"TERMINATE"`, startup-script metadata, `hussh-burst` labels; **env vars**
  become `docker -e` flags and command args are quoted; honors **explicit zone + custom machine
  type**; **TPU → routed to the Cloud TPU API path; 503 without a result bucket**; **requires creds (503)**.
- **retry**: transient **503 retried** then succeeds; non-transient **403 not retried**.
- **pollStatus**: guest-attrs **404 → provisioning**; running; completed (result + exit code);
  failed (exit code in error); unrecognized marker → provisioning; requires creds; **non-404
  propagates**.
- **teardown**: issues DELETE and resolves on 200; **404 = already-gone**; no-op when disabled
  via env; no-op when no instance; **non-404 propagates**.

### `src/lib/burst/client.test.ts` — the route-facing wrapper (4)
- `startBurst` provisions then submits, **passing creds through** for a cloud provider; **nulls
  creds for the mock provider**; `pollBurst` delegates with opts; **`teardownBurst` never throws**.

### `src/lib/burst/client.integration.test.ts` — real layer, no mocks (2)
- A too-big workload bursts, runs to completion via the real mock provider, then tears down; a
  `fail://` image surfaces as failed. (Proves the wiring end-to-end without GCP.)

### `src/lib/db/burst-store.test.ts` — persistence (10)
- No DB → null/no-op everywhere; create is **user-scoped** and the persisted spec **carries no
  credential material**; unknown user → null; mark/complete/fail write status+timing; owned-job
  lookup is user-scoped; tolerates **P2021** (no table) and **P2022** (no column); **re-throws**
  an unexpected DB error; lookup error → null; null job id → skip.

### `src/app/api/one/burst/route.test.ts` — POST (submit) (13)
- Cloud burst streams to **done** + teardown + complete; failure → **teardown + fail** + error
  frame; small workload → **Puppy** (never provisions); missing image → **400**; bad
  `acceleratorCount` → **400**; unauthorized → **401**; invalid JSON → **400**; **start frame**
  names placement+provider; cloud burst with no resolvable creds → **503**; **per-request BYOC
  creds** flow to the customer's cloud; **running progress** then done; **soft-deadline handoff**
  tears down + fails + `pending`; **client disconnect** stops cleanly (no teardown — recovery owns it).

### `src/app/api/one/burst/[id]/route.test.ts` — GET (recovery) (12)
- Completed → saved result; running → **resume, complete, teardown**; **stale → self-heal fail**;
  unauthorized → **401**; unknown id → **404**; failed → saved error; **Puppy → running**
  (out-of-band); no provider job id → running (no poll); resumed poll failed → **teardown + fail**;
  poll throws → running (no crash); **concurrent finalize deduped** (no double complete/teardown);
  real-cloud resume without env/ADC creds → running.

## 5. User-journey coverage matrix

| Journey | Happy path | Edge / failure cases covered |
|---|---|---|
| **Submit a workload** | Puppy (fits) → handshake; Cloud (too big) → provision+stream+done | bad image/count (400), bad JSON (400), unauthorized (401), no creds (503) |
| **BYOC** | per-request SA JSON → burst in customer project; env SA; ADC | malformed JSON (400), missing fields (400), no project (503) |
| **Placement** | local when it fits | memory-bound, disk-bound, TPU, offline, unknown estimate, unified-memory binding |
| **Run lifecycle** | provisioning→running→completed | workload failure, deadline handoff, client disconnect |
| **Cost control (teardown)** | on completion | on failure, on deadline, idempotent 404, never-throws, disabled-via-env |
| **Recovery** | resume running → complete | stale self-heal, unknown 404, already-completed/failed, no-job-id, poll error, concurrent dedupe, no-creds |
| **Durability** | persist create/provisioned/complete/fail | no-DB no-op, unmigrated table/column tolerance, unexpected error re-throw |
| **Scale/perf** | token reuse, bounded caches | LRU eviction proven, abandoned-job eviction proven |

## 6. Intentionally uncovered lines (and why)

Coverage is 97.3% lines / 94.0% statements. The remainder is deliberately not unit-tested:

- **`route.ts` (POST):** the 7-second heartbeat `setInterval` body, and one `.catch(() =>
  undefined)` arrow on the deadline-path `failBurstJob`. The heartbeat would require a >7s
  wall-clock test for no behavioral gain; its cleanup is covered by the disconnect test.
- **`[id]/route.ts`:** the `fresh.status === "failed"` re-check arm of the concurrent-finalize
  race (the `completed` arm is tested), and the outer catch's non-401 status arm.
- **`providers/gcp.ts:55`:** `errorStatus` returning `null` for a non-object throw.
- **`client.ts:59-60` / `lru.ts:28` / `mock.ts:12`:** the teardown-error log object fields (the
  catch itself is covered), an unreachable `break` guard, and the env-default fallback for the
  mock duration.
- **`burst-store.ts:63`:** the `create` catch re-throw for a non-schema error (the analogous
  `update` re-throw IS tested).

None of these change a user-visible outcome; they are guards, logging fields, and timer bodies.

## 7. Exercising the real GCP path

Unit tests stub the network. To validate against real GCP (the only part this harness cannot
self-verify), provide what only you can — see `docs/xtreme-compute-burst.md` §"Real GCP path":
a project with Compute Engine API + GPU quota, an SA JSON with `roles/compute.instanceAdmin.v1`,
a pullable container image, then `npm run db:deploy` and POST with `ONE_ENABLE_MOCK_BURST=false`.
The provisioned instance, the guest-attribute result handshake, and the teardown are the
integration surface to watch (the request-body shape, retry, and teardown logic are already
unit-proven here).
