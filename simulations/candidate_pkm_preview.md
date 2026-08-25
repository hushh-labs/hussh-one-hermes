# Hussh One — PKM-to-Gusto schema simulation

**Status:** `SCHEMA_ONLY_SIMULATION`
**Audit reference:** `GUSTO-KYC-SPEC-2026.01`
**Contains vault values:** no

This artifact documents field paths and mapping behavior only. It is not a PKM
export, a decrypted vault preview, or evidence that any field exists for the
active owner.

## Representation contract

- Durable Hussh One PKM and the trusted-device replica contain AES-256-GCM
  ciphertext envelopes only.
- An authorized `read_my_pkm` call may return a narrow decrypted projection in
  process memory for the current owner request.
- Documentation never represents protected values with strings such as
  `VAULT_ENCRYPTED` or `VAULT_REF`. A placeholder is not encryption and must
  never be interpreted as a stored PKM value.
- Passphrases, vault keys, recovery keys, and connector private keys never enter
  chat, model prompts, logs, docs, or committed fixtures.

## Domain field inventory

The cross-form engine may require these paths, subject to the active owner's
actual PKM manifest and a narrow authorized read:

```json
{
  "artifact_kind": "pkm_field_inventory",
  "storage_representation": "aes-256-gcm-ciphertext",
  "decrypted_values_included": false,
  "domains": {
    "legal_entity": [
      "entity.legal_name",
      "entity.trade_name_dba",
      "entity.entity_type",
      "entity.tax_classification",
      "entity.formation_state",
      "entity.fein",
      "entity.naics_code",
      "entity.industry_description",
      "entity.registered_address",
      "entity.principal_work_location",
      "entity.website",
      "entity.corporate_phone",
      "beneficial_ownership"
    ],
    "identity": [
      "profile.first_name",
      "profile.last_name",
      "profile.date_of_birth",
      "profile.ssn",
      "profile.citizenship",
      "profile.phone",
      "profile.email",
      "profile.residential_address"
    ],
    "financial": [
      "operating_bank_account.bank_name",
      "operating_bank_account.account_holder_name",
      "operating_bank_account.account_type",
      "operating_bank_account.routing_number",
      "operating_bank_account.account_number",
      "operating_bank_account.ach_sweep_authorized",
      "operating_bank_account.purpose"
    ],
    "tax_record": [
      "federal.ein",
      "federal.filing_form",
      "federal.tax_payer_type",
      "federal.form_8655_signed",
      "state_accounts"
    ]
  }
}
```

## Gusto mapping inventory

| Gusto area | Candidate PKM paths | Handling |
| --- | --- | --- |
| Employer identity | `legal_entity.entity.*` | Read only the required fields; show a masked dry-run before any external write. |
| Signatory | `identity.profile.*` | Treat government identifiers, birth date, contact details, and address as sensitive. |
| Payroll bank | `financial.operating_bank_account.*` | Never show full routing or account numbers in previews or logs. |
| Federal tax | `tax_record.federal.*` | Validate identifiers locally; transmit only after explicit approval. |
| State registrations | `tax_record.state_accounts.*` | Resolve relevant jurisdiction and agency paths at runtime. |

## 0-to-1 execution flow

1. Run `/hussh-one status`; if needed, use `/hussh-one connect`,
   `/hussh-one enroll`, or `/hussh-one unlock`. Secret collection stays in the
   protected native UI.
2. Use `read_my_pkm` to list domains, then read only the required scopes.
3. Validate and map the in-memory values with the deterministic KYC mapper.
4. Present a masked, field-by-field dry-run. Do not label it the stored vault
   JSON.
5. Obtain explicit owner approval before submitting any external form or API
   request.
6. If information must be saved or corrected, use `save_to_pkm`; after approval
   it encrypts locally and persists ciphertext only.

## Fail-closed conditions

- Vault locked or not enrolled: stop and direct the owner to the native command.
- Authorized read unavailable: do not substitute docs, simulations, source
  files, conversation history, or guessed values.
- Missing field: report it as absent from the authorized projection; do not
  invent a placeholder value.
- Read too broad: request narrower scopes; do not bypass the limit by decrypting
  replica files directly.
