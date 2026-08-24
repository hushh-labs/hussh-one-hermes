# PKM KYC/KYB & Cross-Form Onboarding Automation Reference

This reference details the canonical domain schemas, field mappings, and execution flow for automating repetitive KYC, KYB, and compliance filings (such as Gusto, Stripe, Mercury, FinCEN BOIR, and State Tax registrations) using the Hussh One Personal Knowledge Model (PKM).

---

## 1. Core Architecture & Philosophy

The Hussh One PKM vault (`~/.hermes/hussh-one/`) holds sovereign, Keychain-encrypted domain replicas. Instead of re-entering sensitive personal identifiers (SSNs, EINs, home addresses, bank routing) into every vendor portal:
1. **Canonical Storage:** Verified identity, entity, banking, and tax data live in local PKM domains.
2. **Least-Privilege Scoping:** When an agent assists with an onboarding form (e.g. Gusto), it requests only the narrow scope path required (e.g. `attr.identity.profile`, `attr.legal_entity.entity`).
3. **Deterministic Transformation:** The agent transforms canonical domain values into target API payloads or form values.
4. **Owner Consent & Dry-Run Preview:** The agent generates a field-by-field dry-run comparison and requires owner approval before submitting.
5. **Source Document Verification:** Attributes reference pinned source artifacts in the Source Library (`cp575.pdf`, `passport.pdf`, `voided_check.pdf`).

---

## 2. Canonical PKM Schemas for Compliance

### A. Domain: `identity` (`scope_path: attr.identity.*`)
```json
{
  "profile": {
    "first_name": "string",
    "middle_name": "string",
    "last_name": "string",
    "suffix": "string",
    "date_of_birth": "YYYY-MM-DD",
    "ssn": "9-digit string without hyphens",
    "citizenship": "ISO-3166-1 alpha-2 (e.g. US)",
    "phone": "E.164 string (+1...)",
    "email": "string",
    "residential_address": {
      "street_1": "string",
      "street_2": "string",
      "city": "string",
      "state": "2-letter uppercase",
      "zip": "5 or 9-digit string",
      "country": "US"
    }
  },
  "verified_credentials": {
    "passport": {
      "number": "string",
      "issuing_country": "US",
      "expiration_date": "YYYY-MM-DD",
      "source_document_ref": "source://pkm/documents/passport.pdf"
    },
    "drivers_license": {
      "number": "string",
      "issuing_state": "2-letter",
      "expiration_date": "YYYY-MM-DD",
      "source_document_ref": "source://pkm/documents/drivers_license.pdf"
    }
  }
}
```

### B. Domain: `legal_entity` (`scope_path: attr.legal_entity.*`)
```json
{
  "entity": {
    "legal_name": "string",
    "trade_name_dba": "string",
    "entity_type": "LLC | C_CORP | S_CORP | PARTNERSHIP | SOLE_PROP",
    "tax_classification": "C_CORP | S_CORP | PARTNERSHIP | DISREGARDED",
    "formation_state": "2-letter uppercase",
    "formation_date": "YYYY-MM-DD",
    "fein": "9-digit string without hyphens",
    "naics_code": "6-digit string",
    "sic_code": "4-digit string",
    "industry_description": "string",
    "registered_address": {
      "street_1": "string",
      "street_2": "string",
      "city": "string",
      "state": "2-letter",
      "zip": "string"
    },
    "principal_work_location": {
      "street_1": "string",
      "street_2": "string",
      "city": "string",
      "state": "2-letter",
      "zip": "string"
    },
    "website": "https://...",
    "corporate_phone": "E.164 string"
  },
  "beneficial_ownership": [
    {
      "identity_ref": "attr.identity.profile",
      "title": "Chief Executive Officer",
      "ownership_percentage": 100.0,
      "is_control_person": true,
      "is_signatory": true
    }
  ],
  "verified_documents": {
    "cp575_ein_letter": "source://pkm/documents/ein_cp575.pdf",
    "articles_of_organization": "source://pkm/documents/articles_of_org.pdf"
  }
}
```

### C. Domain: `financial` (`scope_path: attr.financial.*`)
```json
{
  "operating_bank_account": {
    "bank_name": "string",
    "account_holder_name": "string",
    "account_type": "checking | savings",
    "routing_number": "9-digit string",
    "account_number": "string",
    "ach_sweep_authorized": true,
    "verified_documents": {
      "voided_check": "source://pkm/documents/voided_check.pdf",
      "bank_verification_letter": "source://pkm/documents/bank_letter.pdf"
    }
  }
}
```

### D. Domain: `tax_record` (`scope_path: attr.tax_record.*`)
```json
{
  "federal": {
    "ein": "9-digit string",
    "filing_form": "941 | 944",
    "tax_payer_type": "C-Corporation | S-Corporation | LLC | LLP",
    "form_8655_signed": true
  },
  "state_accounts": {
    "DE": {
      "withholding_account_id": "string",
      "deposit_schedule": "monthly | semi-weekly",
      "unemployment_account_id": "string",
      "sui_tax_rate": 0.027,
      "sui_effective_date": "YYYY-MM-DD"
    }
  }
}
```

---

## 3. Gusto Field & Endpoint Mapping Matrix

| Gusto Onboarding Step | Target API Endpoint / Field | Source PKM Attribute Path |
| :--- | :--- | :--- |
| **Company Name** | `POST /v1/companies` -> `name` | `attr.legal_entity.entity.legal_name` |
| **EIN & Tax Details** | `PUT /v1/companies/{id}/federal_tax_details` -> `ein`, `legal_name`, `tax_payer_type`, `filing_form` | `attr.legal_entity.entity.fein`, `legal_name`, `tax_classification`, `attr.tax_record.federal.filing_form` |
| **Industry NAICS/SIC** | `PUT /v1/companies/{id}/industry_selection` -> `naics_code`, `sic_codes` | `attr.legal_entity.entity.naics_code`, `sic_code` |
| **Primary Signatory** | `POST /v1/companies/{id}/signatories` -> `first_name`, `last_name`, `title`, `email`, `phone`, `birthday`, `ssn`, `home_address` | `attr.identity.profile.*`, `attr.legal_entity.beneficial_ownership[0].title` |
| **Bank Account** | `POST /v1/companies/{id}/bank_accounts` -> `routing_number`, `account_number`, `account_type` | `attr.financial.operating_bank_account.*` |
| **State Tax IDs** | `PUT /v1/companies/{id}/tax_requirements/{state}` -> `requirement_id`, `value` | `attr.tax_record.state_accounts.{state}.*` |
| **Company Address** | `POST /v1/companies/{id}/locations` -> `street_1`, `city`, `state`, `zip` | `attr.legal_entity.entity.principal_work_location` |

---

## 4. Cross-Platform Reusability

The same canonical PKM structure services other platforms seamlessly:
* **Stripe KYB:** Consumes `legal_entity.entity`, `identity.profile`, `identity.verified_credentials.passport`, and `financial.operating_bank_account`.
* **Mercury / Brex Banking:** Consumes `legal_entity.entity`, `legal_entity.verified_documents`, `legal_entity.beneficial_ownership`, and `identity.profile`.
* **FinCEN BOIR:** Consumes `legal_entity.entity` + `legal_entity.beneficial_ownership` mapped to `identity.profile` and `identity.verified_credentials`.
* **State Labor / Revenue Filings:** Consumes `legal_entity.entity`, `identity.profile`, and `tax_record.state_accounts`.
