# Feature — Trusted-Device PKM Bridge

## What it does

Optionally links one Hermes profile to a Hussh One account in UAT. The user
approves the Mac as a trusted device in the browser, enters the existing vault
passphrase once in a masked local field, and receives a narrow local MCP surface
for consent lifecycle guidance and explicitly approved PKM writes.

## How it works

- `hermes_cli/hussh_one_pkm/client.py` owns Authorization Code + PKCE, Firebase
  native token exchange, the registered P-256 device key, and Keychain refresh
  credential.
- `bridge.py` unwraps the existing passphrase wrapper locally and stores only a
  device-bound encrypted vault-key envelope in the active profile.
- `pkm.py` preserves the current PKM v6 ciphertext, manifest,
  `PkmMutationPlanV2`, validation-only, sharing-impact, and optimistic
  concurrency contracts.
- `mcp_server.py` exposes six additive tools through Hermes' existing MCP
  handshake and form elicitation.
- Desktop onboarding remains optional. Completing it installs the local MCP
  entry into only the active profile.
- Setup and vault management endpoints accept loopback workstation requests
  only. Remote conversations can use the configured local MCP bridge, but a
  remote dashboard cannot transport the passphrase or manage local key material.

## Authority boundaries

1. Firebase proves account identity.
2. The P-256 device signature proves this Hermes installation.
3. The locally unwrapped vault key enables cryptographic PKM work.
4. A 15-minute device-bound `VAULT_OWNER` capability authorizes a mutation.
5. Every commit still requires Hermes MCP approval.

A Hussh developer token supplies none of these authorities.

## Local custody

- Device signing key, Firebase refresh credential, and random vault-envelope
  wrapping key: macOS Keychain.
- Encrypted vault-key envelope and non-secret identity metadata: active Hermes
  profile with owner-only file permissions.
- Unwrapped vault key and ID/owner tokens: process memory only.
- Vault passphrase: transient onboarding argument only; never config, MCP,
  environment, log, trace, screenshot, or model context.

The vault clears on explicit lock, inactivity, device authorization failure,
system lock, and suspend. Revocation blocks new owner capabilities and the next
identity refresh.

## Read and write behavior

Reads remain PCHP consent requests and scoped encrypted exports. This bridge
does not return a decrypted PKM domain to the agent. Writes are two-step:
proposal, then commit. Commit displays the affected domain/path, human-readable
summary, and current sharing/export impact; it re-reads the source revision and
fails closed if content or sharing changed.

## Configuration

No vault material is stored in configuration. The first successful enrollment
adds `hussh-one-pkm` to the active profile's `mcp_servers` map. Disconnect
revokes the server-side device, removes that MCP entry, deletes local identity
and envelope state, and removes related Keychain items.

## Fresh-machine onboarding

Prerequisites:

- macOS with Keychain available;
- a Hussh One UAT account allowed to register a trusted device;
- the existing Hussh vault passphrase;
- a clone of the Hussh One product trunk, `origin/main`.

Install and start the normal Hussh One runtime:

```bash
git clone https://github.com/hushh-labs/hussh-one-hermes.git
cd hussh-one-hermes
scripts/hussh-one-bootstrap.sh --manager auto --start
scripts/hussh-one-doctor.sh --require-services
```

Then launch the bundled Desktop app with `hermes desktop`. During the normal
model-confirmation onboarding:

1. Select **Connect in browser**.
2. Approve the new Mac as a trusted device in the Hussh One UAT surface.
3. Return to Desktop after identity reports connected.
4. Enter the vault passphrase in the masked local field.
5. Select **Secure this device**.
6. Confirm **PKM bridge ready**.

Enrollment installs the bundled `hussh-one-pkm` MCP server using the same
Python runtime as Hermes. It does not require cloning another repository,
installing a global Hermes binary, copying a developer token, or placing the
vault passphrase in configuration.

If UAT authorization, Keychain storage, vault compatibility, or MCP
registration fails, onboarding remains recoverable and the bridge stays
locked. The rest of Hermes setup continues to work because this feature is
optional.

## Upgrading an existing machine

Update the Hussh One product trunk and rerun the idempotent bootstrap:

```bash
git switch main
git pull --ff-only origin main
scripts/hussh-one-bootstrap.sh --manager auto --start
scripts/hussh-one-doctor.sh --require-services
```

The Python package includes the PKM bridge and its cryptographic golden vectors,
so a normal Hussh One upgrade updates them together. Existing profile metadata,
the encrypted vault-key envelope, and Keychain items remain local and are not
recreated or uploaded. After upgrade, open Desktop and verify **PKM bridge
ready**; use **Unlock** if the system was locked or suspended.

Do not copy another machine's profile directory or Keychain records. A second
machine must complete browser approval and enrollment independently so its
device identity and vault envelope remain bound to that machine.

## Recovery and rollback

- System lock or suspend: reopen Desktop and select **Unlock**.
- Revoked/invalid device: reconnect through browser approval.
- Vault contract mismatch: stop; update Hussh One and the UAT service before
  retrying. Do not rewrite the vault or bypass golden-vector validation.
- Disconnect: use the trusted local control-plane disconnect action. It revokes
  the server-side device, removes the profile MCP entry and local envelope, and
  deletes related Keychain items.
- Full rollback: disconnect first, then use the normal Hussh One repository
  rollback procedure. Never restore trusted-device secrets from Git.

## Tests

- Mirrored TypeScript/Python vault golden vector.
- Passphrase failure and envelope identity binding.
- PKCE, code replay, nonce replay, signature, and revocation service tests.
- Proposal safe-result and single-use behavior.
- Existing Hermes MCP elicitation and Hussh PKM validation/store regression
  suites remain the integration owners.

## Status

🧪 UAT-only, feature-flagged, allowlisted, macOS MVP.
