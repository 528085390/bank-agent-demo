from app.agents.runtime import EVENT_FIELDS, _custom_event


def test_runtime_event_fields_contain_plan_and_task_update():
    assert "plan" in EVENT_FIELDS
    assert EVENT_FIELDS["plan"] == ("plan_id", "nodes")
    assert "task_update" in EVENT_FIELDS
    assert EVENT_FIELDS["task_update"] == ("plan_id", "node_id", "status", "artifact", "error")


def test_custom_event_adapts_plan_and_task_update():
    plan_payload = {
        "event": "plan",
        "plan_id": "p_1",
        "nodes": [{"id": "n1", "label": "test", "owner": "bill", "status": "pending"}],
    }
    events = _custom_event(plan_payload)
    assert len(events) == 1
    assert events[0].event == "plan"
    assert events[0].data["plan_id"] == "p_1"
    assert len(events[0].data["nodes"]) == 1

    update_payload = {
        "event": "task_update",
        "plan_id": "p_1",
        "node_id": "n1",
        "status": "running",
        "artifact": None,
        "error": None,
    }
    events = _custom_event(update_payload)
    assert len(events) == 1
    assert events[0].event == "task_update"
    assert events[0].data["status"] == "running"
