import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import Settings, get_settings
from app.core.database_file import sqlalchemy_url
from app.core.eventloop import configure_event_loop
from app.runtime import Runtime
from app.storage.seed import seed

# psycopg's async connections refuse the Proactor loop, and pytest builds its loop
# after this module is imported, so the policy has to be installed here.
configure_event_loop()

# Development and test share the `test` database on the cloud instance named by the
# committed `database.txt`, created by `scripts/init_test_database.py`. Tests never use that
# database's `public` schema -- each one gets a schema of its own -- so a test run cannot
# reach development's tables or the other project's database.
TEST_DATABASE_NAME = "test"

# Schemas the per-test fixtures create. Anything matching these that is still present at
# session start was left behind by a run that was killed before its cleanup could run.
TEST_SCHEMA_PREFIXES = ("test_runtime_", "test_langgraph_")

# A leftover is only removed once it is this old. pytest-xdist runs several sessions at
# once and every one of them scans for leftovers, so without a grace period the first
# worker to start would drop the schemas its siblings are still using.
STALE_SCHEMA_GRACE_SECONDS = 2 * 60 * 60


def test_schema_name(prefix: str) -> str:
    """A private schema whose name records the moment it was created.

    The age has to be readable from the name itself: it is what tells a live sibling
    worker's schema apart from one that a killed run left behind.
    """

    return f"{prefix}{int(time.time())}_{uuid4().hex}"


def stale_schema_created_at(name: str) -> float | None:
    """When a test schema was created, or None for a name that carries no timestamp."""

    for prefix in TEST_SCHEMA_PREFIXES:
        if name.startswith(prefix):
            stamp = name[len(prefix) :].split("_", 1)[0]
            return float(stamp) if stamp.isdigit() else None
    return None


def pytest_configure(config):
    """Re-assert the policy after plugins load, before any event loop exists."""

    configure_event_loop()
    del config


def resolve_test_database_url() -> str | None:
    """The test DSN: `TEST_DATABASE_URL` when set, otherwise the shared credentials.

    Returns `None` only if the credentials cannot be read at all, so that a checkout
    without them skips the database tests instead of erroring during collection.
    """

    override = os.getenv("TEST_DATABASE_URL")
    if override:
        return override
    try:
        return sqlalchemy_url(TEST_DATABASE_NAME)
    except RuntimeError:
        return None


def require_test_database(url: str) -> None:
    """Fail with the setup command instead of a confusing connection error."""

    engine = create_engine(url, connect_args={"connect_timeout": 10})
    try:
        with engine.connect():
            return
    except Exception as exc:  # the database may simply not exist yet
        raise pytest.UsageError(
            f"cannot connect to the test database ({type(exc).__name__}). "
            "Create it once with: cd backend; uv run python scripts/init_test_database.py"
        ) from exc
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def drop_stale_test_schemas():
    """Remove test schemas that a killed run could not clean up.

    The per-test fixtures drop their own schema in a `finally`, which does not run when
    the process is killed -- a timeout, Ctrl-C, a crashed worker. Without this, those
    leftovers accumulate in the shared database and nobody notices.

    Only leftovers old enough to predate this run are removed, so that pytest-xdist
    workers -- each of which starts a session and runs this fixture -- do not delete the
    schemas their siblings are using right now.
    """

    url = resolve_test_database_url()
    if not url:
        return
    require_test_database(url)
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as connection:
            condition = " OR ".join(
                f"schema_name LIKE '{prefix}%'" for prefix in TEST_SCHEMA_PREFIXES
            )
            stale = []
            now = time.time()
            for row in connection.execute(
                text(f"SELECT schema_name FROM information_schema.schemata WHERE {condition}")
            ):
                name = row[0]
                created = stale_schema_created_at(name)
                if created is None or now - created > STALE_SCHEMA_GRACE_SECONDS:
                    stale.append(name)
            for name in stale:
                # The name comes from the catalog and already matched a fixed prefix.
                connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        if stale:
            print(f"\ndropped {len(stale)} stale test schema(s) from an earlier run")
    finally:
        engine.dispose()


def psycopg_dsn(url: str) -> str:
    """libpq spelling of a SQLAlchemy URL; psycopg does not accept the dialect prefix."""

    return url.replace("postgresql+psycopg://", "postgresql://")


class RecordingPoolRefiller:
    """替身：只记录「注册有没有去补位」，绝不连 8001。

    注册路径在两类情况下会调它：领到客户之后补一格（`refill`），以及池子空了给这位
    用户直接建一个（`provision_for`）。两者失败都不影响注册，所以这里返回 None 是最
    接近真实常态的替身——银行没起、或者补位没抢到号。

    `raises` 让用例可以模拟「补位器自己炸了」，验证它不会把注册带崩。
    """

    def __init__(self) -> None:
        self.refills = 0
        self.provisioned: list[str] = []
        self.raises = False

    def refill(self):
        self.refills += 1
        if self.raises:
            raise RuntimeError("refill exploded")
        return None

    def provision_for(self, user_id: str):
        self.provisioned.append(user_id)
        if self.raises:
            raise RuntimeError("provision exploded")
        return None


@pytest.fixture
def rt(monkeypatch):
    """Every integration test gets a private schema, never a user's existing tables."""
    url = resolve_test_database_url()
    if not url:
        pytest.skip("Set TEST_DATABASE_URL or provide database.txt to run PostgreSQL tests")
    schema = test_schema_name("test_runtime_")
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", url)
    # 与 bank_backend/tests/conftest.py 里的常量一致：注册路径会真的出一个「补位」请求，
    # 这里只是让那个客户端有一个非空凭证，避免每个用例都打一条「未配置」告警。
    monkeypatch.setenv("BANK_SERVICE_TOKEN", "test-service-token")
    get_settings.cache_clear()
    runtime = None
    try:
        command.upgrade(Config("alembic.ini"), "head")
        settings = Settings(database_url=url)
        runtime = Runtime(settings)
        # 默认的 PoolRefiller 会真的连 8001。测试里换成记录器：注册用例要断言的是
        # 「注册有没有去补位」，而不是「补位成不成」——后者由 tests/test_pool_refill.py
        # 用假银行客户端单独覆盖。顺带保证测试不往开发库的客户池里写东西。
        runtime.identity.refiller = RecordingPoolRefiller()
        seed(runtime.database, settings)
        yield runtime
    finally:
        if runtime:
            runtime.database.dispose()
        monkeypatch.delenv("PGOPTIONS", raising=False)
        get_settings.cache_clear()
        # schema is generated above, not derived from the database URL or caller input.
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def session(rt):
    """A caller for the demo archive, promoted to administrator.

    Most suites exercise the global control plane (model configuration, queue stats, the
    simulated clock), and since P1 those routes require an administrator. Promoting here keeps
    the rule itself intact -- nothing in the API can grant a role -- while letting a test reach
    the surface it is about. Tests that need a plain user register one.
    """

    with rt.database.sessions.begin() as db:
        db.execute(text("UPDATE app_user SET role = 'admin' WHERE user_id = 'user_0001'"))
    return rt.identity.create_demo_session()


@pytest.fixture
def schema_url():
    """A private schema for the LangGraph checkpointer, dropped when the test ends.

    The checkpointer opens its own psycopg connection and does not read PGOPTIONS, so
    the schema travels in the DSN instead.
    """

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    schema = test_schema_name("test_langgraph_")
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    dsn = psycopg_dsn(url)
    separator = "&" if "?" in dsn else "?"
    try:
        yield f"{dsn}{separator}options=-csearch_path%3D{schema}"
    finally:
        # schema is generated above, not derived from the database URL or caller input.
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


# A real HTTP endpoint on loopback, shared by every test module that configures a model:
# one asserts what the SDK put on the wire, another needs a probe that actually succeeds.
# It lives here so neither module owns it and both can rely on the same behaviour.


class ReplyHandler(BaseHTTPRequestHandler):
    """A local Chat Completions endpoint that records what the SDK actually sent."""

    captured: dict = {}
    content: str | None = '{"agent":null,"reply":"好的"}'

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).captured["path"] = self.path
        type(self).captured["authorization"] = self.headers.get("Authorization")
        type(self).captured["body"] = json.loads(self.rfile.read(length))
        body = json.dumps(
            {
                "id": "chatcmpl_local",
                "object": "chat.completion",
                "created": 1,
                "model": "mock-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": type(self).content},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        del args


@pytest.fixture
def provider():
    """A local HTTP provider, so the real openai SDK path is exercised end to end."""

    ReplyHandler.captured = {}
    ReplyHandler.content = '{"agent":null,"reply":"好的"}'
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReplyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def payload(port: int, **overrides) -> dict:
    """A valid settings body pointing at the local provider on `port`."""

    body = {
        "base_url": f"http://127.0.0.1:{port}/v1",
        "api_key": "sk-local-protocol",
        "model": "mock-model",
        "response_format": "json_schema",
        "timeout_seconds": 10,
    }
    body.update(overrides)
    return body
