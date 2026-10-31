"""审计、安全、系统接口与 OpenAPI 契约。

覆盖计划要求：audit、security/status、security/reset-lock、seed/reset、health 接口，
以及每个 mutation 都写入 request_id、旧值、新值、结果和原因。
"""

import pytest
from sqlalchemy import select

from app.domain.security_service import LOCK_THRESHOLD
from app.storage.models import AuditLog, SimulationState
from tests.conftest import mfa_code

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


def _fail_transfer(client, amount_minor=99999999):
    """制造一次必然失败的资金操作（先满足 red 级确认要求）。"""
    return client.post(
        f"{BASE}/transfers",
        json={
            "from_account_id": "a_zhang_current",
            "to_account_id": "a_partner_current",
            "amount_minor": amount_minor,
            "confirm": True,
            "mfa_code": "123456",
        },
    )


# ---------------------------------------------------------------- Health


def test_health_reports_database_and_run_id(seeded_client):
    """`/health` 是最小就绪：库、种子是否已跑，不看余额。"""
    data = _ok(seeded_client.get(f"{BASE}/health"))
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert data["seed_version"] == "1.2"
    assert data["seeded"] is True
    assert data["run_id"]


def test_deep_health_keeps_the_account_reconciliation(seeded_client):
    """账户对账还在，只是搬到了 `/health/deep`，不再拿来当探针。"""
    data = _ok(seeded_client.get(f"{BASE}/health/deep"))
    assert data["account_count"] == 5
    assert data["run_id"]


def test_health_stays_ready_when_the_simulation_state_is_missing(
    seeded_client, seeded_database
):
    """没有种子数据时服务仍然「就绪」：库能查、能建客户，只是还没人跑种子。

    这条是健康检查与固定种子解耦的核心断言——上一版这里返回 503，于是一次正常的清库
    就会把整个容器判成不健康。
    """
    with seeded_database.sessions() as session:
        with session.begin():
            state = session.get_one(SimulationState, 1)
            session.delete(state)

    ready = _ok(seeded_client.get(f"{BASE}/health"))
    assert ready["status"] == "ok"
    assert ready["seeded"] is False
    assert ready["run_id"] is None

    _error(seeded_client.get(f"{BASE}/health/deep"), 503, 1005)


# ---------------------------------------------------------------- Seed reset


def test_seed_reset_restores_the_fixed_seed_and_rotates_the_run_id(seeded_client):
    before = _ok(seeded_client.get(f"{BASE}/health"))["run_id"]

    _ok(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "confirm": True,
            },
        )
    )
    assert _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))[
        "balance_minor"
    ] == 1230050

    after = _ok(seeded_client.post(f"{BASE}/seed/reset"))
    assert after["run_id"] != before
    assert _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))[
        "balance_minor"
    ] == 1250050


def test_seed_reset_can_be_guarded_by_expected_run_id(seeded_client):
    current = _ok(seeded_client.get(f"{BASE}/health"))["run_id"]

    _error(
        seeded_client.post(f"{BASE}/seed/reset", json={"expected_run_id": "not-the-run-id"}),
        409,
        1005,
    )
    assert _ok(seeded_client.get(f"{BASE}/health"))["run_id"] == current

    rotated = _ok(seeded_client.post(f"{BASE}/seed/reset", json={"expected_run_id": current}))
    assert rotated["run_id"] != current


# ---------------------------------------------------------------- Audit


def test_every_mutation_writes_request_id_and_before_after_values(seeded_client):
    _ok(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "confirm": True,
            },
        )
    )
    logs = _ok(seeded_client.get(f"{BASE}/audit", params={"user_id": "u_zhang"}))
    entry = logs["items"][0]
    assert entry["action"] == "transfer"
    assert entry["request_id"].startswith("req_")
    assert entry["old_value"]["from_balance_minor"] == 1250050
    assert entry["new_value"]["from_balance_minor"] == 1230050
    assert entry["new_value"]["transaction_id"].startswith("txn_")
    assert entry["result"] == "success"
    assert entry["permission_level"] == "green"
    assert entry["confirm_required"] is False
    assert entry["mfa_required"] is False
    assert entry["created_at"]


def test_failure_audit_records_the_reason(seeded_client):
    _fail_transfer(seeded_client)
    logs = _ok(
        seeded_client.get(f"{BASE}/audit", params={"user_id": "u_zhang", "action": "transfer"})
    )
    failures = [item for item in logs["items"] if item["result"] == "failure"]
    assert failures
    assert "余额不足" in failures[0]["reason"]


def test_audit_can_be_filtered_by_action_and_resource_type(seeded_client, wealth_risk_profile):
    _ok(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_yuebao",
                "amount_minor": 100000,
                "confirm": True,
                "mfa_code": mfa_code(),
            },
        )
    )
    by_action = _ok(seeded_client.get(f"{BASE}/audit", params={"action": "purchase_product"}))
    assert by_action["total"] == 1
    by_resource = _ok(seeded_client.get(f"{BASE}/audit", params={"resource_type": "order"}))
    assert by_resource["total"] >= 1
    assert _ok(seeded_client.get(f"{BASE}/audit", params={"action": "nothing_like_this"}))[
        "total"
    ] == 0


def test_pending_confirmation_is_audited_but_does_not_count_as_a_security_failure(
    seeded_client,
):
    _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 100000,
            },
        ),
        409,
        1007,
    )
    status = _ok(seeded_client.get(f"{BASE}/security/status"))
    assert status["failure_count"] == 0

    logs = _ok(seeded_client.get(f"{BASE}/audit", params={"action": "transfer"}))
    assert logs["items"][0]["result"] == "failure"


def test_unknown_audit_id_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/audit/audit_missing"), 404, 1004)


# ---------------------------------------------------------------- Security


def test_seeded_security_status_is_clean(seeded_client):
    status = _ok(seeded_client.get(f"{BASE}/security/status"))
    assert status["user_id"] == "u_zhang"
    assert status["tool_name"] == "fund_transfer"
    assert status["failure_count"] == 0
    assert status["locked_until"] is None
    assert status["tools"]


def test_repeated_failures_lock_the_transfer_tool_and_reset_unlocks_it(seeded_client):
    for _ in range(LOCK_THRESHOLD):
        _fail_transfer(seeded_client)

    status = _ok(seeded_client.get(f"{BASE}/security/status"))
    assert status["failure_count"] == LOCK_THRESHOLD
    assert status["locked_until"] is not None

    # 锁定期内连正常的转账也被拒绝：锁有自己的线路码 1009，不再是通用的 1005。
    locked = _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "confirm": True,
            },
        ),
        409,
        1009,
    )
    # 银行侧的 message 是稳定的英文 token：中文文案只在对话侧（Review Focus #5）。
    assert locked["message"] == "locked"
    assert _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))[
        "balance_minor"
    ] == 1250050

    reset = _ok(seeded_client.post(f"{BASE}/security/reset-lock"))
    assert reset["failure_count"] == 0
    assert reset["locked_until"] is None

    _ok(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "confirm": True,
            },
        )
    )
    assert _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))[
        "balance_minor"
    ] == 1230050


def test_failure_for_an_unknown_user_is_audited_without_a_security_row(seeded_client, seeded_database):
    """security_state 对 users 有外键：未知用户的失败只留审计，不建安全行。"""
    from app.storage.models import SecurityState

    _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "user_id": "u_nobody",
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 100,
            },
        ),
        404,
        1004,
    )

    logs = _ok(seeded_client.get(f"{BASE}/audit", params={"user_id": "u_nobody"}))
    assert logs["total"] == 1
    assert logs["items"][0]["result"] == "failure"

    with seeded_database.sessions() as session:
        rows = session.execute(
            select(SecurityState).where(SecurityState.user_id == "u_nobody")
        ).scalars().all()
    assert rows == []


def test_reset_lock_writes_an_audit_entry(seeded_client):
    _ok(seeded_client.post(f"{BASE}/security/reset-lock"))
    logs = _ok(seeded_client.get(f"{BASE}/audit", params={"action": "reset_security_lock"}))
    assert logs["total"] == 1
    assert logs["items"][0]["old_value"]["failure_count"] == 0


# ---------------------------------------------------------------- OpenAPI 契约


def test_openapi_document_is_served():
    from app.main import create_app

    schema = create_app().openapi()
    assert schema["info"]["title"] == "模拟银行系统 API"
    assert schema["info"]["version"] == "1.3.0"
    assert schema["paths"]


def test_fastapi_routes_cover_every_documented_operation_id():
    """把 API接口文档.md 的 operationId 与 FastAPI 实际注册的操作逐一比对。"""
    import io
    from pathlib import Path

    import yaml

    from app.main import create_app

    document = Path(__file__).resolve().parents[2] / "API接口文档.md"
    spec = yaml.safe_load(io.open(document, encoding="utf-8"))

    documented = {}
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if isinstance(operation, dict) and "operationId" in operation:
                documented[operation["operationId"]] = (method.upper(), path)

    prefixes = {server["url"] for server in spec.get("servers", [])}
    assert prefixes, "API 文档必须声明 servers"

    schema = create_app().openapi()
    implemented = {}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            operation_id = operation.get("operationId")
            if operation_id:
                implemented[operation_id] = (method.upper(), path.replace("/api", "", 1))

    missing = sorted(set(documented) - set(implemented))
    assert missing == [], f"文档里有但未实现的 operationId: {missing}"

    mismatched = {
        operation_id: (documented[operation_id], implemented[operation_id])
        for operation_id in documented
        if implemented.get(operation_id) != documented[operation_id]
    }
    assert mismatched == {}, f"方法与路径不一致: {mismatched}"

    undocumented = sorted(set(implemented) - set(documented))
    assert undocumented == [], f"实现了但文档里没有的 operationId: {undocumented}"


def test_every_documented_operation_declares_success_and_error_responses():
    import io
    from pathlib import Path

    import yaml

    document = Path(__file__).resolve().parents[2] / "API接口文档.md"
    spec = yaml.safe_load(io.open(document, encoding="utf-8"))

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            codes = set(operation.get("responses", {}))
            if operation.get("deprecated") is True:
                # 已停用的入口按设计只返回错误（如 /cards/apply 固定 422），
                # 不应为了通过文档检查虚构一个永远不会出现的 2xx。
                assert any(
                    code.startswith(("4", "5")) for code in codes
                ), f"{method} {path} 缺错误响应"
                continue
            assert any(code.startswith("2") for code in codes), f"{method} {path} 缺成功响应"
            assert any(
                code.startswith(("4", "5")) for code in codes
            ), f"{method} {path} 缺错误响应"


def test_served_openapi_declares_success_and_error_responses_for_every_operation():
    """服务实际提供的 OpenAPI 也必须为每个操作声明成功响应与错误响应。"""
    from app.main import create_app

    schema = create_app().openapi()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            codes = set(operation.get("responses", {}))
            assert any(code.startswith("2") for code in codes), f"{method} {path} 缺成功响应"
            assert any(
                code.startswith(("4", "5")) for code in codes
            ), f"{method} {path} 缺错误响应"


def test_server_side_paths_do_not_shadow_each_other(seeded_client):
    """字面量路径必须优先于路径参数，否则 /accounts/balance 会被当成 account_id。"""
    assert isinstance(_ok(seeded_client.get(f"{BASE}/accounts/balance")), list)
    assert isinstance(_ok(seeded_client.get(f"{BASE}/accounts/transactions"))["items"], list)
    assert isinstance(
        _ok(seeded_client.post(f"{BASE}/products/recommend", json={"risk_level": "low"})), list
    )
    assert isinstance(
        _ok(
            seeded_client.post(
                f"{BASE}/products/compare", json={"product_ids": ["p_yuebao", "p_money_fund"]}
            )
        ),
        list,
    )
    assert isinstance(_ok(seeded_client.post(f"{BASE}/subscriptions/run-due")), dict)
    assert _ok(seeded_client.get(f"{BASE}/mock-products"))[0]["product_code"] == "flowers"


def test_schema_tables_are_all_reachable(seeded_database):
    """19 张表在迁移后都存在，包括理财测评和卡片授权记录。"""
    from sqlalchemy import inspect

    tables = set(inspect(seeded_database.engine).get_table_names())
    assert len(tables - {"alembic_version"}) == 19
    assert "card_actions" in tables
    assert "wealth_risk_assessments" in tables


def test_audit_rows_are_persisted_with_old_and_new_values(seeded_client, seeded_database):
    _ok(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "confirm": True,
            },
        )
    )
    with seeded_database.sessions() as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == "transfer")
        ).scalars().one()
    assert entry.old_value is not None
    assert entry.new_value is not None
    assert entry.request_id


@pytest.mark.parametrize(
    "path",
    ["/health", "/clock", "/accounts", "/audit", "/security/status"],
)
def test_read_endpoints_are_reachable(seeded_client, path):
    assert _ok(seeded_client.get(f"{BASE}{path}")) is not None
