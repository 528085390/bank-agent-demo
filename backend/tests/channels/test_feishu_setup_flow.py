"""飞书扫码接入的产品层模块（设备流 / 二维码 / 凭据落盘）纯单元测试。

这一层不发真实网络请求、不碰数据库：transport 由用例注入，落盘目录用 tmp_path。
真实端到端由 _accept/feishu_setup.py 走服务端轮询的人肉验证通道。
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from app.channels.feishu import setup as fs

BEGIN_PAYLOAD = {
    "device_code": "dc_1",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://open.feishu.cn/page/launcher",
    "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=ABCD-EFGH",
    "expires_in": 3600,
    "interval": 5,
}


def _poster(payload, seen=None):
    def post(url, form, timeout):
        if seen is not None:
            seen.append((url, dict(form), timeout))
        return payload

    return post


# --- 设备流：init ------------------------------------------------------------------------


def test_init_device_flow_reports_the_supported_auth_methods():
    seen: list = []
    info = fs.init_device_flow(
        post=_poster({"supported_auth_methods": ["client_secret"], "nonce": "v1:n"}, seen)
    )
    assert seen[0][0].endswith("/oauth/v1/app/registration")
    assert seen[0][1]["action"] == "init"
    assert "client_secret" in info.auth_methods


def test_init_device_flow_surfaces_a_transport_failure():
    def post(url, form, timeout):
        raise fs.SetupError("NETWORK", "网络连接失败")

    with pytest.raises(fs.SetupError) as caught:
        fs.init_device_flow(post=post)
    assert caught.value.code == "NETWORK"


# --- 设备流：begin -----------------------------------------------------------------------


def test_begin_grant_asks_for_a_personal_agent_and_returns_the_scan_payload():
    seen: list = []
    grant = fs.begin_grant(post=_poster(BEGIN_PAYLOAD, seen))

    assert grant.device_code == "dc_1"
    assert grant.user_code == "ABCD-EFGH"
    assert grant.link == BEGIN_PAYLOAD["verification_uri_complete"]
    assert grant.expires_in == 3600
    assert grant.interval == 5

    url, form, _timeout = seen[0]
    assert url.endswith("/oauth/v1/app/registration")
    assert form["action"] == "begin"
    assert form["archetype"] == "PersonalAgent"
    assert form["auth_method"] == "client_secret"
    # 授权人要 open_id：白名单只有认这个才有意义。
    assert form["request_user_info"] == "open_id"


def test_begin_grant_lifts_a_zero_interval_to_the_floor_so_polling_never_hammers_feishu():
    grant = fs.begin_grant(post=_poster({**BEGIN_PAYLOAD, "interval": 0}))
    assert grant.interval == fs.MIN_POLL_INTERVAL_SECONDS


def test_begin_grant_rejects_an_incomplete_answer_instead_of_handing_back_a_broken_link():
    with pytest.raises(fs.SetupError) as caught:
        fs.begin_grant(post=_poster({"device_code": "dc_1"}))
    assert caught.value.code == "BAD_RESPONSE"


def test_begin_grant_reports_a_transport_failure_as_a_readable_error():
    def post(url, form, timeout):
        raise fs.SetupError("NETWORK", "网络连接失败")

    with pytest.raises(fs.SetupError) as caught:
        fs.begin_grant(post=post)
    assert caught.value.code == "NETWORK"


# --- 设备流：poll ------------------------------------------------------------------------


def test_poll_while_the_user_has_not_scanned_yet_stays_pending():
    outcome = fs.poll_grant("dc_1", post=_poster({"error": "authorization_pending"}))
    assert outcome.state == "pending"
    assert outcome.client_id == ""


def test_poll_slow_down_asks_for_a_longer_interval_instead_of_failing():
    outcome = fs.poll_grant("dc_1", post=_poster({"error": "slow_down"}), interval=5)
    assert outcome.state == "pending"
    assert outcome.interval == 8


def test_poll_after_the_scan_hands_back_the_credentials_and_the_owner_open_id():
    outcome = fs.poll_grant(
        "dc_1",
        post=_poster(
            {
                "client_id": "cli_personal",
                "client_secret": "sec_personal",
                "user_info": {"open_id": "ou_owner"},
            }
        ),
    )
    assert outcome.state == "authorized"
    assert outcome.client_id == "cli_personal"
    assert outcome.client_secret == "sec_personal"
    assert outcome.owner_open_id == "ou_owner"


def test_poll_without_the_owner_identity_is_not_a_usable_binding():
    with pytest.raises(fs.SetupError) as caught:
        fs.poll_grant("dc_1", post=_poster({"client_id": "cli_x", "client_secret": "sec_y"}))
    assert caught.value.code == "BAD_RESPONSE"


@pytest.mark.parametrize(
    ("error", "code"),
    [("access_denied", "DENIED"), ("expired_token", "EXPIRED")],
)
def test_poll_turns_the_two_terminal_answers_into_named_errors(error, code):
    with pytest.raises(fs.SetupError) as caught:
        fs.poll_grant("dc_1", post=_poster({"error": error}))
    assert caught.value.code == code


def test_poll_reports_an_unknown_answer_rather_than_pretending_to_be_pending():
    with pytest.raises(fs.SetupError) as caught:
        fs.poll_grant(
            "dc_1", post=_poster({"error": "invalid_request", "error_description": "坏码"})
        )
    assert caught.value.code == "UPSTREAM"
    assert "坏码" in str(caught.value)


# --- 设备流：飞书把「还没好」放在 HTTP 400 的 body 里 ------------------------------------


def _http_error(body: bytes, status: int = 400) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://accounts.feishu.cn" + fs.REGISTRATION_PATH,
        status,
        "Bad Request",
        {},
        io.BytesIO(body),
    )


def test_poll_reads_a_400_authorization_pending_as_pending_not_as_a_failure(monkeypatch):
    # 真实飞书用 HTTP 400 加 body 里的 error 表示「还没扫码」，别当成接入失败。

    def refuse(request, timeout=None):
        raise _http_error(b'{"error": "authorization_pending"}')

    monkeypatch.setattr(fs.urllib.request, "urlopen", refuse)
    outcome = fs.poll_grant("dc_1")
    assert outcome.state == "pending"


def test_begin_with_a_400_error_body_still_fails_loudly(monkeypatch):
    def refuse(request, timeout=None):
        raise _http_error(b'{"error": "invalid_request", "error_description": "bad app"}')

    monkeypatch.setattr(fs.urllib.request, "urlopen", refuse)
    with pytest.raises(fs.SetupError) as caught:
        fs.begin_grant()
    assert caught.value.code == "UPSTREAM"
    assert "bad app" in str(caught.value)


def test_begin_does_not_treat_a_device_flow_state_error_as_a_grant(monkeypatch):
    def refuse(request, timeout=None):
        raise _http_error(b'{"error": "authorization_pending"}')

    monkeypatch.setattr(fs.urllib.request, "urlopen", refuse)
    with pytest.raises(fs.SetupError) as caught:
        fs.begin_grant()
    assert caught.value.code == "UPSTREAM"


# --- 二维码 ------------------------------------------------------------------------------


def test_render_qr_svg_is_standalone_scriptless_svg():
    svg = fs.render_qr_svg("https://open.feishu.cn/page/launcher?user_code=ABCD-EFGH")
    assert svg.lstrip().startswith("<svg")
    assert "<path" in svg
    assert "<script" not in svg.lower()
    # 不引外部资源：这段 SVG 要直接内联进页面，不能出现 <image>/href
    assert "<image" not in svg.lower()
    assert "href" not in svg.lower()


def test_render_qr_svg_encodes_the_link_itself_not_a_constant():
    one = fs.render_qr_svg("https://open.feishu.cn/page/launcher?user_code=AAAA-AAAA")
    two = fs.render_qr_svg("https://open.feishu.cn/page/launcher?user_code=BBBB-BBBB")
    assert one != two


# --- 凭据落盘 ----------------------------------------------------------------------------


def test_write_env_file_updates_existing_keys_appends_new_ones_and_keeps_a_backup(tmp_path):
    target = tmp_path / ".env.channels"
    target.write_text("FEISHU_ENABLED=false\nFEISHU_APP_ID=old\n", encoding="utf-8")

    fs.write_env_file({"FEISHU_ENABLED": "true", "FEISHU_APP_SECRET": "sec_new"}, target)

    text = target.read_text(encoding="utf-8")
    assert "FEISHU_ENABLED=true" in text
    assert "FEISHU_APP_SECRET=sec_new" in text
    assert text.count("FEISHU_ENABLED=") == 1
    backup = target.with_name(target.name + ".bak")
    assert backup.read_text(encoding="utf-8") == "FEISHU_ENABLED=false\nFEISHU_APP_ID=old\n"


def test_write_env_file_creates_the_file_when_absent(tmp_path):
    target = tmp_path / ".env.channels"
    fs.write_env_file({"FEISHU_ENABLED": "true"}, target)
    assert target.read_text(encoding="utf-8").strip() == "FEISHU_ENABLED=true"


def test_remove_env_keys_drops_only_the_named_lines(tmp_path):
    target = tmp_path / ".env.channels"
    target.write_text(
        "FEISHU_ENABLED=true\nSMTP_HOST=smtp.163.com\nFEISHU_APP_SECRET=sec\n", encoding="utf-8"
    )

    fs.remove_env_keys(["FEISHU_ENABLED", "FEISHU_APP_SECRET"], target)

    text = target.read_text(encoding="utf-8")
    assert "FEISHU_" not in text
    assert "SMTP_HOST=smtp.163.com" in text


def test_remove_env_keys_on_a_missing_file_is_a_no_op(tmp_path):
    fs.remove_env_keys(["FEISHU_ENABLED"], tmp_path / ".env.channels")


def test_env_updates_turn_the_channel_on_and_pin_the_owner_as_the_whitelist():
    updates = fs.env_updates_for("cli_x", "sec_y", "ou_owner")

    assert updates["FEISHU_ENABLED"] == "true"
    assert updates["FEISHU_APP_ID"] == "cli_x"
    assert updates["FEISHU_APP_SECRET"] == "sec_y"
    assert updates["FEISHU_ALLOW_FROM"] == "ou_owner"
    assert updates["FEISHU_WHITELIST_ENABLED"] == "true"
    assert updates["FEISHU_AUTO_BIND"] == "true"
    assert updates["FEISHU_DEFAULT_BOUND_USER"] == "user_0001"


def test_mask_secret_keeps_a_readable_prefix_and_hides_the_rest():
    masked = fs.mask_secret("sec_abcdef123456")
    assert masked.startswith("sec_ab")
    assert masked.endswith("3456")
    assert "cdef12" not in masked
    assert fs.mask_secret("") == ""
