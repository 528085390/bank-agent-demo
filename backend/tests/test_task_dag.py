import dataclasses

import pytest

from app.agents.task_dag import TaskNode, TaskPlan, label_for_domain_task


def test_task_node_immutability_and_as_dict():
    node = TaskNode(
        id="node_1",
        label="查询账单与结余",
        owner="bill",
        depends_on=("node_0",),
        status="pending",
        artifact={"total": 500},
    )
    d = node.as_dict()
    assert d["id"] == "node_1"
    assert d["label"] == "查询账单与结余"
    assert d["owner"] == "bill"
    assert d["depends_on"] == ["node_0"]
    assert d["status"] == "pending"
    assert d["artifact"] == {"total": 500}
    assert d["error"] is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.status = "done"


def test_task_plan_as_dict():
    node = TaskNode(id="n1", label="转账", owner="transfer")
    plan = TaskPlan(plan_id="plan_123", nodes=(node,))
    d = plan.as_dict()
    assert d["plan_id"] == "plan_123"
    assert len(d["nodes"]) == 1
    assert d["nodes"][0]["id"] == "n1"


def test_task_node_rejects_illegal_status():
    with pytest.raises(ValueError, match="bogus"):
        TaskNode(id="n1", label="转账", owner="transfer", status="bogus")


def test_label_for_domain_task():
    assert label_for_domain_task("bill", "查上月账单") == "账单查询与分析 · 查上月账单"
    assert label_for_domain_task("transfer", "") == "资金转账办理"
    long_inst = "向张三的中国工商银行卡转账一千元用于采购生产设备耗材"
    short_label = label_for_domain_task("transfer", long_inst)
    assert len(short_label) < 35
    assert short_label.endswith("…")
