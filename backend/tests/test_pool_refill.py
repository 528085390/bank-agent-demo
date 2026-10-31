"""客户池自愈补位：P2 里唯一会「跨两个库」的路径。

设计取舍见 `app/identity/pool.py` 的模块注释。这里验证的是它承诺的四件事：

1. 注册领走一格之后，会去补一格（否则池子只减不增）。
2. 注册撞上空池时，直接给这位用户建一个并绑定（否则那批用户永久没有银行客户）。
3. 补位任何一步失败都不影响注册——它是水位问题，不是用户的问题。
4. 登录时对未绑定的用户再试一次（上一次注册时银行不可用的补救）。

跨库部分用假银行客户端（`httpx.MockTransport`）覆盖，断言落在真实数据库上：
池记录有没有写、`app_user.bank_customer_id` 有没有变。
"""

from __future__ import annotations

import httpx
from sqlalchemy import select, text

from app.identity.pool import PoolRefiller
from app.integration.bank_client import BankClient
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.storage.models import AppUser, BankCustomerPool

PASSWORD = "correct horse battery"


def app_client(rt):
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(rt.settings, runtime=rt))


def register(client, login_name: str):
    return client.post(
        "/api/auth/register", json={"login_name": login_name, "password": PASSWORD}
    )


def fake_bank(data: dict, *, status: int = 201) -> BankClient:
    """一个只会回固定信封的银行客户端；请求头也一并断言。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Service-Token"] == "test-service-token"
        return httpx.Response(status, json={"code": 0, "message": "ok", "data": data})

    return BankClient(
        "http://bank.test/api",
        service_token="test-service-token",
        transport=httpx.MockTransport(handler),
    )


def empty_the_pool(rt) -> None:
    with rt.database.sessions.begin() as db:
        db.execute(text("DELETE FROM bank_customer_pool"))


# ---------------------------------------------------------------- 注册路径


def test_register_refills_the_pool_after_claiming(rt):
    client = app_client(rt)
    response = register(client, "alice")

    assert response.status_code == 201, response.text
    assert rt.identity.refiller.refills == 1
    # 池子有货时不该走「直接建号」那条路。
    assert rt.identity.refiller.provisioned == []


def test_register_provisions_directly_when_the_pool_is_empty(rt):
    """池空不再等于「这个用户永远没有银行客户」。"""

    empty_the_pool(rt)
    client = app_client(rt)
    response = register(client, "bob")

    assert response.status_code == 201, response.text
    user_id = response.json()["data"]["user_id"]
    assert rt.identity.refiller.provisioned == [user_id]
    assert rt.identity.refiller.refills == 0


def test_a_refiller_that_explodes_does_not_fail_the_registration(rt):
    rt.identity.refiller.raises = True
    client = app_client(rt)

    response = register(client, "carol")

    assert response.status_code == 201, response.text
    with rt.database.sessions() as db:
        user = db.scalar(select(AppUser).where(AppUser.user_id == response.json()["data"]["user_id"]))
        assert user is not None


def test_the_pool_is_refilled_only_after_the_account_committed(rt):
    """补位失败时账号必须已经落了库：顺序反了就会出现「池子补上了、用户没建成」。"""

    rt.identity.refiller.raises = True
    client = app_client(rt)
    response = register(client, "dave")

    with rt.database.sessions() as db:
        assert db.scalar(
            select(AppUser).where(AppUser.user_id == response.json()["data"]["user_id"])
        ) is not None


# ---------------------------------------------------------------- 登录路径


def test_login_reclaims_a_binding_that_registration_could_not_make(rt):
    empty_the_pool(rt)
    client = app_client(rt)
    user_id = register(client, "erin").json()["data"]["user_id"]

    with rt.database.sessions.begin() as db:
        db.execute(
            text("UPDATE app_user SET bank_customer_id = NULL WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
    rt.identity.refiller.provisioned.clear()

    response = client.post(
        "/api/auth/login", json={"login_name": "erin", "password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    assert rt.identity.refiller.provisioned == [user_id]


def test_login_still_works_for_a_bound_user(rt):
    client = app_client(rt)
    register(client, "frank")
    rt.identity.refiller.provisioned.clear()

    response = client.post(
        "/api/auth/login", json={"login_name": "frank", "password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    assert rt.identity.refiller.provisioned == []


# ---------------------------------------------------------------- 补位器本身


def test_refill_records_the_new_customer_as_unclaimed(rt):
    refiller = PoolRefiller(rt.database, fake_bank({"customer_id": "u_pool_99"}))

    assert refiller.refill() == "u_pool_99"

    with rt.database.sessions() as db:
        row = db.scalar(select(BankCustomerPool).where(BankCustomerPool.customer_id == "u_pool_99"))
        assert row is not None
        assert row.claimed_by_user_id is None, "补回来的格子必须能被下一个注册领走"


def test_provision_for_binds_the_customer_to_the_user(rt):
    # 用 ORM 插入：`app_user` 的 `id` 是模型层生成的代理键，手写 SQL 会撞上 NOT NULL。
    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id="user_x", status="active", role="user"))
    refiller = PoolRefiller(rt.database, fake_bank({"customer_id": "u_pool_97"}))

    assert refiller.provision_for("user_x") == "u_pool_97"

    with rt.database.sessions() as db:
        user = db.scalar(select(AppUser).where(AppUser.user_id == "user_x"))
        assert user.bank_customer_id == "u_pool_97"
        row = db.scalar(select(BankCustomerPool).where(BankCustomerPool.customer_id == "u_pool_97"))
        assert row.claimed_by_user_id == "user_x"


def test_refill_is_silent_when_the_bank_refuses(rt):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 1001, "message": "unauthorized", "data": None})

    refiller = PoolRefiller(
        rt.database, BankClient("http://bank.test/api", service_token="wrong",
                                transport=httpx.MockTransport(handler))
    )

    # 不抛：补位失败只该是一条日志。
    assert refiller.refill() is None


def test_refill_ignores_a_null_customer_id(rt):
    """银行侧并发抢号用尽时返回 null；调用方应当当作「这次没补上」。"""

    refiller = PoolRefiller(rt.database, fake_bank({"customer_id": None}))

    assert refiller.refill() is None


def test_a_duplicate_pool_row_is_absorbed(rt):
    """两个并发补位算出同一个号时，app 侧的写入必须是幂等的。"""

    refiller = PoolRefiller(rt.database, fake_bank({"customer_id": "u_pool_96"}))
    assert refiller.refill() == "u_pool_96"
    assert refiller.refill() == "u_pool_96"

    with rt.database.sessions() as db:
        rows = db.scalars(
            select(BankCustomerPool).where(BankCustomerPool.customer_id == "u_pool_96")
        ).all()
        assert len(rows) == 1


def test_provision_for_a_vanished_user_leaves_the_grid_unclaimed(rt):
    """记账时用户已不在：这一格必须退回池尾，而不是被一个不存在的用户永久占住。"""

    refiller = PoolRefiller(rt.database, fake_bank({"customer_id": "u_pool_95"}))

    # 返回 None 而不是号：注册接口据此决定响应里的 bank_customer_id，不能宣称一个假绑定。
    assert refiller.provision_for("user_missing") is None

    with rt.database.sessions() as db:
        row = db.scalar(select(BankCustomerPool).where(BankCustomerPool.customer_id == "u_pool_95"))
        assert row is not None
        assert row.claimed_by_user_id is None


def test_a_bank_failure_is_not_reported_as_a_user_error(rt):
    """补位器只吞掉自己能处理的失败，不把 BaseException 一起吞了。"""

    class Exploding:
        def service_request(self, *args, **kwargs):
            raise BankCallError(BankErrorKind.CONNECT, "boom", retryable=True)

    refiller = PoolRefiller(rt.database, Exploding())

    assert refiller.refill() is None
