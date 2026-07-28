from pathlib import Path


SKILL = (
    Path(__file__).parents[2]
    / "skills"
    / "mcp"
    / "hussh-consent-mcp-workflow"
    / "SKILL.md"
)


def test_direct_data_flow_has_one_route_and_safe_failure_copy() -> None:
    contract = SKILL.read_text()

    assert (
        "`search_user_scopes` → `request_consent` → `check_consent_status` →\n"
        "  `get_encrypted_scoped_export`"
    ) in contract
    assert "`prepare_campaign_context` is a campaign/offer compatibility helper" in contract
    assert "call it as a fallback, diagnostic probe" in contract
    assert "do not emit raw connector error JSON" in contract
    assert (
        '"Your trusted Hushh connector is not ready on this device. Complete\n'
        '  connector setup, then retry."'
    ) in contract
