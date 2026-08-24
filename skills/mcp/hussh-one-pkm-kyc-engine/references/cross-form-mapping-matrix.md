# Cross-Form KYC/KYB & Onboarding Mapping Matrix

## Overview
This matrix defines how canonical Hussh One PKM domain attributes map across major compliance, banking, and merchant onboarding services.

---

## Canonical Mapping Table

| PKM Canonical Attribute | Gusto Payroll | Stripe Connect / Custom | Mercury / Brex Banking | FinCEN BOIR Filing | State Tax Registrations |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `attr.legal_entity.entity.legal_name` | `name` / `legal_name` | `company.name` | `business.legal_name` | `reporting_company.legal_name` | `legal_entity_name` |
| `attr.legal_entity.entity.trade_name_dba` | `trade_name` | `company.tax_id_registrar` | `business.dba` | `reporting_company.dba_name` | `trade_name` |
| `attr.legal_entity.entity.fein` | `ein` (9 digits) | `company.tax_id` | `business.ein` | `reporting_company.tax_id_number` | `fein` |
| `attr.legal_entity.entity.entity_type` | `tax_payer_type` | `company.structure` | `business.entity_type` | (implied by jurisdiction) | `entity_structure` |
| `attr.legal_entity.entity.formation_state` | State dropdown | `company.address.state` | `business.state_of_incorporation` | `formation_jurisdiction_state` | `home_state` |
| `attr.legal_entity.entity.naics_code` | `naics_code` | `business_profile.mcc` | `business.naics_code` | N/A | `naics_code` |
| `attr.legal_entity.entity.registered_address` | Location record | `company.address` | `business.registered_address` | `reporting_company.current_us_address` | `legal_address` |
| `attr.identity.profile.first_name` | `signatory.first_name` | `person.first_name` | `signatory.first_name` | `beneficial_owner.name.first` | `officer.first_name` |
| `attr.identity.profile.last_name` | `signatory.last_name` | `person.last_name` | `signatory.last_name` | `beneficial_owner.name.last` | `officer.last_name` |
| `attr.identity.profile.date_of_birth` | `signatory.birthday` | `person.dob` | `signatory.dob` | `beneficial_owner.dob` | `officer.dob` |
| `attr.identity.profile.ssn` | `signatory.ssn` | `person.ssn_last_4` / `person.id_number` | `signatory.ssn` | `beneficial_owner.id_doc` | `officer.ssn` |
| `attr.identity.profile.residential_address` | `signatory.home_address` | `person.address` | `signatory.residential_address` | `beneficial_owner.residential_address` | `officer.home_address` |
| `attr.identity.profile.phone` | `signatory.phone` | `person.phone` | `signatory.phone` | N/A | `contact_phone` |
| `attr.identity.profile.email` | `signatory.email` | `person.email` | `signatory.email` | N/A | `contact_email` |
| `attr.financial.operating_bank_account.routing_number` | `bank_accounts.routing_number` | `external_account.routing_number` | N/A (Source bank) | N/A | `ach_routing_number` |
| `attr.financial.operating_bank_account.account_number` | `bank_accounts.account_number` | `external_account.account_number` | N/A (Source bank) | N/A | `ach_account_number` |
| `attr.tax_record.state_accounts.{state}.withholding_account_id` | `tax_requirements.withholding_id` | N/A | N/A | N/A | `withholding_account_number` |
| `attr.tax_record.state_accounts.{state}.unemployment_account_id` | `tax_requirements.unemployment_id` | N/A | N/A | N/A | `ui_employer_account_number` |
| `attr.tax_record.state_accounts.{state}.sui_tax_rate` | `tax_requirements.sui_rate` | N/A | N/A | N/A | `contribution_rate` |
