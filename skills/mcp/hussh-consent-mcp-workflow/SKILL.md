---
name: hussh-consent-mcp-workflow
description: "Use Hussh Consent MCP with least privilege and connector-only encrypted export handling."
category: mcp
---

# Hussh Consent MCP Workflow

Use this workflow whenever an agent requests consent-backed user data through
`mcp__hushh_consent__*`.

## Security boundary

- The model is not the connector trust boundary.
- Never generate, read, persist, display, remember, or manipulate a connector
  private key in an agent session.
- Never place ciphertext, wrapped keys, nonces, tags, decryption recipes, or
  encrypted-export resource links in model context.
- Never use terminal, code execution, file tools, memory, or a subagent to
  decrypt an export.
- Decryption happens only inside the trusted connector process outside the
  model. If that connector is unavailable, stop after reporting the safe
  lifecycle state.

## Required sequence

1. Call `mcp__hushh_consent__search_user_scopes` and select the narrowest
   available scope that satisfies the user's stated purpose.
2. Call `mcp__hushh_consent__request_consent` with that exact scope and a
   plain-language purpose. A registered connector should omit key fields and
   use its out-of-process key custody. If an unregistered connector requires
   key provisioning, stop and ask for trusted connector setup; the agent must
   not create the key itself.
3. Call `mcp__hushh_consent__check_consent_status` no faster than the returned
   polling interval. Stop on every terminal state. Do not retrieve an export
   unless the status is `granted` and a `grant_ref` is present.
4. Call `mcp__hushh_consent__get_encrypted_scoped_export` only with the granted
   reference and the exact approved scope.
5. Treat the returned model-safe receipt as completion unless all of these are
   true:
   - the connector reports `delivery=decrypted_local`;
   - Hermes returns a `decrypted_export_ready` one-time lease;
   - the user explicitly asked the model to analyze or consume the approved
     information.
6. For that explicit case only, run the exact `consume_command` from the
   receipt once. The lease is mode `0600`, expires after ten minutes, is
   limited to 64 KiB, and is deleted before its authorized information is
   emitted. Never copy the lease into memory or another file. If the receipt
   says `decrypted_export_empty`, report that the approved scope contained no
   information and stop.

## Failure handling

- `pending`: report that approval is pending and wait for a later user turn;
  do not spin in an unbounded polling loop.
- `denied`, `declined`, `expired`, `revoked`, or `failed`: stop and report the
  terminal state without attempting retrieval.
- Connector-key or registration error: stop and request connector
  provisioning. Never work around it with a local script or ad hoc key file.
- `requires_narrower_scope`: request a narrower discovered scope. Do not split,
  copy, or bypass the lease size limit.
- Model refusal or interrupted tool sequence: preserve the terminal assistant
  turn. On resume, inspect current consent state before retrying any action.

## Verification

A safe successful run proves all of these:

- the selected scope was the narrowest available;
- retrieval occurred only after a granted terminal state;
- no encrypted envelope or connector key entered model history or logs;
- the final user-visible answer contains only consent-authorized,
  scope-limited information returned through a trusted plaintext interface.
