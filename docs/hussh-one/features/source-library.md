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

## Runtime configuration and setup

The Source Library capability is enabled by default only for an interactive local
workstation. Its explicit feature switch is profile-scoped:

```yaml
# ~/.hermes/config.yaml
hussh_one:
  source_library:
    enabled: true  # set false to remove all Source Library tools locally
```

It is injected into **Hussh One Desktop** and the loopback **dashboard** parent
agent as the `hussh_one` capability. It is never injected into WhatsApp,
Telegram, Slack, Signal, email, other messaging surfaces, or standalone TUI.
The parent receives binding, approval, execution, and `ask_source_library_steward`;
the latter launches the Source Library Steward with the separate, exact
`hussh_one_sources` leaf toolset. The parent does not receive the raw leaf tools.

To configure a source in a local Desktop/dashboard conversation:

1. Connect, enroll, and unlock Hussh One through `/hussh-one`.
2. Ask the parent to bind one existing mounted folder, naming it `icloud_drive`,
   `google_drive`, or `local_drive`, and choose `observe` or `manage` access.
3. Ask it to scan the source. Once a binding exists, natural requests such as
   “find the project documents in my Shared Drive” route to the Steward.
4. Bind a directory inside a manage-enabled root as an owner-reviewed share target
   before asking the Steward to propose a publication.

`google_drive` is a provider-neutral label for a user-selected, materialized folder
mounted by Google Drive for desktop. V1 neither discovers every Google Shared Drive
nor uses Drive OAuth/API metadata. The Steward can manage only the explicitly bound
folder tree; it cannot verify individual recipient emails or change Drive ACLs.

## Parent and Steward harness boundary

| Harness | Exact authority | Cannot do |
|---|---|---|
| Desktop/dashboard parent (`hussh_one`) | Bind/list roots; commit approved knowledge, file, share, and revoke proposals; launch Steward | Read raw leaf tools directly; bypass fresh approval/revision checks |
| Source Library Steward (`hussh_one_sources`) | Scan, browse/search, bounded read, and propose organization, knowledge, file, share, or revocation work | Terminal, generic filesystem, credentials, vault keys, browser, provider APIs, root binding, execution, delegation, or parent MCP tools |
| Messaging/standalone TUI | None | Bind, inspect, share, or execute any Source Library action |

The child harness uses a private product launcher to attach exactly the leaf toolset,
without inheriting parent MCP servers. Hermes may compact that seven-tool leaf catalog
behind its safe `tool_search` bridge; the bridge can resolve only those seven tools,
never parent, generic, or MCP tools.

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

There is no file-event watcher in this UAT milestone. The deterministic reconciliation
scan and durable checkpoints recover offline/human changes; a future watcher can submit
hints to the same reconciliation contract without becoming a new source of truth.

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
scripts/run_tests.sh tests/scripts/test_hussh_one_doctor_reliability.py
HUSSH_ONE_SKIP_DASHBOARD_HEALTH=1 bash scripts/hussh-one-guard.sh
python3 scripts/hussh-one-health-index.py --skip-doctor --skip-fetch
```

The hermetic lifecycle uses synthetic temporary folders only: bind, reconcile human
changes, query, approve knowledge, bind target, publish, list exposure, revoke, and Trash.
This is a local Hermes UAT milestone. A future app tile and authenticated provider adapter
may reuse these contracts; verified per-email Drive permissions are not included.

The health index reports only non-sensitive local readiness (`not-enrolled`,
`vault-locked`, `unbound`, binding-index unavailable, or configured bindings). It never
opens Keychain custody or a mounted root, so it cannot truthfully claim that another
desktop process is currently unlocked. Use the active Desktop/dashboard session for that
live readiness decision.

## Future application adapter boundary

`hushh-research` is not connected in this milestone. A future local/native adapter must
call the public service boundary—status, source/target listing, bounded query, proposal
creation, explicit owner approval/execution, reconciliation status, active shares, and
revocation—rather than reading `source-library.db` or provider folders. Its result schema
must use opaque item/share references and safe display summaries only. It must never
receive local paths, source bytes, extracts, vault/custody material, or the ability to
impersonate a local owner approval. This keeps an eventual application tile modular while
preserving provider-file authority and the Steward/parent split.
