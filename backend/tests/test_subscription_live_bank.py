"""第二层联调：订阅模块对真实运行的 bank_backend 发请求。

这是订阅唯一走真实银行 HTTP 的测试，未设置 SUBSCRIPTION_TEST_BANK_URL 时跳过。

    cd bank_backend
    uv run uvicorn app.main:app --port 8001
    # 另一个终端
    cd backend
    $env:SUBSCRIPTION_TEST_BANK_URL = "http://127.0.0.1:8001/api"
    uv run pytest tests/test_subscription_live_bank.py -q -s

取消在银行里是终态、会污染演示数据，所以本文件只跑可逆的暂停/恢复，并且无论成败都在
finally 里恢复；要复位演示数据用 `POST /api/seed/reset` 或重跑订阅种子脚本。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.integration.bank_client import BankClient
from app.modules.money import format_minor
from app.modules.registry import build_registry
from app.modules.subscription import MODULE
from app.tools.bank_adapter import BankToolAdapter
from tests.support.domain_harness import run_domain
from tests.support.scripted_model import ScriptedModel

BANK_URL_ENV = "SUBSCRIPTION_TEST_BANK_URL"
CUSTOMER_ENV = "SUBSCRIPTION_TEST_BANK_CUSTOMER"
DEMO_CUSTOMER = "u_wealth"
NETFLIX = "Netflix"
SPOTIFY = "Spotify"
NETFLIX_MINOR = 5900
SPOTIFY_MINOR = 2900


@pytest.fixture(scope="module")
def bank_url() -> str:
    url = os.environ.get(BANK_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"未设置 {BANK_URL_ENV}：本文件只对真实运行的 bank_backend 执行")
    return url


@pytest.fixture(scope="module")
def customer() -> str:
    return os.environ.get(CUSTOMER_ENV, "").strip() or DEMO_CUSTOMER


@pytest.fixture(scope="module")
def client(bank_url):
    bank = BankClient(bank_url, connect_timeout=5.0, read_timeout=20.0)
    try:
        yield bank
    finally:
        bank.close()


@pytest.fixture(scope="module")
def adapter(client):
    """The production adapter bound to the real bank: the shape the module graph runs with."""

    registry = build_registry([MODULE])
    return BankToolAdapter(registry, client)


def rows(client, customer) -> list[dict]:
    response = client.request("GET", "/subscriptions", user_id=customer)
    assert response.code == 0, response.message
    return response.data


def one(client, customer, merchant) -> dict:
    matches = [row for row in rows(client, customer) if row["merchant_name"] == merchant]
    assert len(matches) == 1, f"{merchant} 在演示库中应恰好一条，实际 {len(matches)}"
    return matches[0]


def test_live_listing_reads_the_seeded_subscriptions(client, adapter, customer):
    """The module's read tool really reads the seeded rows over HTTP."""

    model = ScriptedModel([("call", "subscription_list", "{}"), "已列出你的订阅。"])
    result = run_domain(
        MODULE,
        model=model,
        adapter=adapter,
        customer=customer,
        message="我有哪些自动扣费的订阅？",
    )
    assert result.status == "SUCCEEDED"
    assert result.final_text == "已列出你的订阅。"
    seen = str(model.messages[1])
    assert NETFLIX in seen and SPOTIFY in seen
    assert format_minor(NETFLIX_MINOR) in seen and format_minor(SPOTIFY_MINOR) in seen


def test_live_due_dates_come_from_the_bank(client, adapter, customer):
    """Reminder dates are the bank's, not the host clock's."""

    model = ScriptedModel([("call", "subscription_list", "{}"), "最近有订阅要续费。"])
    result = run_domain(
        MODULE,
        model=model,
        adapter=adapter,
        customer=customer,
        message="最近有什么订阅要续费？",
    )
    assert result.status == "SUCCEEDED"
    seen = str(model.messages[1])
    for row in rows(client, customer):
        assert row["next_due_date"]
        assert str(row["next_due_date"]) in seen


def test_live_pause_then_resume_really_writes_and_restores(client, adapter, customer):
    spotify = one(client, customer, SPOTIFY)
    if spotify["status"] == "cancelled":
        pytest.skip(f"{SPOTIFY} 已被取消，请先 POST /api/seed/reset 复位演示数据")
    if spotify["status"] == "paused":
        client.request(
            "POST", f"/subscriptions/{spotify['subscription_id']}/resume", user_id=customer
        )

    try:
        # A write tool only freezes a snapshot and raises the card; the bank stays untouched.
        parked = run_domain(
            MODULE,
            replies=[("call", "subscription_pause", '{"merchant_name":"Spotify"}')],
            adapter=adapter,
            customer=customer,
            message="帮我把 Spotify 停一停",
        )
        assert parked.status == "WAITING_INPUT"
        question = next(event.data for event in parked.events if event.event == "interrupt")
        assert question["kind"] == "confirmation"
        assert question["confirmation"]["amount_minor"] == SPOTIFY_MINOR
        assert one(client, customer, SPOTIFY)["status"] == "active"

        # The button click is what writes, and it writes the frozen operation.
        paused = run_domain(
            MODULE,
            replies=[("call", "subscription_pause", '{"merchant_name":"Spotify"}')],
            adapter=adapter,
            customer=customer,
            message="帮我把 Spotify 停一停",
            answer={"decision": "confirm"},
        )
        assert paused.status == "SUCCEEDED"
        assert one(client, customer, SPOTIFY)["status"] == "paused"

        resumed = run_domain(
            MODULE,
            replies=[("call", "subscription_resume", '{"merchant_name":"Spotify"}')],
            adapter=adapter,
            customer=customer,
            message="把 Spotify 恢复",
            answer={"decision": "confirm"},
        )
        assert resumed.status == "SUCCEEDED"
        assert one(client, customer, SPOTIFY)["status"] == "active"
    finally:
        current = one(client, customer, SPOTIFY)
        if current["status"] == "paused":
            client.request(
                "POST", f"/subscriptions/{current['subscription_id']}/resume", user_id=customer
            )


def test_live_preview_charges_nothing(client, adapter, customer):
    before = one(client, customer, NETFLIX)
    if before["status"] == "cancelled":
        pytest.skip(f"{NETFLIX} 已被取消，请先 POST /api/seed/reset 复位演示数据")
    parked = run_domain(
        MODULE,
        replies=[("call", "subscription_charge", '{"merchant_name":"Netflix"}')],
        adapter=adapter,
        customer=customer,
        message="现在就扣 Netflix 这一期",
    )
    after = one(client, customer, NETFLIX)
    assert after["last_charge_id"] == before["last_charge_id"]
    if parked.status != "WAITING_INPUT":
        # A bank refusal is a legitimate outcome; an unconfirmed charge is not.
        assert parked.status in ("FAILED", "DENIED")


def test_the_module_does_not_depend_on_the_frontend_or_the_platform():
    """The module may use the shared base agent helpers, but not the frontend or turn runtime."""

    import app.modules.subscription as subscription_package

    # app.agents.supervisor is the shared base layer every graph module builds on (card,
    # transfer and wealth import it too). What a module must never reach into is the frontend
    # (fastapi / app.main) or the platform's whole-turn wiring (app.agents.runtime).
    forbidden = ("app.main", "app.agents.runtime", "fastapi")
    root = Path(subscription_package.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and any(
                name in stripped for name in forbidden
            ):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == [], f"订阅包不应依赖前端或平台整轮接线：{offenders}"
