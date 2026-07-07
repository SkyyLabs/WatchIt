"""Hosting config: env-driven CORS + per-environment extension build."""
import json
import re
import sys
from pathlib import Path

import pytest

from watchit_core.config import Settings, settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_extension import build  # noqa: E402


# --- CORS ------------------------------------------------------------------------

def test_cors_default_is_local_dashboard():
    assert settings.cors_origin_list() == ["http://127.0.0.1:4848", "http://localhost:4848"]


def test_cors_env_parsing_trims_and_drops_empties():
    custom = Settings(WATCHIT_CORS_ORIGINS=" https://app.example.com , http://localhost:4848 ,, ")
    assert custom.cors_origin_list() == ["https://app.example.com", "http://localhost:4848"]


def test_main_uses_env_cors():
    import inspect

    from watchit_api import main

    src = inspect.getsource(main)
    assert "settings.cors_origin_list()" in src
    assert '"http://127.0.0.1:4848","http://localhost:4848"' not in src


# --- extension build ----------------------------------------------------------------

def _config_values(out_dir: Path):
    text = (out_dir / "config.js").read_text()
    api_base = json.loads(re.search(r"apiBase: (\".*?\")", text).group(1))
    skip_hosts = json.loads(re.search(r"skipHosts: (\[.*?\])", text, re.S).group(1))
    return api_base, skip_hosts, text


def test_build_stamps_api_base_and_derives_skip_hosts(tmp_path):
    out = build("https://api.example.com/", None, tmp_path / "ext")
    api_base, skip_hosts, _ = _config_values(out)
    assert api_base == "https://api.example.com"  # trailing slash stripped
    assert skip_hosts[0] == "api.example.com"     # API host never monitored
    assert "127.0.0.1:4848" in skip_hosts         # local dashboard kept
    # full extension copied, dependency-free
    for name in ("manifest.json", "background.js", "content.js", "popup.html", "popup.js"):
        assert (out / name).exists()


def test_build_respects_explicit_skip_hosts(tmp_path):
    out = build("https://api.example.com", ["api.example.com", "app.example.com"], tmp_path / "ext")
    _, skip_hosts, _ = _config_values(out)
    assert skip_hosts == ["api.example.com", "app.example.com"]


def test_build_rejects_non_http_base(tmp_path):
    with pytest.raises(SystemExit):
        build("api.example.com", None, tmp_path / "ext")
