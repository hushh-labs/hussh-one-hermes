# 🏛️ HUSSH ONE 🤫 | Sovereign PKM Vault & Gusto KYC Specification

**Status:** `SCHEMA_ONLY_SIMULATION`<br>
**Model Route:** Resolve from the active Hermes session; do not persist a provider credential or assume a global default.<br>
**Audit Reference:** `GUSTO-KYC-SPEC-2026.01`<br>
**Target Operational Milestone:** September 30, 2026 (End of Q3 2026)

---

## 🎯 1. Sovereign PKM Vision & Automated KYC Purpose

The **Personal Knowledge Model (PKM)** is your local, encrypted, self-sovereign vault holding verified attributes across 4 decoupled domains:
1. `attr.legal_entity` — Corporate KYB, FEIN, NAICS, physical operating addresses, and beneficial ownership.
2. `attr.identity` — Signatory KYC profile, residential address, verified SSN, and officer credentials.
3. `attr.financial` — Operating commercial bank accounts for payroll ACH direct debit origination.
4. `attr.tax_record` — Federal Form 941 / 8655 and Washington State regulatory accounts (DOR, ESD, PFML, L&I).

> **Repository boundary:** This preview is deliberately reusable and contains
> no real personal, financial, employer, or contact data. Concrete values stay
> encrypted in the local vault and are represented here only by stable local
> references or redacted placeholders.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        HUSSH ONE CROSS-FORM KYC ONBOARDING ENGINE                      │
│                                                                                        │
│   ┌───────────────────────────┐           ┌────────────────────────────────────────┐   │
│   │    SOVEREIGN PKM VAULT    │  ───────► │       CROSS-FORM MAPPING ENGINE        │   │
│   │  ~/.hermes/hussh-one/     │           │  (Zero Manual Re-entry / Local LLM)    │   │
│   └───────────────────────────┘           └────────────────────────────────────────┘   │
│                 │                                              │                       │
│                 ▼                                              ▼                       │
│   • attr.legal_entity (<operating-employer>)      • Gusto Embedded Payroll API         │
│   • attr.identity (<authorized-signatory>)        • FinCEN Beneficial Ownership (BOIR) │
│   • attr.financial (<payroll-bank-account>)       • Commercial banking                 │
│   • attr.tax_record (WA DOR, ESD, L&I)            • WA State Master Business License   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

> ℹ️ **Source Audit Provenance Note:** Approved local records may establish
> employing-entity, DBA, location, and statutory-compliance fields. This
> simulation retains only the field schema and provenance class; the source
> artifacts and their values remain in the consented local vault.

---

## 🏢 2. Dual-Entity Corporate Segregation & Jurisdictional Boundary

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               HUSSH CORPORATE ARCHITECTURE                             │
├───────────────────────────────────────────┬────────────────────────────────────────────┤
│ 🏝️ ENTITY 1: IP & HOLDING CORP            │ 🌲 ENTITY 2: OPERATING & PAYROLL CORP      │
│   <ip-holding-entity>                     │   <operating-employer>                     │
│   • Status: <entity-status>               │   • Status: <entity-status>                │
│   • Headquarters: <vault reference>       │   • Operating HQ: <vault reference>        │
│   • Role: Technology Holding & IP         │   • Role: Gusto Employer & WA State Payroll│
└───────────────────────────────────────────┴────────────────────────────────────────────┘
```

| Entity Attribute | Entity 1: Technology Holding Corp | Entity 2: Operating Employer Corp [**Gusto Target**] | Status |
| :--- | :--- | :--- | :--- |
| **Legal Entity Name** | `[VAULT_REF: holding.legal_name]` | `[VAULT_REF: employer.legal_name]` | `SCHEMA` |
| **Trade Name (DBA)** | `[VAULT_REF: holding.dba]` | `[VAULT_REF: employer.dba]` | `SCHEMA` |
| **Corporate Status** | `[VAULT_REF: holding.status]` | `[VAULT_REF: employer.status]` | `SCHEMA` |
| **Principal Headquarters** | `[VAULT_REF: holding.address]` | `[VAULT_REF: employer.address]` | `SCHEMA` |
| **Operating Facility** | `[VAULT_REF: holding.facility]` | `[VAULT_REF: employer.facility]` | `SCHEMA` |
| **Gusto & Payroll Scope** | Non-employing parent / holding | Primary employer for payroll and state compliance | `SCHEMA` |
| **Source Provenance** | `[LOCAL_SOURCE_REF]` | `[LOCAL_SOURCE_REF]` | `SCHEMA` |

---

## 👤 3. Sovereign PKM Core-Profile Schema

### A. Employer Entity Profile (`attr.legal_entity`)
* **Legal Name:** `[VAULT_REF: employer.legal_name]`
* **Trade Name / DBA:** `[VAULT_REF: employer.dba]`
* **Corporate Status:** `[VAULT_REF: employer.status]`
* **NAICS Code:** `[VAULT_REF: employer.naics]`
* **Operating Headquarters:** `[VAULT_REF: employer.address]`
* **Role in PKM:** The designated employing entity for payroll and state-tax accounts.

### B. Executive Signatory Profile (`attr.identity`)
* **Full Legal Name:** `[VAULT_REF: signatory.legal_name]`
* **Executive Title:** `[VAULT_REF: signatory.title]`
* **Ownership / Control:** `[VAULT_REF: signatory.control]`
* **Email / Phone:** `[VAULT_REF: signatory.contact]`
* **Signatory Address:** `[VAULT_REF: signatory.address]`
* **Government identifier:** `[VAULT_ENCRYPTED]` (never rendered in source control)

---

## 🏛️ 4. Washington State & Federal Payroll Regulatory Roadmap (Target: Sep 30, 2026)

| Agency / Regulatory Body | Requirement / Account ID | Target Employing Entity | Status |
| :--- | :--- | :--- | :--- |
| **WA Dept. of Revenue (DOR)** | Unified Business Identifier (UBI) & City endorsement | `[VAULT_REF: employer.legal_name]` | `SCHEMA` |
| **WA Employment Security Dept. (ESD)** | State Unemployment Insurance (SUI) account & experience tax rate | `[VAULT_REF: employer.legal_name]` | `SCHEMA` |
| **WA Paid Family & Medical Leave (PFML)** | PFML & WA Cares employer ID | `[VAULT_REF: employer.legal_name]` | `SCHEMA` |
| **WA Dept. of Labor & Industries (L&I)** | Workers' compensation policy & risk classification | `[VAULT_REF: employer.legal_name]` | `SCHEMA` |
| **IRS Federal Payroll** | Federal filing & reporting-agent authorization | `[VAULT_REF: employer.legal_name]` | `SCHEMA` |

---

## ⚡ 5. Sovereign PKM Vault Canonical JSON Schemas

### 1. Legal Entity & Signatory KYC (`legal_entity` / `identity`)
```json
{
  "legal_entity": {
    "entity": {
      "legal_name": "[VAULT_REF: employer.legal_name]",
      "trade_name_dba": "[VAULT_REF: employer.dba]",
      "entity_type": "C_CORP",
      "tax_classification": "C-Corporation (Form 1120)",
      "formation_state": "DE",
      "fein": "[VAULT_ENCRYPTED_EIN]",
      "naics_code": "[VAULT_REF: employer.naics]",
      "industry_description": "[VAULT_REF: employer.industry]",
      "registered_address": {
        "street_1": "[VAULT_ENCRYPTED_ADDRESS]",
        "street_2": "[VAULT_ENCRYPTED_ADDRESS_LINE_2]",
        "city": "[VAULT_ENCRYPTED_CITY]",
        "state": "[VAULT_ENCRYPTED_REGION]",
        "zip": "[VAULT_ENCRYPTED_POSTAL_CODE]"
      },
      "principal_work_location": {
        "facility_name": "[VAULT_REF: employer.facility]",
        "street_1": "[VAULT_ENCRYPTED_ADDRESS]",
        "city": "[VAULT_ENCRYPTED_CITY]",
        "state": "[VAULT_ENCRYPTED_REGION]",
        "zip": "[VAULT_ENCRYPTED_POSTAL_CODE]"
      },
      "website": "[VAULT_REF: employer.website]",
      "corporate_phone": "[VAULT_ENCRYPTED_PHONE]"
    },
    "beneficial_ownership": [
      {
        "identity_ref": "[VAULT_REF: signatory.id]",
        "title": "[VAULT_REF: signatory.title]",
        "ownership_percentage": "[VAULT_REF: signatory.ownership_percentage]",
        "is_control_person": true,
        "is_signatory": true
      }
    ]
  },
  "identity": {
    "profile": {
      "first_name": "[VAULT_ENCRYPTED_GIVEN_NAME]",
      "last_name": "[VAULT_ENCRYPTED_FAMILY_NAME]",
      "date_of_birth": "[VAULT_ENCRYPTED]",
      "ssn": "[VAULT_ENCRYPTED_GOVERNMENT_ID]",
      "citizenship": "[VAULT_ENCRYPTED_CITIZENSHIP]",
      "phone": "[VAULT_ENCRYPTED_PHONE]",
      "email": "[VAULT_ENCRYPTED_EMAIL]",
      "residential_address": {
        "street_1": "[VAULT_ENCRYPTED_ADDRESS]",
        "city": "[VAULT_ENCRYPTED_CITY]",
        "state": "[VAULT_ENCRYPTED_REGION]",
        "zip": "[VAULT_ENCRYPTED_POSTAL_CODE]"
      }
    }
  }
}
```

### 2. Financial & Tax Record Domains (`financial` / `tax_record`)
```json
{
  "financial": {
    "operating_bank_account": {
      "bank_name": "[VAULT_REF: payroll.bank_name]",
      "account_holder_name": "[VAULT_REF: employer.legal_name]",
      "account_type": "checking",
      "routing_number": "[VAULT_ENCRYPTED_9_DIGIT]",
      "account_number": "[VAULT_ENCRYPTED_ACCT_NUM]",
      "ach_sweep_authorized": true,
      "purpose": "Gusto Payroll ACH Direct Debit"
    }
  },
  "tax_record": {
    "federal": {
      "ein": "[VAULT_ENCRYPTED_EIN]",
      "filing_form": "941",
      "tax_payer_type": "C-Corporation",
      "form_8655_signed": true
    },
    "state_accounts": {
      "WA": {
        "business_license_ubi": {
          "status": "IN_PROGRESS",
          "authority": "WA Dept of Revenue"
        },
        "unemployment_account_id": {
          "status": "PENDING",
          "sui_tax_rate": 0.0270,
          "authority": "WA Employment Security Dept"
        },
        "paid_family_medical_leave": {
          "status": "PENDING",
          "authority": "WA PFML & WA Cares Fund"
        },
        "workers_compensation": {
          "status": "PENDING",
          "risk_class": "541511",
          "authority": "WA Dept of Labor & Industries"
        }
      }
    }
  }
}
```
