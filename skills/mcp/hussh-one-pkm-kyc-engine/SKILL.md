---
name: hussh-one-pkm-kyc-engine
description: "Execute cross-form KYC/KYB onboarding via Hussh One PKM."
category: mcp
---

# Hussh One PKM KYC Engine Skill

The Hussh One PKM KYC Engine automates compliance, employer, banking, and merchant onboarding across platforms (Gusto, Stripe, Mercury, FinCEN BOIR) using verified local PKM vault attributes. It reads sovereign, encrypted identity, entity, tax, and banking records under cryptographic consent and outputs deterministic form payloads.

## When to Use
- Onboarding a company or employer onto payroll services like Gusto or Rippling.
- Completing FinCEN Beneficial Ownership Information Reports (BOIR).
- Submitting merchant KYB applications (Stripe, Adyen) or business bank applications (Mercury, Brex).
- Auto-filling complex state tax registrations (SUI, Withholding) from sovereign PKM memory.

## Prerequisites
- A registered and unlocked Hussh One PKM vault (`~/.hermes/hussh-one/`).
- Populated domain records for `identity`, `legal_entity`, `financial`, and `tax_record`.
- Access to the mapper helper in `scripts/kyc_pkm_form_mapper.py`.

## How to Run
Run the deterministic mapper against local or decrypted PKM snapshots using `.venv/bin/python3` via `execute_code` or `terminal`:

```bash
.venv/bin/python3 skills/mcp/hussh-one-pkm-kyc-engine/scripts/kyc_pkm_form_mapper.py
```

## Quick Reference

| Domain Scope | Target Form / Entity | Primary Use Case |
| :--- | :--- | :--- |
| `attr.identity.profile` | Signatory / Officer KYC | SSN, DOB, Name, Residential Address |
| `attr.legal_entity.entity` | Company KYB / FinCEN BOIR | FEIN, Legal Name, Entity Type, NAICS |
| `attr.financial.operating_bank_account` | Payroll & ACH Funding | Bank Routing, Account Number, Plaid |
| `attr.tax_record.state_accounts` | State Tax Departments | SUI Account ID, Tax Rate, Withholding ID |

## Procedure

1. **Inspect Vault Readiness:**
   Use `read_file` to check `~/.hermes/hussh-one/vault-lock-state.json` and ensure `locked: false`.

2. **Scoped Attribute Retrieval:**
   Use `read_my_pkm` for the narrowest required scopes in `identity`,
   `legal_entity`, `financial`, and `tax_record`. Never open replica files or
   invoke private decryption helpers. The returned values are authorized,
   memory-only projections; the stored replica remains ciphertext.

3. **Validate & Transform:**
   Invoke `validate_pkm_kyc_data()` and map the data to the target service payload (e.g., `map_to_gusto_payloads()` or `map_to_fincen_boir_payload()`).

4. **Generate Dry-Run Preview:**
   Execute `generate_dry_run_preview(target, pkm_data)` and present the field summary to the user for explicit confirmation.

5. **Execute Under Consent:**
   Upon user confirmation, submit the payload to the destination API or complete the form fields. Log the completed transaction in the local audit log.

## Mandatory PKM Storage & Dry-Run Confirmation Mandate

Whenever populating or mutating PKM records from external sources (such as Gmail, OneDrive, bank portals):
1. **Multimodal Model Ingestion (No Heuristic Guessing):** Primary ID documents (passports, SSN cards, PAN cards, EAD cards, offer letters) MUST be processed directly with the active multimodal model provider (e.g., `gemini-3.7-flash` via `extract_pkm_from_sources.py`) on full-resolution raw images/PDF streams, avoiding downscaled thumbnails or synthetic defaults.
2. **Confirmation Breakdown:** The agent MUST present a structured domain breakdown showing exact fields, source provenance, and target KYC payload mapping before committing.
3. **Interactive Confirmation:** All external writes or local vault commits require explicit owner confirmation.
4. **Auditability:** Every mapped payload carries timestamps and origin references for full traceability.

Never combine real plaintext with `[VAULT_ENCRYPTED]`, `[VAULT_REF:*]`, or
similar sentinels and call the result vault JSON. Schema documents contain
field paths only. Exact values come only from an authorized `read_my_pkm` call,
and a passphrase, vault key, recovery key, or connector key must never be
requested in chat.

## Pitfalls
- **Unchecked SSN/EIN formatting:** APIs reject dashes in FEIN and SSN. Always pass values through `normalize_digits()`.
- **Missing SUI tax rates:** State unemployment registrations require the specific active employer contribution rate, not just the account ID.
- **Physical vs P.O. Box addresses:** Compliance KYC/KYB services reject P.O. Box addresses for company headquarters and signatory home addresses.
- **No unapproved writes:** Never submit live compliance or tax forms without presenting the dry-run summary and receiving explicit user consent.

## Verification
- Validate generated payloads against the JSON schemas in `references/canonical-kyc-pkm-schema.json`.
- Confirm that all sensitive fields (SSN, routing, account numbers) are masked in preview logs.
- Run tests via `scripts/run_tests.sh tests/skills/test_hussh_one_pkm_kyc_engine_skill.py -q`.
