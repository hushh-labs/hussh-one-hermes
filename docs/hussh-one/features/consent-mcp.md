# Hussh Consent MCP

Hussh One packages a least-privilege consent workflow for Gemini and every
other Hermes provider. The model may manage scope discovery and consent
lifecycle receipts, but connector keys and encrypted envelopes never enter
model context.

## Runtime contract

1. Discover the narrowest available scope.
2. Request consent with a plain-language purpose.
3. Poll no faster than the connector requests and stop on every terminal
   state.
4. Retrieve only after `granted`, using the exact approved scope.
5. Keep hosted ciphertext, resource links, wrapped keys, and cryptographic
   metadata outside the model.

A trusted local connector may return `delivery=decrypted_local`. Hermes does
not place that plaintext directly into conversation history. It creates an
opaque one-time lease under the active `HERMES_HOME`:

- directory mode: `0700`;
- file mode: `0600`;
- lifetime: ten minutes;
- maximum authorized payload: 64 KiB;
- consumption: explicit and one time;
- deletion: before plaintext is emitted to the requesting model turn.

The bundled `hussh-consent-mcp-workflow` skill permits consumption only when
the user explicitly asks the model to analyze the approved information. Empty
exports produce no lease. Oversized exports require a narrower scope.

## Connector modes

Hussh One uses the hosted streamable endpoint
`https://api.uat.hushh.ai/mcp/` as the consent lifecycle source of truth. Its
bearer header references the one-time developer credential in the active
profile's mode-`0600` `.env`; the token is never embedded in MCP configuration.

Hermes' MCP transport boundary creates one persistent X25519 identity under
that profile with mode-`0600`. For an `attr.*` consent request it adds the
public binding after model argument validation. On an approved encrypted
export it authenticates the envelope, decrypts locally, narrows to the exact
approved scope, and immediately converts the result into the bounded lease.
The model can neither submit connector keys nor observe the private key,
ciphertext, or wrapping metadata.

Codex uses the same hosted endpoint and bearer environment reference when its
CLI is available. Neither host configuration contains the developer token or
connector private key.

## TUI behavior

MCP tools are fixed for the life of a conversation to preserve prompt caching.
After enabling or changing the consent connector, start a new TUI session or
run `/reload-mcp` and approve the reload. Resuming a session that captured the
connector as disabled will not silently mutate that session's toolset.

The TUI must report terminal states without retrieval:

- `pending`: wait for a later user turn;
- `denied`, `expired`, `revoked`, or `cancelled`: stop;
- `granted`: retrieve once with the exact scope;
- `decrypted_export_empty`: report that no approved information was available;
- `requires_narrower_scope`: request a narrower scope.

## Repository ownership

`origin/main` is the Hussh One product trunk and default installation branch.
`upstream/main` is the read-only Nous Research comparison source. Upstream
reconciliation happens on short-lived sync branches and returns to
`origin/main`; Hussh One does not maintain a second long-lived product trunk.
