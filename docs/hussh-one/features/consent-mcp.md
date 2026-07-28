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

The hosted HTTPS connector supports lifecycle management and encrypted
delivery. It cannot provide model-consumable plaintext because the model is
not a connector key custodian.

For explicit local consumption, configure the official trusted consent
connector as a stdio MCP server. Its private export key remains in the
connector's own state. Hussh One receives only locally decrypted,
scope-limited information and immediately converts it into the bounded lease
described above.

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
