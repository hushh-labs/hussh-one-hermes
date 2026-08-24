---
name: hussh-one-pkm-vault-operations
description: "Inspect, unlock, and operate the Hussh One local PKM vault."
category: mcp
---

# Hussh One PKM Vault Operations

Use this skill when inspecting, unlocking, diagnosing, or operating the Hussh One Personal Knowledge Model (PKM) local vault bridge (`hermes_cli/hussh_one_pkm`) and native Desktop/TUI adapter.

## Overview & Local Custody

The Hussh One PKM bridge links a local Hermes profile to a Hussh account in UAT (`https://api.uat.hushh.ai`).
- **Storage Path:** `~/.hermes/hussh-one/` (or `get_hermes_home() / "hussh-one"`)
- **Local Envelope:** `vault-envelope.json` (encrypted vault key sealed via macOS Keychain, AES-256-GCM)
- **Lock State:** `vault-lock-state.json` (`{"locked": false, "reason": "vault_enrolled", ...}`)
- **Identity:** `identity.json` (`account_email`, `device_id`, `profile_id`, `environment`)
- **Replica:** `pkm-replica/domains/` (local JSON snapshot files: `developer.json`, `financial.json`, `food.json`, `identity.json`, `location.json`, `travel.json`)
- **Trusted Device Detection & Push Sync:** This Hermes node is registered as an active trusted device (`custody_mode: trusted_device_until_lock_or_revoke`). Inbound event notifications trigger `sync_encrypted_replica()` immediately to pull incremental ciphertext deltas without polling lag.

### Fast Direct Inspection (Zero-Import Fallback)

When checking vault status or connection without Python dependencies:
- Read `~/.hermes/hussh-one/identity.json` to verify `account_email`, `device_id`, and `environment`.
- Read `~/.hermes/hussh-one/vault-lock-state.json` to check `locked` boolean and enrollment reason.
- List `~/.hermes/hussh-one/pkm-replica/domains/` to check active local replica domain cache.

## Python Inspection & Diagnostics

To programmatically query identity and vault status in Python (always use `.venv/bin/python3` when running shell commands to ensure `httpx` and dependencies are imported):

```python
from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

bridge = HusshVaultBridge()

# Query identity registration
identity = bridge.identity_status()
# {'connected': True, 'connection_state': 'connected', 'device_id': '...', 'account_email': '...', ...}

# Query vault lock and custody status
status = bridge.vault_status()
# {'connected': True, 'enrolled': True, 'unlocked': True, 'profile_locked': False, 'custody_mode': 'trusted_device_until_lock_or_revoke', ...}
```

## Querying & Decrypting PKM Domain Content in Python

To query, list, and decrypt vault domain content (e.g. `financial`):

```python
from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge
from hermes_cli.hussh_one_pkm.pkm import PkmClient, _decrypt_domain

bridge = HusshVaultBridge()
client = PkmClient(bridge)

# 1. List available owner domains & scopes
domains = client.list_domains()  # returns [{'domain': 'financial', 'attribute_count': 738, ...}]

# 2. Acquire vault key and fetch/decrypt domain snapshot
key = bridge.require_vault_key()
owner_token = bridge.acquire_vault_owner_token()
snapshot = client._snapshot(domain='financial', owner_token=owner_token)

if snapshot:
    data = _decrypt_domain(snapshot['encrypted_blob'], key)
    # Root keys in 'financial': ['profile', 'sources', 'portfolio', 'analysis', 'documents', 'analysis_history']

# 3. Reading via PkmClient.read()
# Note: scope_path traverses relative dict keys (e.g., 'profile' or 'portfolio.holdings'), NOT the 'attr.financial.profile' scope string.
profile = client.read(domain='financial', scope_path='profile')
```

### Direct Decryption of Local Replica Cache (Offline / 503 Fallback)

When the remote snapshot API is unreachable or returns HTTP 503, decrypt the local replica snapshot directly from disk:

```python
import json
from pathlib import Path
from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge
from hermes_cli.hussh_one_pkm.pkm import _decrypt_domain

bridge = HusshVaultBridge()
key = bridge.require_vault_key()

replica_path = Path.home() / ".hermes" / "hussh-one" / "pkm-replica" / "domains" / "developer.json"
if replica_path.exists():
    with open(replica_path, "r") as f:
        replica = json.load(f)
    data = _decrypt_domain(replica["encrypted_blob"], key)
```

### Pitfalls & Scope Rules
- **Python Execution:** Always invoke `.venv/bin/python3` when running shell commands to execute scripts that import `httpx`, `hermes_cli`, or project tools. Standard system `python3` lacks virtualenv dependencies.
- **CloudStorage / Virtual Mount Hard Links (`os.link`):** On macOS Google Drive / CloudStorage virtual mounts (`/Users/.../Library/CloudStorage/GoogleDrive-*`), `os.link` throws `OSError` (operation not supported). File operations falling back to `shutil.move` / `os.rename` prevent hard-link failures on cloud-mounted trees.
- **Path Resolution across Symlinked Cloud Drives:** `root.resolve(strict=True)` on macOS CloudStorage paths resolves through `/Library/CloudStorage` symlinks to `/Volumes/...`. Ensure path validation compares against both `root` and `root.resolve(strict=True)` to avoid `relative_to` `ValueError` path mismatch exceptions.
- **Scope Paths vs Scope Strings & Character Restrictions:** `client.read(domain='financial', scope_path='...')` expects relative dictionary key paths (e.g., `'profile'`, `'portfolio.holdings'`). Scope paths are strictly validated by `^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,15}$`. Hyphens or special characters in dictionary keys (e.g., `hushh-labs` or `Almari-app`) will cause `The requested PKM scope path is invalid.` Sanitize nested dictionary keys to snake_case (`[a-z0-9_]`) or query the parent scope path.
- **Size Limits on Reads:** `client.read` enforces maximum leaf (500) and JSON byte (65,536 bytes) limits. Full domain reads without a narrow `scope_path` on large domains will raise `The requested PKM read is too broad.` Decrypt the snapshot directly via `_decrypt_domain(snapshot['encrypted_blob'], key)` or pass narrow sub-paths when reading large datasets.
- **Automatic Replica Snapshot Caching:** Whenever `client.read()` or `client._snapshot()` executes, the fetched encrypted snapshot is automatically saved to local replica storage under `~/.hermes/hussh-one/pkm-replica/domains/<domain>.json`.
- **Local CloudStorage Google Drive Mounts & Source Library Steward:** Local Google Drive accounts mounted on macOS live under `~/Library/CloudStorage/GoogleDrive-*`. The Source Library Steward (`ask_source_library_steward` / `hussh_one_sources` toolset) manages these mounted roots via `from hermes_cli.hussh_one_source_library.service import SourceLibraryService; svc = SourceLibraryService(bridge=bridge)` (`bind_mounted_root`, `list_sources`, `sync_status`). File operation proposals (`hussh_one_source_propose_file_operation`) are revision-pinned and require fresh owner approval, guaranteeing zero information loss.
- **macOS Keychain OSStatus -34018 Fallback:** Non-bundled CLI/Python processes lack Data Protection Keychain entitlements (`kSecUseDataProtectionKeychain`). `MacOSKeychain` in `hermes_cli/hussh_one_pkm/keychain.py` falls back to standard generic password Keychain operations (`up_<account>`) when `-34018` is returned.

## Local `/hussh-one` Commands

| Command | Description |
| --- | --- |
| `/hussh-one status` | Shows verified email, device ID, enrollment status, lock state, and replica cursor |
| `/hussh-one connect` | Opens browser approval for device registration (never pass email in chat) |
| `/hussh-one enroll` | Resumes local vault custody ceremony (passkey or protected passphrase) |
| `/hussh-one unlock` | Opens the Keychain-bound envelope into process memory |
| `/hussh-one lock` | Clears unwrapped vault keys and action capabilities from process memory |
| `/hussh-one disconnect` | Revokes server-side device and purges local profile & Keychain custody |

## Native Write Connector & Mutations (`PkmClient`)

- **Contract:** PKM v6.0.0
- **Flow:** Proposal $\rightarrow$ Fresh User Approval $\rightarrow$ Commit $\rightarrow$ Replica Sync.
- **Safety:** Hard-blocks structural keys and external metadata leaks, verifies source revision, and marks overlapping continuous encrypted exports for automatic refresh.

### Programmatic PKM Write Example (Python)
```python
from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge
from hermes_cli.hussh_one_pkm.pkm import PkmClient

bridge = HusshVaultBridge()
# Always unlock vault in process memory before requiring key / writing
bridge.unlock()

client = PkmClient(bridge)

# 1. Propose mutation
proposal = client.propose(
    domain="developer",
    scope_path="attr.developer.infrastructure",
    merge_patch={"attr": {"developer": {"infrastructure": {"organization": "hushh-labs"}}}},
    summary="Update developer infrastructure organization filter.",
    operation="upsert"
)

# 2. Commit mutation
commit_res = client.commit(proposal)

# 3. Sync local encrypted replica cursor
sync_res = client.sync_encrypted_replica()
```

### Domain & Scope Path Rules
- **Domain:** Lowercase ASCII (`^[a-z][a-z0-9_]{0,63}$`), e.g. `developer`, `financial`.
- **Scope Path:** Dotted lowercase ASCII (`^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,15}$`), e.g. `attr.developer.infrastructure`. Every leaf path in `merge_patch` must remain inside the reviewed scope path.
- **Hussh Consent Integration:** Committed PKM domains auto-register in `mcp__hushh_consent__search_user_scopes` (e.g. `attr.developer.attr.*`).

### Mandatory PKM Storage Confirmation Protocol

Whenever storing, updating, or committing information to the PKM vault, the agent MUST provide a structured confirmation breakdown before or during commit:
1. **Target Domain & Scope Path:** (e.g., `identity` -> `attr.identity.profile.residential_address`)
2. **Exact Stored Attributes:** Cleaned plaintext attributes with sensitive PII (SSN, unmasked bank numbers) masked as appropriate.
3. **Source Provenance:** Verifiable origin handle (e.g., Gmail thread/message ID, OneDrive document path, direct user confirmation).
4. **Downstream Form Mapping Impact:** How the stored data maps into KYC/KYB schemas (Gusto, Stripe, Mercury, FinCEN BOIR).
5. **Interactive Confirmation Gate:** Never finalize PKM mutations without explicit owner consent and legibility over what is written.

## Reference
- KYC/KYB & cross-form onboarding automation reference (Gusto, Stripe, Mercury, FinCEN BOIR): `references/kyc-kyb-cross-form-automation.md`.
- Google Drive & CloudStorage mount technical pitfalls: `references/google-drive-cloudstorage-pitfalls.md`.
- Local bridge diagnostic & inspection guide: `references/local-bridge-inspection.md`.
- Detailed architecture and local custody specs: `docs/hussh-one/features/trusted-device-pkm.md`.
