"""The serve script decides which interface the API listens on."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SERVE = Path(__file__).resolve().parents[1] / "scripts" / "serve.py"


def _load():
    spec = importlib.util.spec_from_file_location("serve_script_under_test", SERVE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nothing_set_means_loopback_on_eight_thousand():
    serve = _load()
    assert serve.bind_target({}) == ("127.0.0.1", 8000)


def test_a_lan_host_is_honoured():
    serve = _load()
    assert serve.bind_target({"BANK_AGENT_HOST": "0.0.0.0"}) == ("0.0.0.0", 8000)


def test_blank_values_fall_back_instead_of_binding_everything():
    serve = _load()
    assert serve.bind_target({"BANK_AGENT_HOST": "   ", "BANK_AGENT_PORT": " "}) == ("127.0.0.1", 8000)


def test_a_port_override_is_read():
    serve = _load()
    assert serve.bind_target({"BANK_AGENT_PORT": "8123"}) == ("127.0.0.1", 8123)


def test_a_nonsense_port_falls_back_to_eight_thousand():
    serve = _load()
    assert serve.bind_target({"BANK_AGENT_PORT": "eighty"}) == ("127.0.0.1", 8000)

def test_dotenv_files_are_read_and_the_process_env_wins(tmp_path):
    serve = _load()
    (tmp_path / ".env").write_text("BANK_AGENT_HOST=127.0.0.1\n", encoding="utf-8")
    (tmp_path / ".env.channels").write_text("CHANNEL_MFA_BASE_URL=http://10.0.0.5:8000\n", encoding="utf-8")
    merged = serve.env_sources(tmp_path, {"BANK_AGENT_HOST": "0.0.0.0"})
    assert merged["CHANNEL_MFA_BASE_URL"] == "http://10.0.0.5:8000"
    assert merged["BANK_AGENT_HOST"] == "0.0.0.0"
    assert serve.bind_target(merged) == ("0.0.0.0", 8000)
