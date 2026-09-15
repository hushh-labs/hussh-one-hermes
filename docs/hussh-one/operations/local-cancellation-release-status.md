# Local cancellation release status

Status: implementation under review; release acceptance incomplete.
Evidence date: 2026-09-14, controlled local host.

## Confirmed failure boundary

The original runtime separated the compression transcript fence from the
auxiliary transport cancellation signal. A compression inactivity timeout
could return control while its inference request continued to occupy local
capacity. Foreground attempts then exhausted short admission retries.
Buffered answer display and a sidebar channel that changed across refresh
made the ongoing work harder to see.

The proposed changes join attempt cancellation, give local auxiliary calls
independent transport ownership, and let foreground calls wait within their
turn deadline. Deterministic tests cover transport shutdown and transcript
fencing. Those tests do not establish when LM Studio releases its engine slot.

## Provider termination gap

Synthetic cancellation probes with `meta/muse-glimmer` on LM Studio showed:

| Signal | Observation | Sufficient engine-release proof? |
|---|---|---|
| HTTP disconnection | Client ended before engine task release | No |
| Python SDK 1.5.0 `userStopped` result | Returned before engine task release | No |
| `lms ps` status `idle`, queued count zero | Reported idle while engine task remained active | No |

In the status-correlation probe, cancellation was sent at epoch
1789451693.812608. The SDK returned at 1789451693.818727; the first idle sample
was 1789451693.987267. Engine release was first observed at
1789451700.4516711. Polling bounds the observation; it is not an exact engine
completion timestamp. The synthetic request was confirmed released before the
probe finished. No model was unloaded, replaced, or reconfigured.

The provider's documented cancellation API therefore cannot, by itself, prove
the capacity-release boundary required for this release on the tested host.
A fixed grace period, a second short request, or frontend idle status is not
an adequate substitute. Engine logs supplied evidence for this isolated probe;
production request correlation, concurrent external clients, and log rotation
have not been solved and are not claimed as an implemented confirmation API.

The draft auxiliary worker currently releases capacity when its transport
worker exits. That remains a known release blocker. Do not treat its passing
socket tests as permission to merge or deploy it. An implementation must retain
occupied status after uncertain cancellation until engine termination can be
confirmed, and must expose that state to the waiting interaction.

## Acceptance evidence and limits

The frozen synthetic baseline at pre-fix revision
`e7c98f22496590eab0d209207a6aaf2d6a660589` passed all 12 selected cases: three
repetitions each of terminal, tool selection, file edit, and long context.
The model reported 131072 loaded context. Provider model revision was not
reported and is recorded as unknown. The first long-context pilot timed out;
that incomplete run is retained separately and does not count as a pass.
The revised long-context deadline was frozen before the complete baseline.

These are narrow synthetic model cases, not end-to-end runtime acceptance.
Three-repeat cancellation recovery, browser reconnect, committed-tool replay
protection, second-install adoption, and final-main process receipts remain
unverified. No result from another SHA may satisfy a merge-group check.

## Installation migration gap

The pre-fix updater defines its shell functions before pulling new code.
Replacing the file on disk does not replace functions already executing in the
old shell. A second-install rehearsal must explicitly cover this first update;
ordinary tests starting with the new updater do not prove migration safety.
The old invocation must not continue into upstream reconciliation or restart
before the new locked-dependency and guard checks have succeeded.
