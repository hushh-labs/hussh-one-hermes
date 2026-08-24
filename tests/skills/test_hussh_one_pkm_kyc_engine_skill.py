# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
from pathlib import Path
import re

import pytest

SKILL_DIR = (
    Path(__file__).parents[2]
    / "skills"
    / "mcp"
    / "hussh-one-pkm-kyc-engine"
)

MAPPER_SCRIPT = SKILL_DIR / "scripts" / "kyc_pkm_form_mapper.py"

# Dynamically import kyc_pkm_form_mapper
spec = importlib.util.spec_from_file_location("kyc_pkm_form_mapper", MAPPER_SCRIPT)
assert spec is not None and spec.loader is not None
mapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapper)

normalize_digits = mapper.normalize_digits
validate_pkm_kyc_data = mapper.validate_pkm_kyc_data
map_to_gusto_payloads = mapper.map_to_gusto_payloads
map_to_fincen_boir_payload = mapper.map_to_fincen_boir_payload
generate_dry_run_preview = mapper.generate_dry_run_preview


@pytest.fixture
def sample_pkm_data():
    return {
        "identity": {
            "profile": {
                "first_name": "Kushal",
                "middle_name": "Kumar",
                "last_name": "Trivedi",
                "suffix": "",
                "date_of_birth": "1990-01-15",
                "ssn": "123-45-6789",
                "citizenship": "US",
                "phone": "+1 (201) 241-9368",
                "email": "kushaltrivedi1711@gmail.com",
                "residential_address": {
                    "street_1": "123 Main St",
                    "street_2": "Apt 4B",
                    "city": "Jersey City",
                    "state": "NJ",
                    "zip": "07302",
                    "country": "US",
                },
            },
            "verified_credentials": {
                "passport": {
                    "number": "A12345678",
                    "issuing_country": "US",
                    "expiration_date": "2032-05-15",
                }
            },
        },
        "legal_entity": {
            "entity": {
                "legal_name": "Hushh Technologies LLC",
                "trade_name_dba": "Hussh",
                "entity_type": "LLC",
                "tax_classification": "C_CORP",
                "formation_state": "DE",
                "formation_date": "2023-01-15",
                "fein": "12-3456789",
                "naics_code": "541511",
                "sic_code": "7371",
                "industry_description": "Custom Computer Programming Services",
                "registered_address": {
                    "street_1": "1209 Orange St",
                    "street_2": "",
                    "city": "Wilmington",
                    "state": "DE",
                    "zip": "19801",
                },
                "principal_work_location": {
                    "street_1": "123 Business Way",
                    "street_2": "Suite 500",
                    "city": "New York",
                    "state": "NY",
                    "zip": "10001",
                },
                "corporate_phone": "2012419368",
            },
            "beneficial_ownership": [
                {
                    "title": "Chief Executive Officer",
                    "ownership_percentage": 100.0,
                    "is_control_person": True,
                    "is_signatory": True,
                }
            ],
        },
        "financial": {
            "operating_bank_account": {
                "bank_name": "Mercury (Choice Financial Group)",
                "account_holder_name": "Hushh Technologies LLC",
                "account_type": "checking",
                "routing_number": "123456789",
                "account_number": "9876543210",
                "ach_sweep_authorized": True,
            }
        },
        "tax_record": {
            "federal": {
                "ein": "123456789",
                "filing_form": "941",
                "tax_payer_type": "C-Corporation",
            },
            "state_accounts": {
                "DE": {
                    "withholding_account_id": "DE-WTH-100",
                    "unemployment_account_id": "DE-SUI-200",
                    "sui_tax_rate": 0.027,
                    "deposit_schedule": "monthly",
                }
            },
        },
    }


def test_skill_manifest_standards():
    skill_file = SKILL_DIR / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text()

    desc_match = re.search(r"^description: \"([^\"]+)\"", content, re.MULTILINE)
    assert desc_match is not None, "description frontmatter missing"
    description = desc_match.group(1)
    assert len(description) <= 60, f"Description too long: {len(description)} chars"
    assert description.endswith("."), "Description must end with a period"

    # Verify standard sections
    assert "## When to Use" in content
    assert "## Prerequisites" in content
    assert "## How to Run" in content
    assert "## Quick Reference" in content
    assert "## Procedure" in content
    assert "## Pitfalls" in content
    assert "## Verification" in content


def test_canonical_schema_is_valid_json():
    schema_file = SKILL_DIR / "references" / "canonical-kyc-pkm-schema.json"
    assert schema_file.exists()
    data = json.loads(schema_file.read_text())
    assert data.get("title") == "HusshOnePkmKycDomains"
    assert "identity" in data["properties"]
    assert "legal_entity" in data["properties"]
    assert "financial" in data["properties"]
    assert "tax_record" in data["properties"]


def test_normalize_digits():
    assert normalize_digits("12-3456789") == "123456789"
    assert normalize_digits("+1 (201) 241-9368") == "12012419368"
    assert normalize_digits(None) == ""
    assert normalize_digits(12345) == "12345"


def test_validate_pkm_kyc_data(sample_pkm_data):
    errors = validate_pkm_kyc_data(sample_pkm_data)
    assert errors == []

    # Test missing field
    incomplete_data = {"identity": {"profile": {}}}
    errs = validate_pkm_kyc_data(incomplete_data)
    assert len(errs) > 0
    assert any("first_name" in e for e in errs)
    assert any("fein" in e for e in errs)


def test_map_to_gusto_payloads(sample_pkm_data):
    payloads = map_to_gusto_payloads(sample_pkm_data)

    # Check company payload
    assert payloads["company"]["name"] == "Hushh Technologies LLC"
    assert payloads["company"]["trade_name"] == "Hussh"

    # Check federal tax payload
    assert payloads["federal_tax"]["ein"] == "123456789"
    assert payloads["federal_tax"]["filing_form"] == "941"

    # Check industry
    assert payloads["industry"]["naics_code"] == "541511"
    assert payloads["industry"]["sic_codes"] == ["7371"]

    # Check signatory
    assert payloads["signatory"]["first_name"] == "Kushal"
    assert payloads["signatory"]["last_name"] == "Trivedi"
    assert payloads["signatory"]["ssn"] == "123456789"
    assert payloads["signatory"]["title"] == "Chief Executive Officer"
    assert payloads["signatory"]["home_address"]["city"] == "Jersey City"

    # Check bank account
    assert payloads["bank_account"]["routing_number"] == "123456789"
    assert payloads["bank_account"]["account_number"] == "9876543210"
    assert payloads["bank_account"]["account_type"] == "Checking"

    # Check state tax
    assert "DE" in payloads["state_taxes"]
    assert payloads["state_taxes"]["DE"]["withholding_account_id"] == "DE-WTH-100"
    assert payloads["state_taxes"]["DE"]["sui_tax_rate"] == 0.027


def test_map_to_fincen_boir_payload(sample_pkm_data):
    boir = map_to_fincen_boir_payload(sample_pkm_data)
    assert boir["reporting_company"]["legal_name"] == "Hushh Technologies LLC"
    assert boir["reporting_company"]["tax_id_number"] == "123456789"
    assert boir["reporting_company"]["formation_jurisdiction_state"] == "DE"

    assert len(boir["beneficial_owners"]) == 1
    owner = boir["beneficial_owners"][0]
    assert owner["legal_name"]["first"] == "Kushal"
    assert owner["identifying_document"]["type"] == "passport"
    assert owner["identifying_document"]["number"] == "A12345678"


def test_generate_dry_run_preview(sample_pkm_data):
    gusto_preview = generate_dry_run_preview("gusto", sample_pkm_data)
    assert "GUSTO ONBOARDING DRY-RUN PREVIEW" in gusto_preview
    assert "Hushh Technologies LLC" in gusto_preview
    assert "Kushal Trivedi" in gusto_preview
    assert "Form 941" in gusto_preview

    boir_preview = generate_dry_run_preview("fincen", sample_pkm_data)
    assert "FinCEN BOIR FILING DRY-RUN PREVIEW" in boir_preview
    assert "Hushh Technologies LLC" in boir_preview


def test_extract_pkm_from_sources_helpers(tmp_path, monkeypatch):
    extractor_script = SKILL_DIR / "scripts" / "extract_pkm_from_sources.py"
    spec_ext = importlib.util.spec_from_file_location("extract_pkm_from_sources", extractor_script)
    assert spec_ext is not None and spec_ext.loader is not None
    extractor = importlib.util.module_from_spec(spec_ext)
    spec_ext.loader.exec_module(extractor)

    # Test model resolution
    monkeypatch.setenv("AGENT_GEMINI_MODEL", "gemini-3.7-flash")
    assert extractor.resolve_active_model() == "gemini-3.7-flash"

    # Test JSON parsing
    raw_json = '```json\n{"name": "Kushal Ketan Trivedi", "ssn": "270-81-4901"}\n```'
    parsed = extractor.parse_extracted_json(raw_json)
    assert parsed["name"] == "Kushal Ketan Trivedi"
    assert parsed["ssn"] == "270-81-4901"

    # Test PDF JPEG extraction mock
    test_pdf = tmp_path / "test.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 header \xff\xd8\xff\xe0\x00\x10JFIF...\xff\xd9 trailing")
    extracted_jpeg = extractor.extract_jpeg_from_pdf(test_pdf)
    assert extracted_jpeg is not None
    assert extracted_jpeg.startswith(b"\xff\xd8\xff")
    assert extracted_jpeg.endswith(b"\xff\xd9")

