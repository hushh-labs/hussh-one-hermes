from pathlib import Path
import subprocess


def test_fresh_config_defaults_to_hussh_one(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from hermes_cli.config import load_config

    cfg = load_config()

    assert cfg["brand"]["display_name"] == "🤫 Hussh One"
    assert cfg["display"]["skin"] == "hussh-one"
    assert cfg["dashboard"]["theme"] == "hussh-one"


def test_repo_shipped_hussh_one_skin_loads():
    from hermes_cli.skin_engine import load_skin

    skin = load_skin("hussh-one")

    assert skin.name == "hussh-one"
    assert skin.get_branding("agent_name") == "🤫 Hussh One"


def test_repo_shipped_hussh_one_dashboard_theme_is_discovered():
    from hermes_cli.web_server import _discover_repo_themes

    themes = {theme["name"]: theme for theme in _discover_repo_themes()}

    assert "hussh-one" in themes
    assert themes["hussh-one"]["label"] == "🤫 Hussh One"


def test_tracked_files_do_not_reference_old_puppy_brand():
    root = Path(__file__).resolve().parents[2]
    needles = ("hussh puppy", "hushh-puppy", "HUSSH_PUPPY")
    tracked = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()

    for rel in tracked:
        path = root / rel
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.lower()
        assert all(needle.lower() not in lowered for needle in needles), str(path)
