"""Offline tests for the combined YuE2 and ACE-Step Modal setup script."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_setup_spec = importlib.util.spec_from_file_location(
    "timbor_modal_configure", ROOT / "modal" / "configure_music_models.py")
assert _setup_spec and _setup_spec.loader
setup = importlib.util.module_from_spec(_setup_spec)
_setup_spec.loader.exec_module(setup)


def test_setup_preserves_existing_dotenv_and_generates_separate_keys(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    template_path = tmp_path / ".env.example"
    template_path.write_text(
        "[TEMPLATE]\nEXISTING=value\nYUE2_API_KEY=\nACESTEP_ENABLED=false\n",
        encoding="utf-8")
    monkeypatch.setattr(setup, "ENV_PATH", env_path)
    monkeypatch.setattr(setup, "ENV_EXAMPLE", template_path)

    lines = setup._initial_env()
    assert "[TEMPLATE]" not in lines
    generated = setup._ensure_api_keys(lines)
    setup._set_value(lines, "YUE2_MODAL_URL", "https://team--timbor-yue2-yue2service-web.modal.run")
    setup._save_env(lines)

    saved = env_path.read_text(encoding="utf-8")
    assert "EXISTING=value" in saved
    assert "ACESTEP_ENABLED=false" in saved
    assert "YUE2_API_KEY=" + setup._get_value(lines, "YUE2_API_KEY") in saved
    assert "ACESTEP_API_KEY=" + setup._get_value(lines, "ACESTEP_API_KEY") in saved
    assert setup._get_value(lines, "YUE2_API_KEY") != setup._get_value(lines, "ACESTEP_API_KEY")
    assert set(generated) == {"YUE2_API_KEY", "ACESTEP_API_KEY"}


def test_setup_updates_existing_values_and_detects_only_matching_invocation_urls():
    lines = ["YUE2_MODAL_URL=https://old.modal.run", "YUE2_MODAL_URL=https://duplicate.modal.run"]
    setup._set_value(lines, "YUE2_MODAL_URL", "https://team--timbor-yue2-yue2service-web.modal.run")
    assert setup._get_value(lines, "YUE2_MODAL_URL") == "https://team--timbor-yue2-yue2service-web.modal.run"
    assert any("duplicate YUE2_MODAL_URL" in line for line in lines)

    output = (
        "Dashboard https://modal.com/apps/team/timbor-yue2 "
        "Invocation URL: https://team--timbor-yue2-yue2service-web.modal.run\n"
        "Other app: https://team--timbor-acestep-acestepservice-web.modal.run."
    )
    assert setup._extract_url(output, "timbor-yue2") == (
        "https://team--timbor-yue2-yue2service-web.modal.run")
    assert setup._extract_url(output, "timbor-acestep") == (
        "https://team--timbor-acestep-acestepservice-web.modal.run")
    assert setup._extract_url("No endpoint was printed", "timbor-yue2") is None


def test_setup_redacts_both_bearer_keys_from_deploy_output():
    lines = ["YUE2_API_KEY=secret-yue", "ACESTEP_API_KEY=secret-ace"]
    assert setup._redact("keys secret-yue and secret-ace", lines) == (
        "keys [REDACTED] and [REDACTED]")
