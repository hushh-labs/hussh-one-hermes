# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Hussh One PKM Cross-Form KYC/KYB Mapper.

Transforms sovereign, encrypted PKM domain data (identity, legal_entity,
financial, tax_record) into deterministic payloads for compliance and onboarding
APIs (Gusto, Stripe, FinCEN BOIR, Mercury) with least-privilege scoping and
dry-run validation.
"""

import json
import re
from typing import Any, Dict, List, Optional


def normalize_digits(value: Optional[str]) -> str:
    """Strip non-digit characters from SSN, EIN, or phone numbers."""
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def validate_pkm_kyc_data(pkm_data: Dict[str, Any]) -> List[str]:
    """Validate that essential PKM domain fields are present for KYC/KYB flows."""
    errors: List[str] = []

    # Check identity
    identity = pkm_data.get("identity", {}).get("profile", {})
    if not identity.get("first_name"):
        errors.append("identity.profile.first_name is required")
    if not identity.get("last_name"):
        errors.append("identity.profile.last_name is required")
    if not identity.get("ssn"):
        errors.append("identity.profile.ssn is required")
    if not identity.get("date_of_birth"):
        errors.append("identity.profile.date_of_birth is required")
    if not identity.get("residential_address"):
        errors.append("identity.profile.residential_address is required")

    # Check legal_entity
    entity = pkm_data.get("legal_entity", {}).get("entity", {})
    if not entity.get("legal_name"):
        errors.append("legal_entity.entity.legal_name is required")
    if not entity.get("fein"):
        errors.append("legal_entity.entity.fein is required")
    if not entity.get("entity_type"):
        errors.append("legal_entity.entity.entity_type is required")

    return errors


def map_to_gusto_payloads(pkm_data: Dict[str, Any]) -> Dict[str, Any]:
    """Map PKM domain attributes to Gusto Embedded Payroll API payloads."""
    identity_profile = pkm_data.get("identity", {}).get("profile", {})
    res_addr = identity_profile.get("residential_address", {})
    entity = pkm_data.get("legal_entity", {}).get("entity", {})
    work_loc = entity.get("principal_work_location", {})
    bank = pkm_data.get("financial", {}).get("operating_bank_account", {})
    tax_fed = pkm_data.get("tax_record", {}).get("federal", {})
    tax_states = pkm_data.get("tax_record", {}).get("state_accounts", {})

    # Extract primary signatory role
    beneficial_owners = pkm_data.get("legal_entity", {}).get("beneficial_ownership", [])
    signatory_title = "Officer"
    for bo in beneficial_owners:
        if bo.get("is_signatory") or bo.get("is_control_person"):
            signatory_title = bo.get("title", "Officer")
            break

    # 1. Company Creation Payload
    company_payload = {
        "name": entity.get("legal_name", ""),
        "trade_name": entity.get("trade_name_dba", ""),
    }

    # 2. Federal Tax Details Payload
    federal_tax_payload = {
        "legal_name": entity.get("legal_name", ""),
        "ein": normalize_digits(entity.get("fein", "")),
        "tax_payer_type": entity.get("tax_classification", "C_CORP"),
        "filing_form": tax_fed.get("filing_form", "941"),
    }

    # 3. Industry Selection Payload
    industry_payload = {
        "naics_code": str(entity.get("naics_code", "")),
        "sic_codes": [str(entity.get("sic_code", ""))] if entity.get("sic_code") else [],
    }

    # 4. Signatory Creation Payload
    signatory_payload = {
        "first_name": identity_profile.get("first_name", ""),
        "last_name": identity_profile.get("last_name", ""),
        "title": signatory_title,
        "email": identity_profile.get("email", ""),
        "phone": normalize_digits(identity_profile.get("phone", "")),
        "birthday": identity_profile.get("date_of_birth", ""),
        "ssn": normalize_digits(identity_profile.get("ssn", "")),
        "home_address": {
            "street_1": res_addr.get("street_1", ""),
            "street_2": res_addr.get("street_2", ""),
            "city": res_addr.get("city", ""),
            "state": res_addr.get("state", ""),
            "zip": res_addr.get("zip", ""),
        },
    }

    # 5. Bank Account Payload
    bank_payload = {
        "routing_number": normalize_digits(bank.get("routing_number", "")),
        "account_number": normalize_digits(bank.get("account_number", "")),
        "account_type": str(bank.get("account_type", "checking")).capitalize(),
    }

    # 6. Company Location Payload
    location_payload = {
        "street_1": work_loc.get("street_1", ""),
        "street_2": work_loc.get("street_2", ""),
        "city": work_loc.get("city", ""),
        "state": work_loc.get("state", ""),
        "zip": work_loc.get("zip", ""),
        "phone_number": normalize_digits(entity.get("corporate_phone", "")),
    }

    # 7. State Tax Requirements Payload
    state_tax_payloads = {}
    for state_code, state_info in tax_states.items():
        state_tax_payloads[state_code] = {
            "withholding_account_id": state_info.get("withholding_account_id", ""),
            "unemployment_account_id": state_info.get("unemployment_account_id", ""),
            "sui_tax_rate": state_info.get("sui_tax_rate", 0.027),
            "deposit_schedule": state_info.get("deposit_schedule", "monthly"),
        }

    return {
        "company": company_payload,
        "federal_tax": federal_tax_payload,
        "industry": industry_payload,
        "signatory": signatory_payload,
        "bank_account": bank_payload,
        "primary_location": location_payload,
        "state_taxes": state_tax_payloads,
    }


def map_to_fincen_boir_payload(pkm_data: Dict[str, Any]) -> Dict[str, Any]:
    """Map PKM domain attributes to FinCEN Beneficial Ownership Information Report (BOIR)."""
    entity = pkm_data.get("legal_entity", {}).get("entity", {})
    reg_addr = entity.get("registered_address", {})
    identity_profile = pkm_data.get("identity", {}).get("profile", {})
    res_addr = identity_profile.get("residential_address", {})
    credentials = pkm_data.get("identity", {}).get("verified_credentials", {})

    passport = credentials.get("passport", {})
    dl = credentials.get("drivers_license", {})

    id_doc_type = "passport" if passport else "drivers_license"
    id_doc_num = passport.get("number") or dl.get("number", "")
    id_doc_country = passport.get("issuing_country", "US")
    id_doc_state = dl.get("issuing_state", "")

    return {
        "reporting_company": {
            "legal_name": entity.get("legal_name", ""),
            "dba_name": entity.get("trade_name_dba", ""),
            "tax_id_type": "EIN",
            "tax_id_number": normalize_digits(entity.get("fein", "")),
            "formation_jurisdiction_state": entity.get("formation_state", ""),
            "formation_jurisdiction_country": "US",
            "current_us_address": {
                "street_1": reg_addr.get("street_1", ""),
                "street_2": reg_addr.get("street_2", ""),
                "city": reg_addr.get("city", ""),
                "state": reg_addr.get("state", ""),
                "zip": reg_addr.get("zip", ""),
            },
        },
        "beneficial_owners": [
            {
                "legal_name": {
                    "first": identity_profile.get("first_name", ""),
                    "middle": identity_profile.get("middle_name", ""),
                    "last": identity_profile.get("last_name", ""),
                },
                "date_of_birth": identity_profile.get("date_of_birth", ""),
                "residential_address": {
                    "street_1": res_addr.get("street_1", ""),
                    "city": res_addr.get("city", ""),
                    "state": res_addr.get("state", ""),
                    "zip": res_addr.get("zip", ""),
                    "country": res_addr.get("country", "US"),
                },
                "identifying_document": {
                    "type": id_doc_type,
                    "number": id_doc_num,
                    "issuing_jurisdiction_country": id_doc_country,
                    "issuing_jurisdiction_state": id_doc_state,
                },
            }
        ],
    }


def generate_dry_run_preview(target: str, pkm_data: Dict[str, Any]) -> str:
    """Generate a human-readable dry-run summary for user consent and approval."""
    errors = validate_pkm_kyc_data(pkm_data)
    if errors:
        return "❌ Validation Errors in PKM Data:\n" + "\n".join(f" - {err}" for err in errors)

    if target.lower() == "gusto":
        payloads = map_to_gusto_payloads(pkm_data)
        lines = [
            "===========================================================",
            "🤫 HUSSH ONE - GUSTO ONBOARDING DRY-RUN PREVIEW (CONSENT GATE)",
            "===========================================================",
            f"Company Legal Name:   {payloads['company']['name']}",
            f"Trade Name / DBA:     {payloads['company']['trade_name'] or 'N/A'}",
            f"FEIN (EIN):           {payloads['federal_tax']['ein']}",
            f"Federal Tax Form:     Form {payloads['federal_tax']['filing_form']}",
            f"Taxpayer Type:        {payloads['federal_tax']['tax_payer_type']}",
            f"NAICS Code:           {payloads['industry']['naics_code']}",
            "-----------------------------------------------------------",
            "PRIMARY SIGNATORY (KYC / IDENTITY):",
            f"  Name:               {payloads['signatory']['first_name']} {payloads['signatory']['last_name']}",
            f"  Title:              {payloads['signatory']['title']}",
            f"  DOB:                {payloads['signatory']['birthday']}",
            f"  SSN:                ***-**-{payloads['signatory']['ssn'][-4:] if len(payloads['signatory']['ssn']) >= 4 else 'XXXX'}",
            f"  Home Address:       {payloads['signatory']['home_address']['street_1']}, {payloads['signatory']['home_address']['city']}, {payloads['signatory']['home_address']['state']} {payloads['signatory']['home_address']['zip']}",
            "-----------------------------------------------------------",
            "BANKING & ACH SWEEP:",
            f"  Routing Number:     {payloads['bank_account']['routing_number']}",
            f"  Account Number:     *****{payloads['bank_account']['account_number'][-4:] if len(payloads['bank_account']['account_number']) >= 4 else 'XXXX'}",
            f"  Account Type:       {payloads['bank_account']['account_type']}",
            "-----------------------------------------------------------",
            "STATE TAX JURISDICTIONS:",
        ]
        for st, st_data in payloads["state_taxes"].items():
            lines.append(f"  [{st}] Withholding ID: {st_data['withholding_account_id']} | SUI ID: {st_data['unemployment_account_id']} | SUI Rate: {st_data['sui_tax_rate']*100:.2f}%")
        lines.append("===========================================================")
        lines.append("Status: Ready for submission. Awaiting explicit user consent.")
        return "\n".join(lines)

    elif target.lower() in ("fincen", "fincen_boir", "boir"):
        payload = map_to_fincen_boir_payload(pkm_data)
        lines = [
            "===========================================================",
            "🤫 HUSSH ONE - FinCEN BOIR FILING DRY-RUN PREVIEW",
            "===========================================================",
            f"Reporting Company:    {payload['reporting_company']['legal_name']}",
            f"EIN:                  {payload['reporting_company']['tax_id_number']}",
            f"Formation State:      {payload['reporting_company']['formation_jurisdiction_state']}",
            "BENEFICIAL OWNERS:",
        ]
        for idx, owner in enumerate(payload["beneficial_owners"], 1):
            name = f"{owner['legal_name']['first']} {owner['legal_name']['last']}"
            lines.append(f"  Owner #{idx}:          {name} (DOB: {owner['date_of_birth']})")
            lines.append(f"  ID Document:        {owner['identifying_document']['type'].upper()} #{owner['identifying_document']['number']}")
            lines.append(f"  Residential Addr:   {owner['residential_address']['street_1']}, {owner['residential_address']['city']}, {owner['residential_address']['state']}")
        lines.append("===========================================================")
        lines.append("Status: Ready for FinCEN filing submission.")
        return "\n".join(lines)

    return f"Unsupported target service: {target}"
