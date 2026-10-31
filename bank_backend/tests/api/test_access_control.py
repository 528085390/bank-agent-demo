"""访问控制：服务凭证、运维口令、身份必填、资源归属。

这一组用例对应验证「8001 不再是裸奔的内部接口」这件事的**可观测后果**，而不是实现细节：

* 没有服务凭证 → 401（1001）。任何能连到 8001 的进程都不该读到客户数据。
* 有服务凭证但没有运维口令 → 清库/推时钟 403（1002），且库里数据确实没被删。
* 缺 `user_id` → 422（1013），不再回落到某个默认客户。
* 以 u_zhang 的身份去读 u_partner 的账户/卡 → 404（1004），与「不存在」不可区分，
  所以状态码本身也探测不出别人的资源是否存在。
* 审计只能看自己的。
* 补位接口幂等：连调两次拿到两个不同的池号。
* `/api/health` 只做最小就绪，余额被写歪不会让它 503；对账下沉到 `/api/health/deep`。
"""

from __future__ import annotations

from app.domain import audit_service
from app.storage.models import Account, Card, User
from tests.conftest import OPS_TOKEN, SERVICE_TOKEN

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


# ---------------------------------------------------------------- 服务凭证


def test_health_needs_no_credentials(anonymous_client, seeded_database):
    # 容器探针不持有任何钥匙；健康检查也不含客户数据，所以它是唯一的例外。
    data = _ok(anonymous_client.get(f"{BASE}/health"))
    assert data["status"] == "ok"


def test_business_endpoint_without_service_token_is_refused(anonymous_client, seeded_database):
    _error(anonymous_client.get(f"{BASE}/accounts", params={"user_id": "u_zhang"}), 401, 1001)


def test_wrong_service_token_is_refused(anonymous_client, seeded_database):
    response = anonymous_client.get(
        f"{BASE}/accounts",
        params={"user_id": "u_zhang"},
        headers={"X-Service-Token": SERVICE_TOKEN + "x"},
    )
    _error(response, 401, 1001)


def test_service_token_alone_cannot_reset_the_database(anonymous_client, seeded_client):
    """app 进程只持有服务凭证；这一条确认它清不了库。"""

    response = anonymous_client.post(
        f"{BASE}/seed/reset", headers={"X-Service-Token": SERVICE_TOKEN}
    )
    _error(response, 403, 1002)

    # 数据还在：不是「响应说失败了但其实删了一半」。
    accounts = _ok(seeded_client.get(f"{BASE}/accounts", params={"user_id": "u_zhang"}))
    assert accounts


def test_service_token_alone_can_still_move_the_clock(anonymous_client, seeded_database):
    """`/clock` 只受服务凭证保护：app 的演示控制面就是要代理它。

    这一条同时在钉住边界的两侧：清库需要第二把钥匙（上一条），推时钟不需要（这一条）。
    如果哪天有人把 `/clock` 也划进运维口令范围，前端的「推进一天」会立刻失效。
    """

    response = anonymous_client.post(
        f"{BASE}/clock",
        json={"current_time": "2026-12-01T00:00:00+08:00"},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    _ok(response)


def test_the_clock_still_needs_a_service_token(anonymous_client, seeded_database):
    _error(
        anonymous_client.post(
            f"{BASE}/clock", json={"current_time": "2026-12-02T00:00:00+08:00"}
        ),
        401,
        1001,
    )


def test_ops_endpoint_works_with_both_keys(anonymous_client, seeded_database):
    # 两把钥匙齐了才放行：这里验证「加了保护」没有把运维本身锁死。
    response = anonymous_client.post(
        f"{BASE}/seed/reset",
        headers={"X-Service-Token": SERVICE_TOKEN, "X-Ops-Token": OPS_TOKEN},
    )
    _ok(response)


# ---------------------------------------------------------------- 身份必填


def test_missing_user_id_is_rejected_instead_of_defaulting(seeded_client):
    # 空串等同于没给：上一版这里会静默变成 u_zhang，等于任何人都能读演示客户。
    response = seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance", params={"user_id": ""})
    _error(response, 422, 1013)


def test_unknown_user_id_is_rejected(seeded_client):
    _error(
        seeded_client.get(f"{BASE}/accounts", params={"user_id": "u_nobody"}),
        404,
        1004,
    )


def test_audit_list_requires_user_id(seeded_client):
    _error(seeded_client.get(f"{BASE}/audit", params={"user_id": ""}), 422, 1013)


# ---------------------------------------------------------------- 资源归属


def test_other_users_balance_is_not_readable(seeded_client):
    _error(
        seeded_client.get(
            f"{BASE}/accounts/a_partner_current/balance", params={"user_id": "u_zhang"}
        ),
        404,
        1004,
    )
    # 对照：本人账户读得到，说明上面失败的是归属而不是路径写错。
    data = _ok(
        seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance", params={"user_id": "u_zhang"})
    )
    assert data["balance_minor"] == 1250050


def test_other_users_card_cannot_be_frozen(seeded_client, seeded_database):
    _error(
        seeded_client.post(f"{BASE}/cards/card_partner_debit/freeze", params={"user_id": "u_zhang"}),
        404,
        1004,
    )
    with seeded_database.sessions() as session:
        card = session.get(Card, "card_partner_debit")
        # 404 必须真的没生效：状态码说谎比越权本身更难查。
        assert card.status == "active"


def test_other_users_card_details_are_hidden(seeded_client):
    _error(
        seeded_client.get(f"{BASE}/cards/card_partner_debit", params={"user_id": "u_zhang"}),
        404,
        1004,
    )


def test_other_users_transactions_are_hidden(seeded_client):
    _error(
        seeded_client.get(
            f"{BASE}/accounts/a_partner_current/transactions", params={"user_id": "u_zhang"}
        ),
        404,
        1004,
    )


def test_audit_detail_of_another_user_is_hidden(seeded_client, seeded_database):
    with seeded_database.sessions() as session:
        with session.begin():
            audit_id = audit_service.record(
                session,
                action="read_balance",
                resource_type="account",
                resource_id="a_partner_current",
                user_id="u_partner",
                reason="构造一条属于别人的审计",
            ).id

    _error(seeded_client.get(f"{BASE}/audit/{audit_id}", params={"user_id": "u_zhang"}), 404, 1004)


def test_audit_list_only_returns_own_entries(seeded_client, seeded_database):
    with seeded_database.sessions() as session:
        with session.begin():
            for owner in ("u_zhang", "u_partner"):
                audit_service.record(
                    session,
                    action="read_balance",
                    resource_type="account",
                    resource_id="a_partner_current",
                    user_id=owner,
                    reason="构造两条分属不同人的审计",
                )

    data = _ok(seeded_client.get(f"{BASE}/audit", params={"user_id": "u_zhang"}))
    assert data["items"]
    assert {item["user_id"] for item in data["items"]} == {"u_zhang"}


# ---------------------------------------------------------------- 客户池补位


def test_pool_provision_creates_a_new_customer_each_call(seeded_client, seeded_database):
    first = _ok(seeded_client.post(f"{BASE}/pool/provision"))
    second = _ok(seeded_client.post(f"{BASE}/pool/provision"))

    assert first["customer_id"] == "u_pool_01"
    assert second["customer_id"] == "u_pool_02"
    with seeded_database.sessions() as session:
        assert session.get(User, "u_pool_01") is not None
        assert session.get(User, "u_pool_02") is not None


def test_pool_provision_seeds_a_birthday_for_the_life_event_demo(seeded_database):
    """生日域只列「有生日」的联系人，池客户必须自带一个可演示的生日。

    固定时钟是为了钉住「生日永远在 9 天后」这条规则，而不是钉某一天。
    """

    from datetime import date, datetime

    from app.domain.pool_service import provision_customer
    from app.storage.models import Contact

    with seeded_database.sessions() as session:
        with session.begin():
            provision_customer(
                session,
                customer_id="u_pool_99",
                index=99,
                now=datetime(2026, 10, 30, 9, 0),
            )
        contact = session.get(Contact, "c_u_pool_99_family")

    assert contact is not None
    assert contact.birthday == date(1996, 11, 8)


def test_pool_provision_skips_ids_that_already_exist(seeded_client, seeded_database):
    _ok(seeded_client.post(f"{BASE}/pool/provision"))
    # 手工把 02 也建出来（模拟「银行里存在但 app 不知道」的残留），下一次补位应落到 03。
    with seeded_database.sessions() as session:
        with session.begin():
            from app.domain.pool_service import provision_customer

            provision_customer(
                session,
                customer_id="u_pool_02",
                index=2,
                now=session.get(User, "u_pool_01").created_at,
            )

    data = _ok(seeded_client.post(f"{BASE}/pool/provision"))
    assert data["customer_id"] == "u_pool_03"


# ---------------------------------------------------------------- 健康检查


def test_minimal_health_survives_balance_drift(seeded_client, seeded_database):
    """最小就绪不等于对账：余额写歪了，容器不该被判定为不可用。"""

    with seeded_database.sessions() as session:
        with session.begin():
            session.get(Account, "a_zhang_current").available_minor += 1

    assert _ok(seeded_client.get(f"{BASE}/health"))["status"] == "ok"
    # 对账信息没有消失，只是搬到了一个不会被探针调用的端点；它照旧用 503 表示不就绪，
    # 因为健康检查的语义是「能不能服务」，而不是某一次业务调用的 409。
    _error(seeded_client.get(f"{BASE}/health/deep"), 503, 1005)
