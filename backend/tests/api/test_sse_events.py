import json
import re
from pathlib import Path
import pytest
from app.api.sse import EVENT_NAMES, encode_event


class DummyEvent:
    def __init__(self, event: str, data: dict):
        self.event = event
        self.data = data


def test_frozen_sse_events_include_new_control_events():
    assert "plan" in EVENT_NAMES
    assert "task_update" in EVENT_NAMES
    assert "clock" in EVENT_NAMES


def test_encode_new_sse_events():
    for name in ["plan", "task_update", "clock"]:
        ev = DummyEvent(event=name, data={"test_key": "val"})
        encoded = encode_event(ev, request_id="req-123")
        assert f"event: {name}\n" in encoded
        assert '"request_id": "req-123"' in encoded
        assert '"test_key": "val"' in encoded


def test_encode_unknown_event_raises():
    ev = DummyEvent(event="invalid_custom_event", data={})
    with pytest.raises(ValueError, match="unknown SSE event"):
        encode_event(ev, request_id="req-123")


def test_backend_sse_events_subset_of_frontend_allowlist():
    """跨端一致性守门断言：后端 EVENT_NAMES 必须是前端 AGENT_EVENT_NAMES 的子集，防止静默丢弃。"""
    repo_root = Path(__file__).resolve().parents[2].parent
    frontend_api_path = repo_root / "frontend" / "src" / "api.ts"
    assert frontend_api_path.exists(), f"Frontend api.ts not found at {frontend_api_path}"
    content = frontend_api_path.read_text(encoding="utf-8")

    match = re.search(r"AGENT_EVENT_NAMES.*?=\s*\[(.*?)\]", content, re.DOTALL)
    assert match, "Cannot find AGENT_EVENT_NAMES in frontend api.ts"
    raw_names = match.group(1)
    frontend_events = set(re.findall(r"['\"]([a-zA-Z0-9_]+)['\"]", raw_names))

    missing = EVENT_NAMES - frontend_events
    assert not missing, f"Backend EVENT_NAMES has events missing from frontend allowlist: {missing}"
