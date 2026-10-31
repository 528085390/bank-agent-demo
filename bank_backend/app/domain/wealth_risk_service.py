"""风险测评：银行侧唯一权威的问卷评分、落库与有效期判断。

规则版本 `wealth-risk-v1`：五题编号 q1..q5，每题选项 0／1／2。

| 题目 | 0 分 | 1 分 | 2 分 |
| --- | --- | --- | --- |
| q1 本金损失接受度 | 不能接受本金损失 | 接受少量波动和损失 | 接受较大波动和损失 |
| q2 投资经验 | 无经验 | 有低风险产品经验 | 有较多波动型产品经验 |
| q3 收入稳定性 | 不稳定、备用金少 | 较稳定、备用金一般 | 稳定、有足够备用金 |
| q4 计划投资期限 | 3 个月以内 | 超过 3 个月且不超过 12 个月 | 超过 12 个月 |
| q5 短期资金需求 | 随时可能急用 | 部分资金可能短期使用 | 这部分资金短期不使用 |

总分 0–3 为 low、4–7 为 medium、8–10 为 high；q1 为 0 时等级上限为 low。
有效期 30 天，以银行模拟时钟为准；过期记录仍保留并返回，只是 `valid=false`，
不用删除记录表示过期。等级由服务端计算，客户端提交的任何等级字段都会被拒绝。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.core.ids import new_id
from app.core.timeutil import iso
from app.domain import audit_service, clock_service
from app.domain.account_service import require_user
from app.storage.database import Database
from app.storage.models import WealthRiskAssessment

RULE_VERSION = "wealth-risk-v1"
VALID_DAYS = 30
ASSESSMENT = "wealth_assessment"
ACTION_SUBMIT = "submit_risk_assessment"
RESOURCE_TYPE = "wealth_risk_assessment"

QUESTION_IDS = ("q1", "q2", "q3", "q4", "q5")
OPTIONS = (0, 1, 2)

# 等级高低，用于申购时判断产品风险是否高于测评等级。
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def validate_answers(answers: object) -> dict[str, int]:
    """校验五题齐全且选项只能是 0／1／2（bool 不算数值）。"""

    if not isinstance(answers, dict):
        raise ValidationError("answers 必须是 q1..q5 到 0/1/2 的对象")
    unknown = sorted(set(answers) - set(QUESTION_IDS))
    if unknown:
        raise ValidationError(f"answers 含未知题目: {', '.join(unknown)}")
    missing = [question for question in QUESTION_IDS if question not in answers]
    if missing:
        raise ValidationError(f"answers 缺少题目: {', '.join(missing)}")

    cleaned: dict[str, int] = {}
    for question in QUESTION_IDS:
        value = answers[question]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{question} 的选项必须是 0/1/2 整数")
        if value not in OPTIONS:
            raise ValidationError(f"{question} 的选项必须是 0/1/2 之一")
        cleaned[question] = value
    return cleaned


def score_answers(answers: dict[str, int]) -> tuple[int, str]:
    """按 wealth-risk-v1 计算总分与等级；q1 为 0 时等级上限为 low。"""

    return _score(validate_answers(answers))


def _score(cleaned: dict[str, int]) -> tuple[int, str]:
    """对已校验的答案评分，避免调用方重复校验。"""

    total = sum(cleaned[question] for question in QUESTION_IDS)
    if total <= 3:
        level = "low"
    elif total <= 7:
        level = "medium"
    else:
        level = "high"
    if cleaned["q1"] == 0:
        level = "low"
    return total, level


def is_valid(assessment: WealthRiskAssessment, moment: datetime) -> bool:
    """有效性以银行模拟时钟为准：到达 expires_at 即为过期。"""

    return assessment.expires_at > moment


def assessment_view(assessment: WealthRiskAssessment, *, now: datetime) -> dict:
    return {
        "assessment_id": assessment.id,
        "user_id": assessment.user_id,
        "answers": dict(assessment.answers or {}),
        "score": assessment.score,
        "risk_level": assessment.risk_level,
        "rule_version": assessment.rule_version,
        "assessed_at": iso(assessment.assessed_at),
        "expires_at": iso(assessment.expires_at),
        "valid": is_valid(assessment, now),
    }


def latest_row(session: Session, user_id: str) -> WealthRiskAssessment | None:
    """本人最近一条测评（含已过期），供服务与申购复核共用。"""

    return session.execute(
        select(WealthRiskAssessment)
        .where(WealthRiskAssessment.user_id == user_id)
        .order_by(WealthRiskAssessment.assessed_at.desc(), WealthRiskAssessment.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def valid_row(
    session: Session, user_id: str, moment: datetime
) -> WealthRiskAssessment | None:
    """本人最近一条仍然有效的测评；没有或已过期返回 None。"""

    assessment = latest_row(session, user_id)
    if assessment is None or not is_valid(assessment, moment):
        return None
    return assessment


class WealthRiskService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    def submit(
        self, *, user_id: str, answers: dict[str, int], request_id: str | None = None
    ) -> dict:
        """保存一次测评：服务端评分后追加记录，并写审计。"""

        with self.database.sessions() as session:
            with session.begin():
                require_user(session, user_id)
                cleaned = validate_answers(answers)
                score, level = _score(cleaned)
                now = clock_service.ClockService(self.database).current_time()
                assessed_at = self._monotonic_assessed_at(session, user_id, now)

                assessment = WealthRiskAssessment(
                    id=new_id(ASSESSMENT),
                    user_id=user_id,
                    answers=cleaned,
                    score=score,
                    risk_level=level,
                    rule_version=RULE_VERSION,
                    assessed_at=assessed_at,
                    expires_at=assessed_at + timedelta(days=VALID_DAYS),
                )
                session.add(assessment)
                session.flush()

                view = assessment_view(assessment, now=now)
                audit_service.record(
                    session,
                    action=ACTION_SUBMIT,
                    resource_type=RESOURCE_TYPE,
                    resource_id=assessment.id,
                    user_id=user_id,
                    new_value=view,
                    reason=f"风险测评得分 {score}，等级 {level}",
                    request_id=request_id,
                )
                return view

    def latest(self, *, user_id: str) -> dict | None:
        """本人最近一次测评；没有记录返回 None（接口 data=null）。"""

        with self.database.sessions() as session:
            require_user(session, user_id)
            now = clock_service.ClockService(self.database).current_time()
            assessment = latest_row(session, user_id)
            if assessment is None:
                return None
            return assessment_view(assessment, now=now)

    def latest_valid(self, *, user_id: str) -> dict | None:
        """本人最近一次仍然有效的测评，供推荐与预检使用。"""

        latest = self.latest(user_id=user_id)
        if latest is None or not latest["valid"]:
            return None
        return latest

    @staticmethod
    def _monotonic_assessed_at(session: Session, user_id: str, now: datetime) -> datetime:
        """同一模拟时刻重复提交时仍保证先后可区分。

        模拟时钟精度有限，两次问卷可能落在同一时刻；给后一条加 1 微秒，
        让“最近一条测评”始终是最后一次提交，而不依赖随机 ID 排序。
        """

        previous = latest_row(session, user_id)
        if previous is not None and previous.assessed_at >= now:
            return previous.assessed_at + timedelta(microseconds=1)
        return now
