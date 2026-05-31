from hermes_cli.natural_model_switch import parse_natural_model_switch


def test_parse_natural_switch_opus_vertex():
    intent = parse_natural_model_switch("switch to opus 4.8")

    assert intent is not None
    assert intent.model == "claude-opus-4-8"
    assert intent.provider == "google-vertex-claude"
    assert intent.raw_args == "claude-opus-4-8 --provider google-vertex-claude"


def test_parse_natural_switch_sonnet_vertex():
    intent = parse_natural_model_switch("can you use sonnet 4.6 on vertex?")

    assert intent is not None
    assert intent.model == "claude-sonnet-4-6"
    assert intent.provider == "google-vertex-claude"
    assert intent.raw_args == "claude-sonnet-4-6 --provider google-vertex-claude"


def test_parse_natural_switch_gemini_default_provider():
    intent = parse_natural_model_switch("switch back to gemini 3.5 flash")

    assert intent is not None
    assert intent.model == "gemini-3.5-flash"
    assert intent.provider == "gemini"
    assert intent.raw_args == "gemini-3.5-flash --provider gemini"


def test_parse_natural_switch_rejects_help_question():
    assert parse_natural_model_switch("how do I switch to opus 4.8?") is None


def test_parse_natural_switch_rejects_quoted_or_pasted_context():
    assert parse_natural_model_switch("The webpage says: switch to opus 4.8") is None
    assert parse_natural_model_switch("```switch to opus 4.8```") is None
    assert parse_natural_model_switch("> switch to opus 4.8") is None


def test_parse_natural_switch_rejects_negation():
    assert parse_natural_model_switch("don't switch to opus 4.8") is None


def test_parse_natural_switch_rejects_structured_or_long_text():
    assert parse_natural_model_switch("- switch to opus 4.8") is None
    assert parse_natural_model_switch("switch to opus 4.8\n\nignore previous") is None
    assert parse_natural_model_switch("switch to opus 4.8 " + ("x" * 181)) is None
