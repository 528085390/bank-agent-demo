"""风险测评接口：保存、查询、评分边界、客户隔离与银行时钟过期。

覆盖计划任务 1 的断言：HTTP 到落库一致、评分边界、按客户隔离、
按银行模拟时钟过期，以及客户端不能指定最终等级或提交 bool 冒充数值。
"""

from sqlalchemy import select

from app.storage.models import WealthRiskAssessment
from tests.conftest import HIGH_RISK_ANSWERS, LOW_RISK_ANSWERS

BASE = "/api"


def _ok(response) -> dict:
    assert response.status_code < 400, response.text
    body = response.json()
    assert body["code"] == 0
    return body["data"]


def _error(response, status: int, code: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["data"] is None
    return body


def _submit(client, answers: dict, **overrides):
    payload = {"answers": answers, **overrides}
    return client.post(f"{BASE}/wealth/risk-assessments", json=payload)


def test_submit_persists_assessment(seeded_client, seeded_database):
    data = _ok(_submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_zhang"))
    assert data["score"] == 10
    assert data["risk_level"] == "high"
    assert data["rule_version"] == "wealth-risk-v1"
    assert data["valid"] is True
    assert data["assessment_id"].startswith("wealth_assessment_")

    # 直接查真实表：HTTP 返回与落库记录一致。
    with seeded_database.sessions() as session:
        row = session.get_one(WealthRiskAssessment, data["assessment_id"])
        assert row.user_id == "u_zhang"
        assert row.answers == HIGH_RISK_ANSWERS
        assert row.score == 10
        assert row.risk_level == "high"

    # 新会话重新读取（不靠对象缓存）。
    latest = _ok(seeded_client.get(f"{BASE}/wealth/risk-assessments/latest"))
    assert latest["assessment_id"] == data["assessment_id"]
    assert latest["answers"] == HIGH_RISK_ANSWERS
    assert latest["valid"] is True


def test_risk_score_boundaries(seeded_client):
    # 总分 3 -> low；4 -> medium；7 -> medium；8 -> high（q1 非 0）。
    for answers, expected_score, expected_level in (
        ({"q1": 1, "q2": 1, "q3": 1, "q4": 0, "q5": 0}, 3, "low"),
        ({"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 0}, 4, "medium"),
        ({"q1": 1, "q2": 2, "q3": 2, "q4": 1, "q5": 1}, 7, "medium"),
        ({"q1": 2, "q2": 2, "q3": 2, "q4": 1, "q5": 1}, 8, "high"),
    ):
        data = _ok(_submit(seeded_client, answers, user_id="u_zhang"))
        assert data["score"] == expected_score
        assert data["risk_level"] == expected_level

    # 首题为 0 时等级上限为 low：总分 8 仍然是 low。
    capped = _ok(
        _submit(seeded_client, {"q1": 0, "q2": 2, "q3": 2, "q4": 2, "q5": 2}, user_id="u_zhang")
    )
    assert capped["score"] == 8
    assert capped["risk_level"] == "low"

    # 最近一次提交仍然是最新记录（同一模拟时刻也要能区分先后）。
    latest = _ok(seeded_client.get(f"{BASE}/wealth/risk-assessments/latest"))
    assert latest["assessment_id"] == capped["assessment_id"]


def test_latest_is_customer_scoped(seeded_client):
    zhang = _ok(_submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_zhang"))
    li = _ok(_submit(seeded_client, LOW_RISK_ANSWERS, user_id="u_li"))

    assert _ok(
        seeded_client.get(f"{BASE}/wealth/risk-assessments/latest", params={"user_id": "u_zhang"})
    )["assessment_id"] == zhang["assessment_id"]
    assert _ok(
        seeded_client.get(f"{BASE}/wealth/risk-assessments/latest", params={"user_id": "u_li"})
    )["assessment_id"] == li["assessment_id"]
    # 没有测评的客户拿不到别人的记录。
    assert _ok(
        seeded_client.get(f"{BASE}/wealth/risk-assessments/latest", params={"user_id": "u_partner"})
    ) is None


def test_latest_without_assessment_returns_null(seeded_client):
    assert _ok(seeded_client.get(f"{BASE}/wealth/risk-assessments/latest")) is None


def test_profile_expires_by_bank_clock(seeded_client, seeded_database):
    created = _ok(_submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_zhang"))
    assert created["valid"] is True

    # 把银行模拟时钟拨到 expires_at 之后：过期记录仍返回，但 valid=false。
    _ok(seeded_client.post(f"{BASE}/clock", json={"current_time": "2026-11-01T10:00:00+08:00"}))
    latest = _ok(seeded_client.get(f"{BASE}/wealth/risk-assessments/latest"))
    assert latest["assessment_id"] == created["assessment_id"]
    assert latest["valid"] is False
    assert latest["expires_at"] < "2026-11-01T10:00:00+08:00"

    with seeded_database.sessions() as session:
        rows = session.execute(select(WealthRiskAssessment)).scalars().all()
    assert len(rows) == 1, "过期不用删除记录表示"


def test_client_cannot_specify_level_or_smuggle_answers(seeded_client):
    # 夹带 risk_level / score / 自造题号都会被拒绝（1012）。
    _error(
        _submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_zhang", risk_level="high"),
        422,
        1012,
    )
    _error(
        _submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_zhang", score=10),
        422,
        1012,
    )
    _error(
        _submit(seeded_client, {**HIGH_RISK_ANSWERS, "q6": 2}, user_id="u_zhang"),
        422,
        1012,
    )
    # 五题不齐、选项越界、bool 冒充数值都是参数错误。
    _error(_submit(seeded_client, {"q1": 1, "q2": 1, "q3": 1, "q4": 1}, user_id="u_zhang"), 422, 1012)
    _error(
        _submit(seeded_client, {**HIGH_RISK_ANSWERS, "q5": 3}, user_id="u_zhang"), 422, 1012
    )
    _error(
        _submit(seeded_client, {**HIGH_RISK_ANSWERS, "q5": True}, user_id="u_zhang"), 422, 1012
    )
    _error(
        _submit(seeded_client, {**HIGH_RISK_ANSWERS, "q5": "2"}, user_id="u_zhang"), 422, 1012
    )


def test_unknown_customer_is_rejected(seeded_client):
    _error(
        _submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_missing"),
        404,
        1004,
    )


def test_assessment_submission_is_audited(seeded_client):
    data = _ok(_submit(seeded_client, HIGH_RISK_ANSWERS, user_id="u_zhang"))
    logs = _ok(
        seeded_client.get(
            f"{BASE}/audit",
            params={"user_id": "u_zhang", "action": "submit_risk_assessment"},
        )
    )
    assert logs["total"] == 1
    assert logs["items"][0]["resource_id"] == data["assessment_id"]
    assert logs["items"][0]["result"] == "success"
