"""The demo control plane's clock routes.

The simulated clock is what "today" means for every policy decision, due-date calculation
and birthday reminder, so the three routes that move it have to hold three lines:

- closed unless the *running* application's settings say demo mode (`runtime.settings`, not
  the global `get_settings()`, because that is the object an operator or a test flips);
- "now" is the bank's clock and never this machine's -- the point of a simulated clock is
  that the clock the user sees is the bank's;
- a bank failure arrives as `502 BANK_UNAVAILABLE`, never as the catch-all 500 an escaping
  `BankCallError` would produce.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.clock import SystemClock
from app.integration.bank_client import BankResponse
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.main import create_app

OPERATOR = "demo_operator"

# The bank's `GET /clock` view: the simulated clock plus the two fields the dashboard shows
# next to it (`bank_backend/app/domain/clock_service.py:96-101`).
CLOCK_VIEW = {
    "current_time": "2026-03-01T09:00:00+08:00",
    "run_id": "7",
    "due_task_count": 2,
}


class FakeBank:
    """Stands in for `BankClient`, recording every call and answering per route.

    A route key is `(method, path)`. `error` makes every call fail, which is how a bank that
    is down is staged; it is raised rather than returned because `BankClient` raises too.
    """

    def __init__(self, replies=None, *, error=None):
        self.replies = dict(replies or {})
        self.error = error
        self.calls = []

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "user_id": user_id,
                "params": params,
                "json": json,
                "idempotency_key": idempotency_key,
            }
        )
        if self.error is not None:
            raise self.error
        try:
            reply = self.replies[(method, path)]
        except KeyError:
            raise AssertionError(f"unexpected bank call {method} {path}") from None
        return BankResponse(code=0, message="success", data=reply)


def client_for(rt):
    """The app on this runtime, signed in as the administrator.

    P1 之后演示控制面不再是匿名可用的：推时钟会影响所有人的「今天」，所以它要求管理员
    会话。这里照着 conftest 的 `session` 夹具做同样的事——先把演示档案提成管理员，再
    用 `create_demo_session()` 签发一个真实的 bearer——而不是去伪造一个身份头，
    否则测的就不是真实的门禁了。
    """

    with rt.database.sessions.begin() as db:
        db.execute(text("UPDATE app_user SET role = 'admin' WHERE user_id = 'user_0001'"))
    issued = rt.identity.create_demo_session()
    return TestClient(
        create_app(rt.settings, runtime=rt),
        headers={"Authorization": "Bearer " + issued["token"]},
    )


def calls_to(fake, method, path):
    return [call for call in fake.calls if call["method"] == method and call["path"] == path]


def test_the_control_plane_is_closed_outside_demo_mode(rt, monkeypatch):
    """The switch is the runtime's settings, so flipping it closes all three routes."""

    fake = FakeBank()
    monkeypatch.setattr(rt, "bank_client", fake)
    # 先登进来再关开关：关掉之后连演示会话都签不出来，那测的就只是「签不出会话」。
    client = client_for(rt)
    monkeypatch.setattr(rt.settings, "demo_mode", False)

    responses = {
        "clock": client.get("/api/demo/clock"),
        "advance": client.post("/api/demo/clock/advance", json={"days": 1}),
        "run-tasks": client.post("/api/demo/clock/run-tasks"),
    }

    for name, response in responses.items():
        assert response.status_code == 403, (name, response.text)
        assert response.json()["error"]["code"] == "DEMO_DISABLED", name
    assert fake.calls == []


def test_advance_reads_the_bank_clock_before_moving_it(rt, monkeypatch):
    """The step is added to the bank's time, and the bank is told the result."""

    advanced = {**CLOCK_VIEW, "current_time": "2026-03-03T09:00:00+08:00"}
    fake = FakeBank({("GET", "/clock"): dict(CLOCK_VIEW), ("POST", "/clock"): advanced})
    monkeypatch.setattr(rt, "bank_client", fake)
    invalidated = []
    monkeypatch.setattr(SystemClock, "invalidate", lambda: invalidated.append(1))

    response = client_for(rt).post("/api/demo/clock/advance", json={"days": 2})

    assert response.status_code == 200, response.text
    assert response.json()["data"] == advanced
    assert [(call["method"], call["path"]) for call in fake.calls] == [
        ("GET", "/clock"),
        ("POST", "/clock"),
    ]
    # Two days after the bank's 2026-03-01, not two days after this machine's today.
    assert fake.calls[-1]["json"] == {"current_time": "2026-03-03T09:00:00+08:00"}
    assert {call["user_id"] for call in fake.calls} == {OPERATOR}
    assert invalidated == [1]


@pytest.mark.parametrize(
    "body",
    [{}, {"days": 0, "hours": 0}, {"days": 366}, {"hours": 25}, {"days": -1}],
)
def test_advance_refuses_a_step_it_cannot_make(rt, monkeypatch, body):
    """Zero is a business refusal (422); the ranges are pydantic's. Neither reaches the bank."""

    fake = FakeBank()
    monkeypatch.setattr(rt, "bank_client", fake)

    response = client_for(rt).post("/api/demo/clock/advance", json=body)

    assert response.status_code == 422, (body, response.text)
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"
    if body in ({}, {"days": 0, "hours": 0}):
        assert response.json()["error"]["message"] == "advance 至少需要向前拨 1 小时"
    assert fake.calls == []


def test_advance_treats_a_bank_reply_without_a_clock_as_unavailable(rt, monkeypatch):
    """A view without `current_time` cannot be advanced, and guessing one would be worse."""

    fake = FakeBank({("GET", "/clock"): {"run_id": "7", "due_task_count": 2}})
    monkeypatch.setattr(rt, "bank_client", fake)
    invalidated = []
    monkeypatch.setattr(SystemClock, "invalidate", lambda: invalidated.append(1))

    response = client_for(rt).post("/api/demo/clock/advance", json={"hours": 1})

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "BANK_UNAVAILABLE"
    assert calls_to(fake, "POST", "/clock") == []
    assert invalidated == []


BANK_DOWN = BankCallError(BankErrorKind.TIMEOUT, "银行服务响应超时，请稍后重试。", retryable=True)


@pytest.mark.parametrize(
    ("path", "body"),
    [("/api/demo/clock/advance", {"days": 1}), ("/api/demo/clock/run-tasks", None)],
)
def test_a_bank_failure_is_reported_as_bank_unavailable(rt, monkeypatch, path, body):
    """A retryable upstream failure stays 502, and nothing is invalidated on the way out."""

    fake = FakeBank(error=BANK_DOWN)
    monkeypatch.setattr(rt, "bank_client", fake)
    invalidated = []
    monkeypatch.setattr(SystemClock, "invalidate", lambda: invalidated.append(1))
    client = client_for(rt)

    response = client.post(path) if body is None else client.post(path, json=body)

    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == "BANK_UNAVAILABLE"
    assert error["retryable"] is True
    assert invalidated == []


def test_the_clock_route_reports_the_banks_time(rt, monkeypatch):
    """`GET /api/demo/clock` passes the bank's view through, without reading the local clock."""

    fake = FakeBank({("GET", "/clock"): dict(CLOCK_VIEW)})
    monkeypatch.setattr(rt, "bank_client", fake)

    def local_clock_must_not_be_read(*args, **kwargs):
        raise AssertionError("the demo clock route must not read this machine's clock")

    monkeypatch.setattr(SystemClock, "now", local_clock_must_not_be_read)

    response = client_for(rt).get("/api/demo/clock")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == CLOCK_VIEW
    assert [(call["method"], call["path"]) for call in fake.calls] == [("GET", "/clock")]
    assert fake.calls[0]["user_id"] == OPERATOR


def test_run_tasks_forwards_the_outcome_and_refreshes_the_cache(rt, monkeypatch):
    """An explicit outcome is forwarded; a bare body stays bare. Both refresh the cache."""

    ran = {"processed_count": 1, "completed_count": 1, "failed_count": 0, "items": []}
    fake = FakeBank({("POST", "/clock/run-tasks"): ran})
    monkeypatch.setattr(rt, "bank_client", fake)
    invalidated = []
    monkeypatch.setattr(SystemClock, "invalidate", lambda: invalidated.append(1))
    client = client_for(rt)

    explicit = client.post("/api/demo/clock/run-tasks", json={"birthday_outcome": "fail"})
    bare = client.post("/api/demo/clock/run-tasks")

    assert explicit.status_code == 200, explicit.text
    assert bare.status_code == 200, bare.text
    assert explicit.json()["data"] == ran
    assert fake.calls[0]["json"] == {"birthday_outcome": "fail"}
    assert fake.calls[1]["json"] is None
    assert {call["user_id"] for call in fake.calls} == {OPERATOR}
    assert invalidated == [1, 1]
