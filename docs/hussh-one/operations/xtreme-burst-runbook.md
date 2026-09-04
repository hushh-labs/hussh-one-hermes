# Xtreme Burst — operations runbook

How to run a burst, how to prove it was released, and what to do when it wasn't.

Every step here was exercised against `hushh-pda-dev` on 2026-08-08. Where a check exists,
it is because skipping it broke something real that day.

## Before anything: is the fleet clean?

Dev is shared and costed. Check what is already running before adding to it.

```python
from hermes_cli.hussh_one_burst.credentials import resolve_credentials
from google.auth.transport.requests import AuthorizedSession

P = "hushh-pda-dev"
creds, _ = resolve_credentials(project=P, region="us-central1")
r = AuthorizedSession(creds).get(
    f"https://compute.googleapis.com/compute/v1/projects/{P}/aggregated/instances",
    timeout=60,
)
live = [i["name"] for _z, b in (r.json().get("items") or {}).items()
        for i in (b.get("instances") or [])]
print(len(live), live)
```

Run this **before and after** every burst. "After" is not optional — it is the only
independent check that teardown did what the receipt says.

## Pre-flight — four things that will fail a burst

Each of these was hit for real. None cost money because they were checked first; all four
would have cost money if they had not been.

| Check | Why | Symptom if skipped |
|---|---|---|
| **Boot image family exists** | Google retires Deep Learning VM families as CUDA ages | `404` at provision. The first value shipped in `providers.py` was already gone. |
| **Accelerator quota** | Spot quota is separate from on-demand | `QUOTA_EXCEEDED`. Look for `PREEMPTIBLE_NVIDIA_*` — the provider uses SPOT. |
| **Zone has the accelerator** | Not every zone in a region carries every part | `ZONE_RESOURCE_POOL_EXHAUSTED` or an invalid `acceleratorType` |
| **Credentials resolve to the right project** | The SA key's own `project_id` is not necessarily the target | A burst provisioned in the wrong project, billed to the wrong team |

```python
# image family — the one that bit us
s.get("https://compute.googleapis.com/compute/v1/projects/"
      "deeplearning-platform-release/global/images/family/<family>")   # want 200

# spot quota, in the region
s.get(f".../projects/{P}/regions/us-central1").json()["quotas"]         # PREEMPTIBLE_NVIDIA_T4_GPUS

# does this zone carry the part?
s.get(f".../projects/{P}/zones/us-central1-a/acceleratorTypes")

# which project will this key actually use?
creds, ref = resolve_credentials(project=P, region="us-central1")
print(ref.project, ref.region, ref.source)                              # never print creds
```

## Running one

Default provider is `mock` — real spend is opt-in, and that is deliberate.

```python
from hermes_cli.hussh_one_burst.providers import GcpBurstProvider
from hermes_cli.hussh_one_burst.execution import BurstRequest, run_burst

prov = GcpBurstProvider(project="hushh-pda-dev", region="us-central1",
                        teardown_confirm_seconds=420, teardown_poll_seconds=10)
receipt = run_burst(
    BurstRequest(label="smoke", accelerator_id="nvidia-t4", chip_count=1,
                 usd_per_hour=0.35, deadline_minutes=5.0),
    prov,
)
print(receipt.as_dict())
assert not receipt.leaked_instance
```

The cheapest shape that proves the whole path is **1× T4 → `n1-standard-8`**, about
$0.35/hour on spot. A full lifecycle costs well under a cent. Prove the path with that
before running anything expensive.

## Proving teardown — read this before trusting a receipt

`instances.delete` returns a **long-running Operation**. A 2xx means the request was
accepted, not that anything has been released.

This is not hypothetical. On the first live burst, `teardown` reported
`torn_down: true` while the instance was still **STAGING with a T4 attached and billing**.
It deleted about ninety seconds later, so nothing leaked — but the receipt had already
claimed a release that had not happened. Every mock-based test passed while this was true,
because a mock deletes synchronously.

`teardown` now polls until the instance 404s, and `teardown_confirm_seconds` bounds the
wait. **Still verify independently** — the receipt and the cloud are two different
witnesses:

```python
r = session.get(f".../projects/{P}/zones/{zone}/instances/{receipt.instance_id}")
assert r.status_code == 404          # 404 is the only proof
```

A `200` here with status `STAGING`, `RUNNING` or `STOPPING` means it is still there.
`STOPPING` is fine and resolves; `STAGING` or `RUNNING` minutes later is not.

## When teardown fails

`receipt.leaked_instance` is `True` whenever something was provisioned and could not be
confirmed gone. `as_dict()` carries a `warning` naming the instance and where to look.

1. **Delete it by hand, now.** It bills by the hour.
   ```
   DELETE .../projects/<project>/zones/<zone>/instances/<instance_id>
   ```
2. Poll until `404`. Do not assume the delete took.
3. Sweep for anything labelled `app=hussh-one-burst` — the label exists for exactly this.
4. Only then work out why confirmation failed.

A burst whose teardown could not be confirmed is an **incident**, not a warning. The cost
is unbounded until someone looks.

## When payload transfer lands, this section grows

Today an incomplete teardown leaves a bill. With a payload staged it will leave *the
person's records in a bucket* — a privacy failure, not a cost one. Bucket and secret
deletion must be built to the same "confirmed absent" standard, and the payload must be
deleted **before** the instance. See
[the payload transfer design](../architecture/xtreme-burst-payload-transfer.md).

## Cost sanity

- Quoted rates are **modeled** (`ACCEL_CATALOG` in `hardware.py`), not fetched. Treat them
  as estimates, never quotes.
- The largest parts sell only as whole nodes — H100/H200/B200 as 8 GPUs, GB200 NVL as 4.
  `sellable_chips` encodes this so a quote matches a bill; if you edit prices, do not
  edit that.
- Spot instances are cheap and **can vanish mid-job**. Fine for a smoke test, a real
  consideration for anything long.
- `maxRunDuration` + `instanceTerminationAction: DELETE` is a second brake behind
  teardown, not a replacement for it.

---

### Related
- [Architecture & migration record](../architecture/xtreme-burst.md)
- [Production-readiness scorecard](../architecture/xtreme-burst-roadmap.md)
- [Payload transfer design](../architecture/xtreme-burst-payload-transfer.md)
