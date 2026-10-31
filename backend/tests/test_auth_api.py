"""P1 认证面的验收测试。

对应 `docs/superpowers/specs/2026-10-27-auth-and-user-memory-isolation-design.md` 的
A01–A04、A18、A20，以及新增的「管理员边界」。每个用例只断言一件事，失败时能直接指出
是哪条规则破了。

这里不用 `session` 夹具：那个夹具走的是 `create_demo_session()`，而本轮要证明的恰恰是
「没有凭据就拿不到身份」。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, select

import app.identity.service as identity_service
from app.main import create_app
from app.storage.models import AppUser, BankCustomerPool

PASSWORD = "correct horse battery"


def client_for(rt) -> TestClient:
    return TestClient(create_app(rt.settings, runtime=rt))


def register(client, login_name: str, password: str = PASSWORD) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"login_name": login_name, "password": password},
    )
    return response


def login(client, login_name: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"login_name": login_name, "password": password})


# --- 注册 -----------------------------------------------------------------------------


def test_register_creates_an_account_and_claims_one_pool_customer(rt):
    """A01：注册建号，并且领到一个独立的银行客户，而不是共用演示客户。"""

    client = client_for(rt)
    name = f"alice{uuid4().hex[:8]}"
    response = register(client, name)

    assert response.status_code == 201, response.text
    user_id = response.json()["data"]["user_id"]

    with rt.database.sessions() as db:
        user = db.scalar(select(AppUser).where(AppUser.user_id == user_id))
        assert user is not None
        assert user.bank_customer_id is not None
        assert user.bank_customer_id.startswith("u_pool_")
        assert user.bank_customer_id != "u_wealth", "新用户不得共用演示客户"
        claimed = db.scalar(
            select(BankCustomerPool).where(BankCustomerPool.claimed_by_user_id == user_id)
        )
        assert claimed is not None


def test_two_users_get_different_customers(rt):
    client = client_for(rt)
    first = register(client, f"bob{uuid4().hex[:8]}").json()["data"]["user_id"]
    second = register(client, f"carol{uuid4().hex[:8]}").json()["data"]["user_id"]

    with rt.database.sessions() as db:
        one = db.scalar(select(AppUser).where(AppUser.user_id == first))
        two = db.scalar(select(AppUser).where(AppUser.user_id == second))
    assert one.bank_customer_id != two.bank_customer_id


def test_login_name_is_case_and_space_insensitive(rt):
    """归一化是唯一的重名判据："Alice" 和 " alice " 是同一个账号。"""

    client = client_for(rt)
    name = f"dave{uuid4().hex[:6]}"
    assert register(client, name.upper()).status_code == 201

    duplicate = register(client, f"  {name}  ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "LOGIN_NAME_TAKEN"


def test_short_password_is_refused(rt):
    client = client_for(rt)
    response = register(client, f"erin{uuid4().hex[:8]}", password="short")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PASSWORD_POLICY"


# --- 登录 -----------------------------------------------------------------------------


def test_login_returns_a_csrf_token_and_sets_an_httponly_cookie(rt):
    client = client_for(rt)
    name = f"frank{uuid4().hex[:8]}"
    register(client, name)

    response = login(client, name)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["csrf_token"]

    cookies = (
        response.headers.get_list("set-cookie")
        if hasattr(response.headers, "get_list")
        else [response.headers["set-cookie"]]
    )
    joined = " | ".join(cookies).lower()
    assert "httponly" in joined
    assert "samesite=strict" in joined
    # The two cookies have different paths on purpose: the session token is scoped to the API,
    # while the CSRF token must be readable by `document.cookie` on the page at `/`. Putting
    # the CSRF cookie under `/api` made every write after a reload fail with
    # CSRF_TOKEN_MISSING — the browser test `_e2e/browser/auth-sync.cjs` caught it.
    session_cookie = next(item for item in cookies if item.startswith("bank_session=")).lower()
    # 登录时会顺手删掉旧路径（`/api`）的那一份 `bank_csrf`，所以同名条目不止一个；
    # 要看的是**真正写入**的那一条，即值非空的那条。
    csrf_cookie = next(
        item
        for item in cookies
        if item.startswith("bank_csrf=") and item.split("=", 1)[1].split(";", 1)[0].strip('"')
    ).lower()
    assert "path=/api" in session_cookie
    assert "path=/" in csrf_cookie and "path=/api" not in csrf_cookie
    assert "httponly" in session_cookie
    assert "httponly" not in csrf_cookie


def test_wrong_password_and_unknown_account_look_identical(rt):
    """A02：两种失败同一码同一文案，登录接口不能当账号探测器。"""

    client = client_for(rt)
    name = f"grace{uuid4().hex[:8]}"
    register(client, name)

    wrong = login(client, name, "definitely not the password")
    unknown = login(client, f"nobody{uuid4().hex[:8]}", PASSWORD)

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_repeated_failures_lock_the_account(rt):
    """A02：连续失败后限速生效，而且限速是按账号记的。"""

    client = client_for(rt)
    name = f"heidi{uuid4().hex[:8]}"
    register(client, name)

    for _ in range(rt.settings.auth_login_max_failures):
        assert login(client, name, "wrong password here").status_code == 401

    locked = login(client, name)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "LOGIN_RATE_LIMITED"


def test_a_disabled_account_cannot_log_in(rt):
    client = client_for(rt)
    name = f"ivan{uuid4().hex[:8]}"
    user_id = register(client, name).json()["data"]["user_id"]

    with rt.database.sessions.begin() as db:
        db.scalar(select(AppUser).where(AppUser.user_id == user_id)).status = "disabled"

    response = login(client, name)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


# --- 登录态 ---------------------------------------------------------------------------


def test_logout_revokes_the_session_immediately(rt):
    """A04：退出之后同一个 Cookie 立刻失效，不等到期。"""

    client = client_for(rt)
    name = f"judy{uuid4().hex[:8]}"
    register(client, name)
    token = login(client, name).json()["data"]["csrf_token"]

    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": token}).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_logout_all_ends_the_other_devices_session(rt):
    client = client_for(rt)
    name = f"ken{uuid4().hex[:8]}"
    register(client, name)

    phone = client_for(rt)
    csrf_phone = login(phone, name).json()["data"]["csrf_token"]
    laptop = client_for(rt)
    csrf_laptop = login(laptop, name).json()["data"]["csrf_token"]
    assert phone.get("/api/auth/me").status_code == 200

    laptop.post("/api/auth/logout-all", headers={"X-CSRF-Token": csrf_laptop})
    assert phone.get("/api/auth/me").status_code == 401
    del csrf_phone


def test_changing_the_password_revokes_every_session(rt):
    client = client_for(rt)
    name = f"laura{uuid4().hex[:8]}"
    register(client, name)
    csrf = login(client, name).json()["data"]["csrf_token"]

    changed = client.post(
        "/api/auth/password/change",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": PASSWORD, "new_password": "another good passphrase"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/api/auth/me").status_code == 401

    fresh = client_for(rt)
    assert login(fresh, name, PASSWORD).status_code == 401
    assert login(fresh, name, "another good passphrase").status_code == 200


def test_an_expired_session_is_refused(rt, monkeypatch):
    client = client_for(rt)
    name = f"mallory{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    real_datetime = identity_service.datetime

    class Shifted(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(hours=48)

    monkeypatch.setattr(identity_service, "datetime", Shifted)
    assert client.get("/api/auth/me").status_code == 401


def test_authentication_hits_the_database_every_time(rt):
    """没有进程内缓存：撤销要能立刻生效，就不能让某个 worker 手里留着一份旧答案。"""

    client = client_for(rt)
    name = f"niaj{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    queries = 0

    def count(*_args) -> None:
        nonlocal queries
        queries += 1

    event.listen(rt.database.engine, "before_cursor_execute", count)
    try:
        assert client.get("/api/auth/me").status_code == 200
        first = queries
        assert client.get("/api/auth/me").status_code == 200
        second = queries - first
    finally:
        event.remove(rt.database.engine, "before_cursor_execute", count)

    assert first > 0 and second > 0, "两次读取都必须查库"


# --- 演示旁路 -------------------------------------------------------------------------


def test_the_credential_free_entry_is_closed_by_default(rt, monkeypatch):
    """A03/A20：没有凭据就拿不到会话。

    这条钉的是"出厂默认"：本机 backend/.env.channels 为了演练会打开 ALLOW_DEMO_LOGIN，
    而开关落在运行时的 rt.settings 上，所以这里显式关掉再发请求。
    """

    monkeypatch.setattr(rt.settings, "allow_demo_login", False)
    client = client_for(rt)
    response = client.post("/api/session")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "DEMO_DISABLED"


def test_a_forged_session_id_is_not_a_credential(rt):
    client = client_for(rt)
    response = client.get("/api/auth/me", headers={"X-Session-Id": "sess_forged"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_REQUIRED"


# --- CSRF -----------------------------------------------------------------------------


def test_a_cookie_request_without_the_csrf_token_is_refused(rt):
    """A18：Cookie 是环境凭据，改动类请求必须带 CSRF token。"""

    client = client_for(rt)
    name = f"olivia{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    missing = client.post("/api/conversations", json={"title": "没有 token"})
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "CSRF_TOKEN_MISSING"

    csrf = client.get("/api/auth/me").status_code  # 只是确保会话还在
    assert csrf == 200


def test_a_wrong_csrf_token_is_told_apart_from_a_missing_one(rt):
    """两种失败要能分辨：没带 token 和带错了 token，处理方式不一样。"""

    client = client_for(rt)
    name = f"peggy2{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    wrong = client.post(
        "/api/conversations", headers={"X-CSRF-Token": "not-the-token"}, json={"title": "错 token"}
    )
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "CSRF_TOKEN_INVALID"


def test_a_cookie_request_with_the_csrf_token_succeeds(rt):
    client = client_for(rt)
    name = f"peggy{uuid4().hex[:8]}"
    register(client, name)
    csrf = login(client, name).json()["data"]["csrf_token"]

    created = client.post(
        "/api/conversations", headers={"X-CSRF-Token": csrf}, json={"title": "带 token"}
    )
    assert created.status_code in (200, 201), created.text


def test_a_bearer_request_does_not_need_a_csrf_token(rt):
    """Bearer 不是环境凭据：浏览器不会自动带上它，所以它本身不是 CSRF 载体。"""

    client = client_for(rt)
    name = f"quinn{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)
    token = client.cookies.get("bank_session")

    bare = client_for(rt)
    response = bare.post(
        "/api/conversations",
        headers={"Authorization": "Bearer " + token},
        json={"title": "无 CSRF"},
    )
    assert response.status_code in (200, 201), response.text


def test_a_cross_site_origin_is_refused(rt):
    client = client_for(rt)
    name = f"rupert{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    response = client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "ORIGIN_REJECTED"
    # 报文要能定位问题：说清收到的来源和本站认的来源，而不是「请刷新重试」。
    assert error["details"]["origin"] == "evil.example"
    assert error["details"]["expected"]


def test_a_same_origin_request_through_a_host_preserving_proxy_is_accepted(rt):
    """注册曾经在这条链路上全挂：代理把 `Host` 改写成了 `127.0.0.1:8000`，而浏览器
    `Origin` 说的是 `127.0.0.1:5173`，一次真正的同源请求被判成跨站。

    修法是让代理保留浏览器的 `Host`（`changeOrigin: false`）。这里钉住这条链路的报文形状：
    两边都是 5173。
    """

    client = client_for(rt)
    response = client.post(
        "/api/auth/register",
        headers={"Host": "127.0.0.1:5173", "Origin": "http://127.0.0.1:5173"},
        json={"login_name": f"sam{uuid4().hex[:8]}", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text


def test_a_proxy_that_rewrites_host_is_still_same_origin_via_x_forwarded_host(rt):
    """nginx 的 `$host` 会丢掉端口，`Host` 也与浏览器看到的不是一回事。

    `X-Forwarded-Host` 记着浏览器真正用的主机，校验认它——认它是安全的，因为跨站表单没法
    自己加这个头，而它护住的正是跨站表单。
    """

    client = client_for(rt)
    response = client.post(
        "/api/auth/register",
        headers={
            "Host": "127.0.0.1:8000",
            "X-Forwarded-Host": "bank.example:8088",
            "Origin": "http://bank.example:8088",
        },
        json={"login_name": f"tina{uuid4().hex[:8]}", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text


# --- 管理员边界 -----------------------------------------------------------------------


def test_a_normal_user_cannot_touch_the_global_model_config(rt):
    """A14：全局配置只有管理员能动，普通用户连读都不行。"""

    client = client_for(rt)
    name = f"sybil{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    assert client.get("/api/settings/llm").status_code == 403
    assert client.get("/api/queue/stats").status_code == 403


def test_a_normal_user_cannot_move_the_simulated_clock(rt):
    """A14：时钟是全局的，一个人拨动就是所有人的时间变了。"""

    client = client_for(rt)
    name = f"trent{uuid4().hex[:8]}"
    register(client, name)
    csrf = login(client, name).json()["data"]["csrf_token"]

    response = client.post(
        "/api/demo/clock/advance",
        headers={"X-CSRF-Token": csrf},
        json={"days": 1, "hours": 0},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_ROLE"


def test_an_admin_can_read_the_global_model_config(rt):
    client = client_for(rt)
    name = f"uma{uuid4().hex[:8]}"
    user_id = register(client, name).json()["data"]["user_id"]

    with rt.database.sessions.begin() as db:
        db.scalar(select(AppUser).where(AppUser.user_id == user_id)).role = "admin"

    login(client, name)
    assert client.get("/api/settings/llm").status_code == 200


# --- 越权 -----------------------------------------------------------------------------


def test_one_user_cannot_read_another_users_conversation(rt):
    """A06：换账号看不到别人的会话，按 404 处理。"""

    alice = client_for(rt)
    name = f"vera{uuid4().hex[:8]}"
    register(alice, name)
    csrf = login(alice, name).json()["data"]["csrf_token"]
    conversation = alice.post(
        "/api/conversations", headers={"X-CSRF-Token": csrf}, json={"title": "私有"}
    ).json()["data"]["conversation_id"]

    bob = client_for(rt)
    bob_name = f"walter{uuid4().hex[:8]}"
    register(bob, bob_name)
    bob_csrf = login(bob, bob_name).json()["data"]["csrf_token"]
    response = bob.get(f"/api/conversations/{conversation}", headers={"X-CSRF-Token": bob_csrf})
    assert response.status_code == 404


def test_me_reports_whether_the_bank_is_ready(rt):
    client = client_for(rt)
    name = f"xena{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    data = client.get("/api/auth/me").json()["data"]
    assert data["bank_ready"] is True
    assert data["login_name"] == name
    assert data["role"] == "user"


def test_a_response_that_carries_an_identity_is_not_cacheable(rt):
    """A18：带身份的响应不许进浏览器缓存，否则回退键会放出上一个人的会话与报告。"""

    client = client_for(rt)
    name = f"nina{uuid4().hex[:8]}"
    register(client, name)
    login(client, name)

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_the_stream_keeps_its_own_cache_header(rt):
    """SSE 不能被缓冲，它声明的是 `no-cache, no-transform`，不是统一那句 `no-store`。"""

    client = client_for(rt)
    name = f"omar{uuid4().hex[:8]}"
    register(client, name)
    csrf = login(client, name).json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    created = client.post("/api/conversations", json={}, headers=headers).json()["data"]

    response = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )

    assert response.headers["Cache-Control"] == "no-cache, no-transform"


def test_the_demo_archive_has_no_credential_until_ops_sets_one(rt):
    """`user_0001` 保留为受控档案：迁移不会给它编一个默认密码。"""

    with rt.database.sessions() as db:
        user = db.scalar(select(AppUser).where(AppUser.user_id == "user_0001"))
    assert user is not None
    assert user.role == "user"

    client = client_for(rt)
    response = login(client, "user_0001", PASSWORD)
    assert response.status_code == 401
