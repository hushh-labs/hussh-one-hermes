# Xtreme Burst — payload transfer design

**Status: design, not implemented.** No code in `hermes_cli/hussh_one_burst/` moves a
workload today, and `run_burst` says so in its own docstring. This page exists so that when
it is built, it is built deliberately.

Getting a workload onto a burst instance is the one step in this capability that genuinely
moves a person's information off their machine. Everything before it — measurement,
placement, hardware choice, even provisioning — is resource numbers. This step is their
actual work. It therefore gets a consent design first and code second, which is the reverse
of how the rest of this feature was built and is intentional.

## What actually has to move

| Thing | Sensitivity | Notes |
|---|---|---|
| Input records | **Highest** | The person's own material. The reason this page is careful. |
| Model weights / checkpoints | High | Often derived from their records; a fine-tune is a lossy copy of the training set. |
| Code and config | Medium | May embed paths, names, credentials pasted by mistake. |
| Runtime environment | Low | Package lists, CUDA version. |
| Resource numbers | None | Already crosses today, in `InstanceSpec`, which carries no workload fields by construction. |

A test asserts `InstanceSpec`'s exact field set precisely so this line does not blur later.

## The trust boundary, stated honestly

The burst instance runs **in the person's own cloud project, under their own credentials,
billed to them.** Their information never reaches Hushh infrastructure. That is a real and
unusual property and it should be said plainly.

It is not a free pass. Three things remain true:

1. **Their cloud is not their device.** A GCP project has IAM, audit logs, other principals,
   and a support organisation. Moving records there widens the set of people who could
   reach them, even if Hushh is not among them.
2. **Hermes is the one choosing to move it.** "It's your own project" cannot become the
   reason not to ask.
3. **A burst instance is disposable and therefore easy to forget.** Anything staged for it
   outlives it unless teardown is explicitly extended — see *Teardown grows* below.

## Consent: what the person is actually agreeing to

Mirroring the canonical lifecycle already used by `hussh_one_pkm.mcp_server`
(`search-user-scopes` → `request-consent` → `check-consent-status` →
`get-encrypted-scoped-export`), with the burst-specific parts made explicit.

The approval prompt must name, in the person's terms:

- **what** is leaving the device — not "your workload" but "the 4,182 photos in this folder";
- **where** it is going — the project id and region, which they may not have chosen recently;
- **how long** it lives there, and that it is deleted at teardown;
- **what comes back**, and where the results land;
- **what it costs**, which they already see today.

The existing `hussh_burst_run` elicitation covers cost, hardware and destination. It does
not yet name the payload, because there is no payload. When there is, the prompt grows and
approval must be re-obtained — an approval given for "provision hardware" is not an approval
for "and send my records to it".

**Two consents, not one.** Placement (`decide`, `plan`) needs no consent because nothing
moves. Provisioning needs spend approval. Payload transfer needs record approval. Collapsing
the last two into a single yes is the failure mode to avoid: a person agreeing to $12 of
GPU time is not thereby agreeing to upload their medical scans.

## Mechanism

Follows the primitives already in the tree — AES-GCM with a purpose-bound AAD, exactly as
`hussh_one_pkm.crypto.envelope_aad` does it.

1. **Stage encrypted, client-side.** The device encrypts the payload with a fresh per-burst
   key (AES-GCM), AAD binding `{purpose: "hussh-one-burst-payload-v1", burst_id, project,
   user_id}`, and uploads ciphertext to a per-burst bucket in the person's project. The
   cloud storage layer never sees plaintext, so a misconfigured bucket ACL is a
   confidentiality *incident* rather than a confidentiality *failure*.
2. **Deliver the key out of band.** The per-burst key goes into Secret Manager in the same
   project, IAM-bound to the burst instance's service account only.
   **Not instance metadata** — metadata is visible in the console, appears in logs, and
   persists with the instance.
3. **Instance pulls, decrypts in memory, works.** It holds a key scoped to one burst and
   nothing else. It never receives a vault key, a refresh credential, or an owner capability
   — the same rule `hussh_one_pkm.mcp_server` states for the PKM bridge.
4. **Results return the same way**, encrypted under the same per-burst key, and the device
   decrypts locally.
5. **Teardown destroys all three**: instance, bucket, secret.

## Teardown grows — and its meaning changes

Today teardown releases one instance and the failure mode is a bill. With a payload staged,
an incomplete teardown leaves **the person's records sitting in a bucket** — a privacy
failure, not a cost one.

The current guarantee was just hardened for exactly this class of mistake: `teardown` used
to treat an accepted `DELETE` as proof of deletion, and a live burst showed it reporting
`torn_down: true` while the instance was still STAGING and billing. It now polls until
confirmed absent and reports a named leak otherwise. **Bucket and secret deletion must be
built to that same standard from the first commit** — confirmed absent, never
fire-and-forget — because the version of this bug that leaves records behind is much worse
than the version that leaves a GPU running.

Ordering matters: delete the payload **before** releasing the instance. An instance that
outlives its data is harmless; data that outlives its instance is the leak.

## What must never happen

- Plaintext records touching cloud storage, logs, instance metadata, or a receipt.
- The instance holding any credential beyond its one per-burst key.
- A single approval covering both spend and record transfer.
- Teardown reporting success it has not confirmed — the mistake already made once here.
- Workload contents reaching the placement decision. Placement is resource numbers only;
  that is the whole privacy argument and payload transfer must not quietly erode it.

## Rejected alternatives

| Option | Why not |
|---|---|
| Stream the payload directly to the instance over SSH/IAP | Requires the device to hold a long-lived path into the instance, and gives nothing to resume from if the spot instance is preempted. |
| Server-side encryption only (Google-managed keys) | Simpler, but then the cloud can read the records. Client-side encryption is what makes "your own project" defensible rather than merely true. |
| Reuse the person's vault key for payload encryption | Spreads the vault key beyond the device. Per-burst keys are cheap and disposable; the vault key is neither. |
| Key via instance metadata | Visible in console, captured in logs, persists with the instance. |
| Skip staging; have the instance pull from the person's existing storage | Attractive, and worth revisiting — but it grants the burst instance standing access to a live record store rather than a disposable copy, which is a much larger blast radius. |

## Open questions — these need a human

1. **Does payload transfer belong in Hermes at all, or in the pod?** Hermes is on the
   device and holds the records; the pod holds the person's consent state. Today the
   decision is on the device and the consent protocol is in the pod, and payload transfer
   needs both.
2. **What is the consent artifact?** A PCHP scope, a one-off elicitation, or a signed
   receipt from the existing consent MCP? This determines whether a burst is auditable
   alongside the person's other sharing.
3. **Spot preemption.** Spot instances are cheap and can vanish mid-job. Does a preempted
   burst resume, or restart? Resume implies durable intermediate state, which is more of
   the person's information at rest for longer.
4. **Payload size ceiling.** A 400GB training set moving over consumer upstream is hours of
   transfer for minutes of compute. There is a size above which bursting is the wrong answer
   and the honest response is to say so.

---

### Related
- [Architecture & migration record](./xtreme-burst.md)
- [Production-readiness scorecard](./xtreme-burst-roadmap.md) — KPI 2.5
