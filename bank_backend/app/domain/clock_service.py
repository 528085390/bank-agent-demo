"""模拟时钟与统一到期任务执行。

时钟由 simulation_state 单行保存，只能向前拨；拨钟后显式调用 run_due_tasks
触发到期任务，不使用常驻调度器。

定时转账、订阅扣款、生日订单共用同一张 scheduled_tasks 表和同一套执行逻辑，
各自通过 register_handler 注册处理器。每个任务在独立保存点内执行：
业务失败会回滚该任务的资金变更，但失败状态和审计保留，且不重试。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import BankError, BusinessError
from app.core.timeutil import iso
from app.domain import audit_service
from app.storage import tasks as task_store
from app.storage.database import Database
from app.storage.models import ScheduledTask, SimulationState

TaskHandler = Callable[[Session, ScheduledTask, "TaskContext"], dict]
FailureHook = Callable[[Session, ScheduledTask, "str | None"], None]


@dataclass
class TaskContext:
    now: datetime
    birthday_outcome: str = "success"
    extra: dict = field(default_factory=dict)


_HANDLERS: dict[str, TaskHandler] = {}
_FAILURE_HOOKS: dict[str, FailureHook] = {}


def register_handler(task_type: str, handler: TaskHandler) -> None:
    _HANDLERS[task_type] = handler


def unregister_handler(task_type: str) -> None:
    _HANDLERS.pop(task_type, None)


def register_failure_hook(task_type: str, hook: FailureHook) -> None:
    """注册任务失败后的补记逻辑。

    处理器抛错时它的保存点会整体回滚，因此处理器内写下的失败状态（例如把订单
    置为 failed、补记一条失败的账期）不会保留。这个钩子在保存点回滚之后、外层事务
    仍然有效时运行，用来把这些“失败状态”落库。
    """
    _FAILURE_HOOKS[task_type] = hook


def unregister_failure_hook(task_type: str) -> None:
    _FAILURE_HOOKS.pop(task_type, None)


def registered_task_types() -> frozenset[str]:
    return frozenset(_HANDLERS)


def handler_for(task_type: str) -> TaskHandler | None:
    return _HANDLERS.get(task_type)


def task_view(task: ScheduledTask) -> dict:
    """scheduled_tasks 行到 API Task schema 的映射。"""
    payload = task.payload or {}
    return {
        "task_id": task.id,
        "user_id": task.user_id,
        "task_type": task.task_type,
        "execute_at": iso(task.execute_at),
        "status": task.status,
        "amount_minor": payload.get("amount_minor"),
        "result": task.result,
        "error_code": task.error_code,
    }


class ClockService:
    def __init__(self, database: Database):
        self.database = database

    def _state(self, session: Session) -> SimulationState:
        state = session.get(SimulationState, 1)
        if state is None:
            raise BusinessError("模拟时钟未初始化，请先执行 seed")
        return state

    def _view(self, session: Session, state: SimulationState) -> dict:
        return {
            "current_time": iso(state.current_time),
            "run_id": str(state.run_id),
            "due_task_count": len(task_store.list_due_tasks(session, state.current_time)),
        }

    def get(self) -> dict:
        with self.database.sessions() as session:
            return self._view(session, self._state(session))

    def current_time(self) -> datetime:
        with self.database.sessions() as session:
            return self._state(session).current_time

    def set_time(self, current_time: datetime) -> dict:
        """设置模拟时间。只允许向前（或原地不动），回拨直接拒绝。"""
        with self.database.sessions() as session:
            with session.begin():
                state = self._state(session)
                if current_time < state.current_time:
                    raise BusinessError(
                        "模拟时钟只能向前拨："
                        f"当前 {state.current_time.isoformat()}，请求 {current_time.isoformat()}"
                    )
                state.current_time = current_time
                session.flush()
                return self._view(session, state)

    def run_due_tasks(
        self,
        *,
        task_types: tuple[str, ...] | None = None,
        birthday_outcome: str = "success",
    ) -> dict:
        """执行所有 execute_at <= 模拟时间且仍为 scheduled 的任务。"""
        with self.database.sessions() as session:
            with session.begin():
                state = self._state(session)
                context = TaskContext(now=state.current_time, birthday_outcome=birthday_outcome)
                due = task_store.list_due_tasks(session, state.current_time, task_types=task_types)

                items = [self._execute(session, task, context) for task in due]
                completed = sum(1 for item in items if item["status"] == task_store.COMPLETED)
                return {
                    "processed_count": len(items),
                    "completed_count": completed,
                    "failed_count": len(items) - completed,
                    "items": items,
                }

    def _execute(
        self, session: Session, task: ScheduledTask, context: TaskContext
    ) -> dict:
        # 抢占先于保存点：无论成功失败，任务都不会停留在 scheduled。
        task_store.claim_task(session, task)
        handler = handler_for(task.task_type)

        if handler is None:
            return self._fail(session, task, "handler_not_registered", f"未注册 {task.task_type}")

        try:
            with session.begin_nested():
                result = handler(session, task, context) or {}
        except BankError as exc:
            # 保存点已回滚：资金变更撤销，但失败状态与审计保留。
            return self._fail(session, task, exc.message, exc.reason)

        task_store.finish_task(session, task, status=task_store.COMPLETED, result=result)
        return task_view(task)

    def _fail(
        self, session: Session, task: ScheduledTask, error_code: str, reason: str | None
    ) -> dict:
        task_store.finish_task(
            session,
            task,
            status=task_store.FAILED,
            result={"reason": reason},
            error_code=error_code,
        )
        audit_service.record(
            session,
            action=f"run_task:{task.task_type}",
            resource_type="scheduled_task",
            resource_id=task.id,
            user_id=task.user_id,
            result=audit_service.RESULT_FAILURE,
            reason=reason,
            new_value={"status": task_store.FAILED, "error_code": error_code},
        )
        # 保存点已回滚，处理器写下的失败状态需要在这里补记。
        hook = _FAILURE_HOOKS.get(task.task_type)
        if hook is not None:
            hook(session, task, reason)
        return task_view(task)
