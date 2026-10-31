"""共享 fixture。

**为什么不是每个用例一个 schema**：迁移要在远程库上建 17 张表加索引，
一次约 7 秒。252 个用例各建一次就是半小时的纯开销。所以这里：

- `shared_schema`：整个会话只建一次 schema、只跑一次迁移。
- 每个用例通过 `SeedService.seed()` 把数据重置回固定种子——seed 本来就是
  「清空业务表 + 重新插入 + 逐账户对账」，天然就是用例级的隔离手段，且比建表快得多。
- `private_schema`：只给 schema 契约测试用（它们要亲手建表、降级、插脏数据），
  这些用例需要真正独立的 schema，因此仍然按用例创建。
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = ROOT / "alembic.ini"

# 服务凭证与运维口令：由 `client` fixture 注入到每个请求的默认头里。
# 设成模块级常量并在导入时就写进环境，是因为 `get_settings()` 会在第一次请求时被中间件
# 读取，晚于任何 fixture 的 setup；这里早于它能保证进程内始终有一致的值。
SERVICE_TOKEN = "test-service-token"
OPS_TOKEN = "test-ops-token"
os.environ.setdefault("SERVICE_TOKEN", SERVICE_TOKEN)
os.environ.setdefault("OPS_TOKEN", OPS_TOKEN)


def _admin_engine(url: str):
    return create_engine(url, connect_args={"connect_timeout": 10})


def _create_schema(url: str, name: str) -> None:
    admin = _admin_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
    finally:
        admin.dispose()


def _drop_schema(url: str, name: str) -> None:
    admin = _admin_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
    finally:
        admin.dispose()


def _activate(url: str, name: str) -> tuple[str | None, str | None]:
    """让后续连接（含 alembic）都落在指定 schema 上。"""
    previous = (os.environ.get("PGOPTIONS"), os.environ.get("DATABASE_URL"))
    os.environ["PGOPTIONS"] = f"-c search_path={name}"
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    return previous


def _restore(previous: tuple[str | None, str | None]) -> None:
    pgoptions, database_url = previous
    for key, value in (("PGOPTIONS", pgoptions), ("DATABASE_URL", database_url)):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL") or get_settings().test_database_url
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    return url


@pytest.fixture(scope="session")
def shared_schema(database_url):
    """会话级 schema：只建一次、只迁移一次，数据由各用例自己 seed 重置。"""
    name = "test_bank_shared_" + uuid4().hex[:12]
    _create_schema(database_url, name)
    previous = _activate(database_url, name)
    try:
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        yield name
    finally:
        _restore(previous)
        _drop_schema(database_url, name)


@pytest.fixture
def database(database_url, shared_schema):
    """指向共享 schema 的 Database；引擎按用例创建并在用例结束时释放。"""
    from app.storage.database import Database

    db = Database(database_url)
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def private_schema(database_url):
    """schema 契约测试专用：独立 schema + 独立迁移。"""
    name = "test_bank_private_" + uuid4().hex[:12]
    _create_schema(database_url, name)
    previous = _activate(database_url, name)
    try:
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        yield name
    finally:
        _restore(previous)
        _drop_schema(database_url, name)


@pytest.fixture
def engine(database_url, private_schema):
    """落在独立 schema 上的引擎（配合 private_schema 使用）。"""
    eng = _admin_engine(database_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def connection(engine):
    with engine.begin() as conn:
        yield conn


class AppStyleClient:
    """代表 app 后端的测试客户端：每个请求都自带服务凭证与 user_id。

    为什么要有这一层：银行现在**拒绝没有身份的调用**，而 app 侧的 `BankClient` 总是把
    `user_id` 同时放进 query 和 body。测试如果直接调裸 TestClient，就要在每个请求里手写
    一遍身份——那既啰嗦，也偏离了被测的真实调用形态。这一层把「app 会做的事」补上，
    于是 `seeded_client.get("/accounts/a_zhang_current/balance")` 依然成立。

    显式传了 `user_id` 的请求不会被覆盖，所以「以 u_nobody 的身份操作」这类用例照样能写；
    想验证「缺身份被拒绝」就显式传 `user_id=""`，或者用 `anonymous_client`。
    """

    def __init__(self, client, default_user_id: str = "u_zhang") -> None:
        self._client = client
        self._default_user_id = default_user_id

    def request(self, method: str, url: str, **kwargs):
        params = dict(kwargs.pop("params", None) or {})
        body = kwargs.get("json")
        body_user = body.get("user_id") if isinstance(body, dict) else None
        if "user_id" not in params and not body_user:
            params["user_id"] = self._default_user_id
        # POST 路由读 body.user_id、GET 路由读 query，app 侧的 BankClient 两处都写，
        # 这里必须照做：只补 query 会让所有写接口仍然报缺身份。
        if isinstance(body, dict) and not body_user:
            kwargs["json"] = {**body, "user_id": self._default_user_id}
        return self._client.request(method, url, params=params, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def __getattr__(self, item):
        return getattr(self._client, item)


@pytest.fixture
def client(database):
    """把 API 的 get_database 依赖指向测试 schema 上的 Database。

    默认头带上服务凭证与运维口令，所以绝大多数用例不需要关心访问控制。想验证「没有
    凭证会被拒绝」的用例用 `anonymous_client`。
    """
    from fastapi.testclient import TestClient

    from app.api.deps import get_database
    from app.main import app

    app.dependency_overrides[get_database] = lambda: database
    try:
        with TestClient(
            app,
            raise_server_exceptions=False,
            headers={"X-Service-Token": SERVICE_TOKEN, "X-Ops-Token": OPS_TOKEN},
        ) as test_client:
            yield AppStyleClient(test_client)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def anonymous_client(database):
    """不带任何凭证的客户端：只用于验证服务凭证确实拦得住调用。"""

    from fastapi.testclient import TestClient

    from app.api.deps import get_database
    from app.main import app

    app.dependency_overrides[get_database] = lambda: database
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def seeded_database(database):
    """已完成种子写入的 Database。seed 会清空业务表并重建固定种子。"""
    from app.domain.seed_service import SeedService

    SeedService(database).seed()
    return database


@pytest.fixture
def seeded_client(client, seeded_database):
    """连到已重置为固定种子的测试 schema 的 API 客户端。"""
    return client


# ---------------------------------------------------------------- 理财辅助

# 五题全选 2：总分 10，等级 high，可申购任意风险等级的产品。
HIGH_RISK_ANSWERS = {"q1": 2, "q2": 2, "q3": 2, "q4": 2, "q5": 2}
# 五题全选 0：总分 0，等级 low。
LOW_RISK_ANSWERS = {"q1": 0, "q2": 0, "q3": 0, "q4": 0, "q5": 0}


def mfa_code() -> str:
    """配置的演示验证码；理财写接口固定校验它。"""
    from app.core.config import get_settings

    return get_settings().mock_mfa_code


def submit_assessment(database, user_id: str, answers: dict | None = None) -> dict:
    """直接走领域服务保存一份测评，返回银行计算后的结果。"""
    from app.domain.wealth_risk_service import WealthRiskService

    return WealthRiskService(database).submit(
        user_id=user_id, answers=dict(answers or HIGH_RISK_ANSWERS)
    )


@pytest.fixture
def wealth_risk_profile(seeded_database):
    """u_zhang 的有效高风险测评；理财写接口要求有有效测评。"""
    return submit_assessment(seeded_database, "u_zhang")
