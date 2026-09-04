# Hussh One PKM Local Bridge Inspection Guide

This guide details exact programmatic inspection routines and local filesystem locations for the Hussh One PKM vault bridge.

## Filesystem Artifacts (`~/.hermes/hussh-one/` or `$HERMES_HOME/hussh-one/`)

1. **`identity.json`**
   - Fields: `user_id`, `device_id`, `profile_id`, `account_email`, `api_base`, `web_base`, `environment`
   - Role: Identity registration and server endpoint binding.

2. **`vault-envelope.json`**
   - Fields: `schema_version`, `user_id`, `device_id`, `profile_id`, `iv`, `ciphertext`, `vault_key_hash`
   - Role: Keychain-bound local custody envelope.

3. **`vault-lock-state.json`**
   - Fields: `schema_version`, `locked`, `reason`, `updated_at_ms`
   - Role: Inter-process shared lock state indicator.

4. **`replica/`**
   - Sub-files: `state.json` (contains `cursor`)
   - Role: Local metadata cursor for encrypted cloud snapshots.

## CLI & Python Diagnostic Snippets

Run with the active virtual environment (`.venv/bin/python`):

```python
from hermes_cli.hussh_one_pkm.bridge import HusshVaultBridge

bridge = HusshVaultBridge()

# Check Identity:
identity = bridge.identity_status()
print(f"Connected: {identity['connected']}")
print(f"Email: {identity['account_email']}")
print(f"Device ID: {identity['device_id']}")

# Check Vault Custody:
status = bridge.vault_status()
print(f"Enrolled: {status['enrolled']}")
print(f"Unlocked: {status['unlocked']}")
print(f"Cursor: {status['encrypted_replica_cursor']}")
```
