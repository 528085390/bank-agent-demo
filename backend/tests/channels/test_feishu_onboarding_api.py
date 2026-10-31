"""Web 端「IM 接入」的端点契约：扫码、状态、取消扫码、取消接入。

服务层（app.channels.feishu.onboarding）通过 app.state 注入：这些用例不发真实网络请求、
不连飞书、不起长连接，只钉住端点对外的承诺——出什么字段、不泄露什么、什么时候写盘、
什么时候调用启停钩子、解除绑定时清到什么程度。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.channels.feishu import setup as fs
from app.channels.feishu.onboarding import FeishuOnboarding
from app.core.config import Settings
from app.main import create_app
from app.storage.models import ChannelBinding

BEGIN = {
    "device_code": "dc_secret_value",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://open.feishu.cn/page/launcher",
    "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=ABCD-EFGH",
    "expires_in": 3600,
    "interval": 5,
}
AUTHORIZED = {
    "client_id": "cli_personal",
    "client_secret": "sec_personal_secret_value",
    "user_info": {"open_id": "ou_owner"},
}


class FakeFeishu:
    """设备流的替身：记下被问了几次，按剧本回答。"""

    def __init__(self, poll_answers=None):
        self.poll_answers = list(poll_answers or [{"error": "authorization_pending"}])
        self.calls: list[dict] = []

    def __call__(self, url, form, timeout):
        action = form.get("action")
        self.calls.append({"action": action, "form": dict(form)})
        if action == "init":
            return {"supported_auth_methods": ["client_secret"], "nonce": "v1:n"}
        if action == "begin":
            return dict(BEGIN)
        if action == "poll":
            answer = (
                self.poll_answers.pop(0) if len(self.poll_answers) > 1 else self.poll_answers[0]
            )
            return dict(answer)
        raise AssertionError("意料之外的 action: %s" % action)

    @property
    def polls(self) -> int:
        return sum(1 for call in self.calls if call["action"] == "poll")


class FakeBridge:
    """长连接的替身：只记调用，不真连飞书。"""

    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.running = False
        self.seen: dict[str, str] = {}

    async def start(self, updates):
        self.starts += 1
        self.seen = dict(updates)
        self.running = True

    async def stop(self):
        self.stops += 1
        self.running = False


@pytest.fixture
def feishu(tmp_path):
    return FakeFeishu()


@pytest.fixture
def bridge():
    return FakeBridge()


@pytest.fixture
def settings(rt):
    """不吃仓库里那份真机 `.env.channels`：这些用例必须从「什么都没配」开始。"""

    return Settings(
        database_url=rt.settings.database_url,
        feishu_enabled=False,
        feishu_app_id="",
        feishu_app_secret="",
        feishu_allow_from="",
    )


@pytest.fixture
def client(rt, settings, session, feishu, bridge, tmp_path):
    app = create_app(rt.settings, runtime=rt)
    onboarding = FeishuOnboarding(
        settings,
        transport=feishu,
        env_path=tmp_path / ".env.channels",
        bridge=bridge,
    )
    app.state.feishu_onboarding = onboarding
    return TestClient(app, headers={"Authorization": "Bearer " + session["token"]})


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- 鉴权 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/channels/feishu/status"),
        ("post", "/api/channels/feishu/setup"),
        ("delete", "/api/channels/feishu/setup/setup_missing"),
        ("post", "/api/channels/feishu/disconnect"),
    ],
)
def test_every_onboarding_endpoint_needs_a_web_session(rt, method, path):
    app = create_app(rt.settings, runtime=rt)
    with TestClient(app) as anonymous:
        assert getattr(anonymous, method)(path).status_code == 401


# --- 状态 --------------------------------------------------------------------------------


def test_status_says_not_configured_before_anything_is_scanned(client):
    data = _data(client.get("/api/channels/feishu/status"))

    assert data["configured"] is False
    assert data["running"] is False
    assert data["bound_users"] == 0
    assert data["enabled"] is False


# --- 扫码 --------------------------------------------------------------------------------


def test_setup_hands_back_a_scannable_qr_and_never_leaks_the_device_code(client):
    data = _data(client.post("/api/channels/feishu/setup"))

    assert data["user_code"] == "ABCD-EFGH"
    assert data["link"].endswith("user_code=ABCD-EFGH")
    assert data["qr_svg"].lstrip().startswith("<svg")
    assert data["setup_id"]
    assert data["expires_in"] >= 60
    assert "dc_secret_value" not in json.dumps(data, ensure_ascii=False)


def test_polling_before_the_scan_says_pending(client):
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]

    data = _data(client.get("/api/channels/feishu/setup/%s" % setup_id))

    assert data["state"] == "pending"


def test_unknown_setup_id_is_a_404_not_a_crash(client):
    assert client.get("/api/channels/feishu/setup/setup_missing").status_code == 404


def test_scan_writes_the_credentials_and_brings_the_bridge_up(
    client, settings, feishu, bridge, tmp_path
):
    feishu.poll_answers = [dict(AUTHORIZED)]
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]

    data = _data(client.get("/api/channels/feishu/setup/%s" % setup_id))

    assert data["state"] == "authorized"
    env_text = (tmp_path / ".env.channels").read_text(encoding="utf-8")
    assert "FEISHU_APP_ID=cli_personal" in env_text
    assert "FEISHU_ALLOW_FROM=ou_owner" in env_text
    assert bridge.starts == 1
    # 写盘之后要真的落到运行中的那份 Settings 上，否则长连接起来也读不到凭据
    assert settings.feishu_app_id == "cli_personal"
    assert settings.feishu_enabled is True


def test_a_second_poll_after_authorizing_does_not_start_a_second_bridge(client, feishu, bridge):
    feishu.poll_answers = [dict(AUTHORIZED)]
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]
    _data(client.get("/api/channels/feishu/setup/%s" % setup_id))

    data = _data(client.get("/api/channels/feishu/setup/%s" % setup_id))

    assert data["state"] == "authorized"
    assert bridge.starts == 1


def test_status_after_scanning_reports_the_masked_app_id(client, feishu):
    feishu.poll_answers = [dict(AUTHORIZED)]
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]
    _data(client.get("/api/channels/feishu/setup/%s" % setup_id))

    data = _data(client.get("/api/channels/feishu/status"))

    assert data["configured"] is True
    assert data["running"] is True
    assert data["app_id"] == fs.mask_secret("cli_personal")
    assert "sec_personal_secret_value" not in json.dumps(data, ensure_ascii=False)


def test_cancelling_stops_asking_feishu_about_that_setup(client, feishu):
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]

    data = _data(client.delete("/api/channels/feishu/setup/%s" % setup_id))
    assert data["state"] == "cancelled"

    polls_before = feishu.polls
    assert _data(client.get("/api/channels/feishu/setup/%s" % setup_id))["state"] == "cancelled"
    assert feishu.polls == polls_before


def test_cancelling_twice_is_harmless(client):
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]
    _data(client.delete("/api/channels/feishu/setup/%s" % setup_id))

    data = _data(client.delete("/api/channels/feishu/setup/%s" % setup_id))

    assert data["state"] == "cancelled"


# --- 取消接入 ----------------------------------------------------------------------------


def _seed_binding(rt):
    with rt.database.sessions() as db:
        db.add(
            ChannelBinding(
                channel="feishu",
                platform_user_id="ou_owner",
                platform_username="演示用户",
                user_id="user_0001",
            )
        )
        db.commit()


def test_disconnect_stops_the_bridge_clears_the_credentials_and_unbinds(
    client, settings, rt, feishu, bridge, tmp_path
):
    feishu.poll_answers = [dict(AUTHORIZED)]
    setup_id = _data(client.post("/api/channels/feishu/setup"))["setup_id"]
    _data(client.get("/api/channels/feishu/setup/%s" % setup_id))
    _seed_binding(rt)

    data = _data(client.post("/api/channels/feishu/disconnect"))

    assert data["disconnected"] is True
    assert bridge.stops == 1
    assert bridge.running is False
    env_text = (tmp_path / ".env.channels").read_text(encoding="utf-8")
    assert "FEISHU_" not in env_text
    assert settings.feishu_enabled is False
    assert settings.feishu_app_id == ""
    with rt.database.sessions() as db:
        assert (
            db.execute(select(ChannelBinding).where(ChannelBinding.channel == "feishu")).all() == []
        )


def test_disconnect_when_nothing_is_connected_is_a_no_op(client, bridge):
    data = _data(client.post("/api/channels/feishu/disconnect"))

    assert data["disconnected"] is False
    assert bridge.stops == 0


def test_status_counts_the_bound_users(client, rt):
    _seed_binding(rt)

    assert _data(client.get("/api/channels/feishu/status"))["bound_users"] == 1
