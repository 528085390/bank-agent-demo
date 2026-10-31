"""任务 DAG 与节点状态定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

TaskNodeStatus = Literal[
    "pending",  # 等待执行
    "running",  # 正在执行
    "waiting_confirm",  # 暂停等待用户确认 (HITL / MFA)
    "done",  # 执行成功完成
    "failed",  # 执行失败
    "skipped",  # 因前序失败被跳过
]


@dataclass(frozen=True, slots=True)
class TaskNode:
    """DAG 中的单个任务节点。"""

    id: str  # 节点唯一标识，如 "node_1", "node_2"
    label: str  # 节点展示名称，如 "查询账单与结余"
    owner: str  # 所属智能体 agent_id，如 "bill", "transfer"
    depends_on: tuple[str, ...] = ()  # 前序依赖节点 id 列表
    status: TaskNodeStatus = "pending"  # 当前状态
    artifact: dict[str, Any] | None = None  # 节点输出的结构化结果摘要
    error: str | None = None  # 失败原因（若有）

    def __post_init__(self) -> None:
        if self.status not in get_args(TaskNodeStatus):
            raise ValueError(f"非法的任务节点状态: {self.status!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "owner": self.owner,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "artifact": dict(self.artifact) if self.artifact else None,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """一轮对话中的完整任务图。"""

    plan_id: str
    nodes: tuple[TaskNode, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "nodes": [node.as_dict() for node in self.nodes],
        }


def label_for_domain_task(agent_id: str, instruction: str) -> str:
    """为任务节点生成清晰、人类可读的中文标题。"""
    DOMAIN_NAME_MAP = {
        "bill": "账单查询与分析",
        "transfer": "资金转账办理",
        "card": "信用卡与账户管理",
        "wealth": "理财持仓与申赎",
        "subscription": "免密扣费管理",
        "life_event": "生活事件与备忘",
    }
    prefix = DOMAIN_NAME_MAP.get(agent_id, f"领域任务 ({agent_id})")
    clean_inst = (instruction or "").strip()
    if clean_inst:
        # 取简短前缀，不超过 20 字
        short_desc = clean_inst[:20] + ("…" if len(clean_inst) > 20 else "")
        return f"{prefix} · {short_desc}"
    return prefix
