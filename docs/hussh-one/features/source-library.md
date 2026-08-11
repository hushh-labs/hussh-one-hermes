# Source Library Steward

Source Library gives the local Hussh One private agent a bounded control plane over
owner-selected, already-mounted Google Drive, iCloud Drive, or local folders. It can
reconcile, query, organize, propose file operations, and publish an exact item through
an owner-bound provider folder. `ask_file_steward` remains a compatibility facade.

```text
provider files (blob truth)
  -> deterministic mounted-tree adapter
  -> private PKM knowledge + profile source-library.db mapping
  -> Source Library Steward proposals
  -> fresh owner approval + revision-safe deterministic execution
```

## Authority and information model

| Plane | Stores | Authority |
|---|---|---|
| Provider blob | File bytes and provider synchronization | Source of truth |
| Private PKM | Reviewed facts/summaries and opaque provenance | Semantic/control memory; never an `attr.*` export |
| SQLite mapping | Opaque refs, revisions, lifecycle state, encrypted locators | Rebuildable local index; never content authority |
| Steward | Bounded query and proposal tools | No binding, execution, credentials, ACL, terminal, or delegation authority |
| Parent executor | Fresh approval and revision revalidation | One exact mutation/share operation |

SQLite contains no plaintext paths, titles, provider identifiers, recipient emails,
source bytes, extracts, or raw content hashes. Decrypted search views exist only in
process while the vault is unlocked.

## Device-local custody

Every new Source Library catalog, artifact, and sealed SQLite record uses a
versioned AES-GCM purpose key derived from both the unlocked 32-byte Hussh vault
key and a separate 32-byte device-custody secret. The latter lives only in the
macOS Data Protection Keychain as a `WhenUnlockedThisDeviceOnly`, local
user-presence protected item; it is never stored in SQLite, an artifact,
configuration, or model context. The bridge caches it only for the unlocked
vault session and zeroizes it on explicit lock, profile lock, workstation lock,
revocation, and disconnect.

This is device-only Keychain plus LocalAuthentication custody, not a claim that
Hermes currently creates a non-exportable Secure Enclave `SecKey`. On compatible
Apple hardware, macOS may mediate the user-presence policy through platform
security facilities; this contract does not claim a hardware-resident source
key. A future non-exportable Secure Enclave device-signing-key adapter is a
separate trusted-device migration, not an implicit property of this source-plane
storage change.

Legacy v1 vault-derived envelopes are re-encrypted transactionally at the first
unlocked Source Library use. A protected Keychain phase latch then rejects any
replayed v1 ciphertext. If a profile has existing Source Library ciphertext but
its device-custody secret is missing, Hermes fails closed; it never rotates a
key over existing data. Disconnect removes the local Source Library plane as
well as its custody item. SQLite remains a field-level sealed metadata index,
not a full-database-encryption claim.

## Synchronization

Scans are metadata-first and bounded by entry count, depth, and time. An incomplete scan
never declares unseen items missing. Complete reconciliation classifies create, modify,
rename/move, unavailable/return, and placeholder transitions. Changed materialized files
are read through bounded no-follow descriptor walks; placeholders remain metadata-only
and are never implicitly hydrated. Root identity drift requires owner re-binding.

## Manage and share

Manage-enabled roots support create, rename, move, atomic overwrite, and move-to-Trash.
Each action is proposed first, pinned to the reviewed revision, freshly approved, and
reported as `provider_sync_pending`; Hermes never claims cloud-wide completion.

Bound share targets use provider access already configured by the owner. Modes are
`reference_existing`, `copy_revision`, `move_original`, and `knowledge_snapshot`.
Audience labels are `provider_managed_unverified`: V1 has no OAuth, provider API, ACL
inspection, permission mutation, or recipient-email claim. Exposure is derived from
recorded shares plus actual target containment. Revocation must move or Trash the
published artifact; deleting a SQLite row is not revocation.

## Safeguards

- Roots and share targets require explicit local binding and fresh approval.
- Observe-only bindings cannot mutate files or become share targets.
- Source text is untrusted and cannot authorize writes, sharing, or tool calls.
- Cross-boundary moves, symlink escapes, root replacement, stale revisions, and races are rejected.
- Irreversible purge, bulk mutation, implicit hydration, and provider ACL changes are outside V1.

## Verification and status

```bash
scripts/run_tests.sh tests/hermes_cli/test_hussh_one_source_library.py
scripts/run_tests.sh tests/test_hussh_one_source_library_tool.py tests/agent/test_system_prompt.py
```

The hermetic lifecycle uses synthetic temporary folders only: bind, reconcile human
changes, query, approve knowledge, bind target, publish, list exposure, revoke, and Trash.
This is a local Hermes UAT milestone. A future app tile and authenticated provider adapter
may reuse these contracts; verified per-email Drive permissions are not included.
