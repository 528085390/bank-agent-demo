"""独立的 MFA 页面：短信验证码页、提交，以及 Web 端的绑定码接口。

飞书的确认卡片里有一个「去手机验证」的按钮，它指向一条**本进程签发**的签名短链：手机浏览器
打开它不需要任何登录态（那条链接本身就是凭据），填验证码、提交，然后由渠道层调用与 Web 端
完全相同的授信状态机。这个文件钉住四件事：

1. 令牌是 HMAC-SHA256 签出来的，被篡改、被换密钥、过期都返回 None，而不是抛异常；
2. 令牌里的 nonce 只能核销一次，所以一条链接不能被两个人先后用掉；
3. 页面永远是中文 HTML —— 链接坏了、过期了、用过了都是能看懂的页面，不是 500 也不是裸 422；
4. 提交走的是 execute_pending_action_decision，因此幂等、点击绑定、验证码规则与 Web 端一字
   不差；同一个幂等键的重复提交拿到回执，不会第二次出账。

验证码只经过内存：它不写进卡片、不写进页面、不写进账本、不写进审计。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.channels import mfa
from app.channels.binding import DEFAULT_CODE_TTL_SECONDS
from app.channels.code_cards import clear_code_cards, remember_code_card
from app.channels.mfa import (
    consume_mfa_token,
    create_mfa_token,
    nonce_already_spent,
    release_mfa_nonce,
    spend_mfa_nonce,
    verify_mfa_token,
)
from app.channels.render.feishu import FeishuCardRenderer
from app.main import create_app
from app.storage.models import ConversationSubmission
from tests.test_conversation_api import messages, new_key
from tests.test_pending_action_api import PENDING_ID, PREVIEW_WITH_CODE, paused

SECRET = "test-secret-key"
# 银行配置的演示验证码（bank_backend 的 mock_mfa_code）。
CODE = "123456"


def phone_for(rt) -> TestClient:
    """手机浏览器：没有 cookie，也没有 X-Session-Id——那条链接自己就是凭据。"""

    return TestClient(create_app(rt.settings, runtime=rt))


def token_for(rt, conversation_id, question_id, *, ttl=300):
    """一条签在应用自己密钥上的链接：手机打开它时，服务端用同一把密钥验证。"""

    return create_mfa_token(
        "user_0001", conversation_id, PENDING_ID, question_id, rt.settings.secret_key, ttl=ttl
    )


def submit_code(phone, token, code=CODE, *, key=None, form_key=None):
    """手机页面的表单提交。幂等键可以走请求头，也可以走表单（浏览器只能走表单）。"""

    headers = {} if key is None else {"Idempotency-Key": key}
    data = {"t": token, "code": code}
    if form_key is not None:
        data["idempotency_key"] = form_key
    return phone.post(
        "/api/channels/mfa/submit",
        data=data,
        headers=headers,
    )


def stored_keys(rt, conversation_id) -> list[str]:
    """这次会话在库里落下的幂等键——直接读 ConversationSubmission，不信页面上的说法。"""

    with rt.database.sessions() as db:
        return [
            row.idempotency_key
            for row in db.scalars(
                select(ConversationSubmission).where(
                    ConversationSubmission.conversation_id == conversation_id
                )
            )
        ]


def signed_payload(secret: str, *, _drop: tuple[str, ...] = (), **overrides) -> str:
    """手签一份载荷：用来造「格式合法、内容不该被接受」的令牌（例如别的版本号）。"""

    payload = {
        "v": 1,
        "uid": "user_0001",
        "cid": "conv_1",
        "pid": PENDING_ID,
        "qid": "q_1",
        "nonce": uuid4().hex,
        "exp": int(time.time()) + 300,
    }
    payload.update(overrides)
    for name in _drop:
        payload.pop(name, None)
    body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    return body + "." + signature.rstrip(b"=").decode("ascii")


def paged(rt, session):
    """一张开着验证码框的卡片（Web 端提问、Web 端点开），手机上再去验证。"""

    return paused(rt, session, preview=PREVIEW_WITH_CODE)


# --- 令牌本身 ----------------------------------------------------------------------------


def test_a_token_round_trips_and_names_the_pending_action():
    token = create_mfa_token("u1", "c1", "p1", "q1", SECRET, ttl=300)

    payload = verify_mfa_token(token, SECRET)

    assert payload is not None
    assert (payload["uid"], payload["cid"], payload["pid"], payload["qid"]) == (
        "u1",
        "c1",
        "p1",
        "q1",
    )
    assert payload["nonce"] and payload["exp"] > 0


def test_a_tampered_or_foreign_token_is_refused_without_raising():
    token = create_mfa_token("u1", "c1", "p1", "q1", SECRET, ttl=300)
    body, signature = token.split(".")
    flipped = ("A" if body[0] != "A" else "B") + body[1:]

    # 改载荷、改签名、换密钥、加尾巴、空串、彻底不是令牌：一律 None，绝不抛异常。
    assert verify_mfa_token(flipped + "." + signature, SECRET) is None
    assert verify_mfa_token(body + "." + signature[:-2] + "xy", SECRET) is None
    assert verify_mfa_token(token, "another-secret") is None
    assert verify_mfa_token(token + "bad", SECRET) is None
    assert verify_mfa_token("", SECRET) is None
    assert verify_mfa_token("not-a-token-at-all", SECRET) is None
    assert verify_mfa_token(None, SECRET) is None


def test_an_expired_token_is_refused():
    expired = create_mfa_token("u1", "c1", "p1", "q1", SECRET, ttl=-1)

    assert verify_mfa_token(expired, SECRET) is None


def test_a_token_whose_payload_is_not_version_one_is_refused():
    """`v` 造了就要校验：换编码时旧链接该被认成「不是这一版的令牌」，而不是被新规则误读。

    三个候选都签名正确、字段齐全、也没过期，唯一的差别是版本号：只有 `v == 1` 那一个能过。
    """

    assert verify_mfa_token(signed_payload(SECRET, v=2), SECRET) is None
    assert verify_mfa_token(signed_payload(SECRET, v="1"), SECRET) is None
    assert verify_mfa_token(signed_payload(SECRET, _drop=("v",)), SECRET) is None
    # 同一份载荷，版本号对得上就是有效令牌——差别只在版本这一项，不是签名或字段。
    assert verify_mfa_token(signed_payload(SECRET, v=1), SECRET) is not None


def test_a_nonce_can_be_spent_only_once():
    token = create_mfa_token("u1", "c1", "p1", "q1", SECRET, ttl=300)

    first = consume_mfa_token(token, SECRET)
    second = consume_mfa_token(token, SECRET)

    assert first is not None and first["pid"] == "p1"
    # 同一条链接不能被第二个人核销：重放拿到 None，而不是又一次授权。
    assert second is None


def test_a_full_nonce_table_refuses_instead_of_forgetting_a_live_link():
    """表满时 fail-closed：宁可拒绝这一次，也不挤掉一枚可能还有效的 nonce。

    旧的「淘汰最早到期的一条」是 fail-open：并存的那几千枚里被淘汰的那一枚完全可能还没过期，
    重放防线就在最需要它的时候静默消失。容量在这里注入成「当前条数 + 2」，所以这条用例不
    依赖别处留下了多少记号，也不会给别处的用例留下记号。
    """

    budget = 2
    capacity = len(mfa._spent) + budget
    tracked: list[str] = []
    try:
        for _ in range(budget):
            nonce = "fill-" + uuid4().hex
            assert spend_mfa_nonce(nonce, time.time() + 300, max_nonces=capacity) is True
            tracked.append(nonce)

        # 表已满：这一次直接失败（调用方把它渲染成「这笔链接已经用过/处理过」）。
        refused = "fill-" + uuid4().hex
        assert spend_mfa_nonce(refused, time.time() + 300, max_nonces=capacity) is False
        # 防线一点没松：最早那枚仍在册，被拒的那枚没有趁机混进去。
        assert nonce_already_spent(tracked[0]) is True
        assert nonce_already_spent(refused) is False
    finally:
        for nonce in tracked:
            release_mfa_nonce(nonce)


def test_a_full_nonce_table_frees_itself_by_dropping_only_expired_entries():
    """满表里那些**确实过期**的先被清掉，位置就让给新链接——腾地方从不靠淘汰有效项。"""

    capacity = len(mfa._spent) + 1
    stale = "stale-" + uuid4().hex
    fresh = "fresh-" + uuid4().hex
    assert spend_mfa_nonce(stale, time.time() - 1, max_nonces=capacity) is True
    try:
        # 上一枚已经过期：清理之后表不满了，这次占用成功（要是清理被拿掉，这里就是 False）。
        assert spend_mfa_nonce(fresh, time.time() + 300, max_nonces=capacity) is True
        # 过期项是真的被清掉了，而不是被挪到别处继续算数。
        assert nonce_already_spent(stale) is False
    finally:
        release_mfa_nonce(fresh)


# --- GET /api/channels/mfa ---------------------------------------------------------------


def test_the_phone_page_asks_for_the_sms_code_and_carries_no_internal_ids(rt, session):
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])

    response = phone_for(rt).get("/api/channels/mfa", params={"t": token})

    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "短信验证码" in response.text
    # 页面上只有一枚不透明的令牌：用户 id、会话/对话/问题/待办 id 一个都不露。
    assert 'action="/api/channels/mfa/submit"' in response.text
    assert 'name="code"' in response.text
    assert token in response.text
    for internal in (
        "user_0001",
        session["session_id"],
        conversation["conversation_id"],
        question["question_id"],
        PENDING_ID,
    ):
        assert internal not in response.text, internal


def test_every_page_refuses_to_be_cached_or_to_leak_the_link(rt, session):
    """URL 与隐藏字段里都是凭据，所以每一张页面都必须带硬化头——错误页也不例外。

    少了 `no-store`，一条带令牌的页面会被手机浏览器或中间的共享缓存留下来；少了
    `no-referrer`，用户从这页点出去时令牌会挂在 `Referer` 上发给第三方。
    """

    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    phone = phone_for(rt)

    form = phone.get("/api/channels/mfa", params={"t": token})
    submitted = submit_code(phone, token)
    refused = phone.get("/api/channels/mfa", params={"t": "forged.token"})

    assert form.status_code == 200 and submitted.status_code == 200, submitted.text
    assert refused.status_code == 400
    for response in (form, submitted, refused):
        assert "text/html" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_an_unreadable_link_is_a_chinese_page_not_a_500(rt):
    response = phone_for(rt).get("/api/channels/mfa", params={"t": "forged.token"})

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert "失效" in response.text
    assert "request_id" not in response.text


def test_a_missing_link_is_the_same_friendly_page(rt):
    response = phone_for(rt).get("/api/channels/mfa")

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]


def test_an_expired_link_is_refused_with_a_friendly_page(rt, session):
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"], ttl=-1)

    response = phone_for(rt).get("/api/channels/mfa", params={"t": token})

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert "失效" in response.text


def test_a_used_link_is_refused_with_a_friendly_page(rt, session):
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    assert consume_mfa_token(token, rt.settings.secret_key) is not None

    response = phone_for(rt).get("/api/channels/mfa", params={"t": token})

    assert response.status_code == 410
    assert "text/html" in response.headers["content-type"]
    assert "已经用过了" in response.text


# --- POST /api/channels/mfa/submit -------------------------------------------------------


def test_the_submission_confirms_the_card_from_the_phone(rt, session):
    client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    before = len(messages(client, conversation["conversation_id"])["items"])

    response = submit_code(phone_for(rt), token)

    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "操作已提交" in response.text
    # 渠道层调的是同一个授信状态机：模块收到的是结构化动作，不是一句话。
    assert [(item.kind, item.pending_id) for item in spy.actions] == [("confirm", PENDING_ID)]
    ledger = messages(client, conversation["conversation_id"])["items"]
    assert len(ledger) == before + 2
    assert ledger[-2]["message_kind"] == "interrupt_answer"
    assert ledger[-2]["content"] == "确认本次操作"
    # 卡片被消费：再打开会话已经没有什么在等确认。
    detail = client.get("/api/conversations/" + conversation["conversation_id"]).json()["data"]
    assert detail["pending_interrupt"] is None


def test_a_successful_submission_hands_the_session_lock_back(rt, session):
    """提交成功后会话锁必须放开：泄漏的锁会把这条会话焊死到进程结束。

    渠道这条路是**唯一**把 `held_lock` 接到 `_run_turn` 的 `finally` 上的调用方，Web 端的
    释放发生在另一个生成器里——所以这条只能在这里测。限时抢锁：锁没放开就是超时timeout（红），
    而不是把整个测试套件挂死。
    """

    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    conversation_id = conversation["conversation_id"]
    token = token_for(rt, conversation_id, question["question_id"])

    response = submit_code(phone_for(rt), token)

    assert response.status_code == 200, response.text
    assert "操作已提交" in response.text
    # 锁的持有者就是这一轮自己：流结束了（页面都渲染完了）它就该回到空闲。
    assert rt.agent.conversation_lock(conversation_id).locked() is False

    async def take_it_again():
        again = await asyncio.wait_for(rt.turns.acquire(conversation_id), timeout=1.0)
        again.release()

    asyncio.run(take_it_again())


def test_the_typed_code_never_reaches_the_page_the_ledger_or_the_audit(rt, session):
    client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])

    response = submit_code(phone_for(rt), token)

    assert response.status_code == 200, response.text
    # 验证码交到了模块手上（它要送进银行），但停在模块里：页面、账本、审计都查不到它。
    assert [item.verification_code for item in spy.actions] == [CODE]
    assert CODE not in response.text
    assert CODE not in json.dumps(messages(client, conversation["conversation_id"]))
    assert CODE not in client.get("/api/audit/run-events").text


def test_a_wrong_code_is_refused_and_writes_nothing(rt, session):
    client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    before = len(messages(client, conversation["conversation_id"])["items"])

    response = submit_code(phone_for(rt), token, code="")

    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]
    assert "验证码" in response.text
    # 一个不可能成立的提交不写任何东西：模块没有动作，账本没有新行，卡片还开着。
    assert spy.actions == []
    assert len(messages(client, conversation["conversation_id"])["items"]) == before
    detail = client.get("/api/conversations/" + conversation["conversation_id"]).json()["data"]
    assert detail["pending_interrupt"]["confirmation"]["requires_code"] is True


def test_a_repeat_submission_gets_the_receipt_instead_of_a_second_write(rt, session):
    client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    key = new_key()
    phone = phone_for(rt)

    first = submit_code(phone, token, key=key)
    second = submit_code(phone, token, key=key)

    assert first.status_code == 200, first.text
    assert "操作已提交" in first.text
    assert second.status_code == 200, second.text
    assert "已经处理过" in second.text
    # 同一个键的重放是同一笔提交：模块只被调用一次，账本里只有一条回答。
    assert [item.pending_id for item in spy.actions] == [PENDING_ID]
    answers = [
        item
        for item in messages(client, conversation["conversation_id"])["items"]
        if item["message_kind"] == "interrupt_answer"
    ]
    assert len(answers) == 1


def test_a_repeat_without_a_header_key_still_does_not_write_twice(rt, session):
    """浏览器发不出请求头，所以表单的幂等键由服务端从令牌派生：重复提交同样是重放。"""

    client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    phone = phone_for(rt)

    first = submit_code(phone, token)
    second = submit_code(phone, token)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert "已经处理过" in second.text
    assert len(spy.actions) == 1


def test_the_submission_key_is_the_header_then_the_form_then_the_nonce(rt, session):
    """幂等键的取法要钉在库里那一行上：头 → 表单 → 由 nonce 派生。

    三档都钉住，是因为它是去重的**唯一**身份：取错一档，重复提交要么被当成新请求（写两次），
    要么两条不同的提交被当成同一条（第二笔悄悄丢掉）。会话里本来就有发起这次确认时留下的行，
    所以只看这次提交**新增**的那一行。
    """

    phone = phone_for(rt)

    def submitted_with(token: str, **kwargs) -> str:
        """提交一次，返回这次提交写进库里的幂等键。"""

        before = stored_keys(rt, conversation_id)
        assert submit_code(phone, token, **kwargs).status_code == 200
        added = [key for key in stored_keys(rt, conversation_id) if key not in before]
        assert len(added) == 1, (before, added)
        return added[0]

    # 1) 什么都没有：按令牌 nonce 派生。
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    conversation_id = conversation["conversation_id"]
    token = token_for(rt, conversation_id, question["question_id"])
    nonce = verify_mfa_token(token, rt.settings.secret_key)["nonce"]
    assert submitted_with(token) == "mfa-" + nonce

    # 2) 只有表单字段：用表单值（纯 HTML 页面能做的极限）。
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    conversation_id = conversation["conversation_id"]
    token = token_for(rt, conversation_id, question["question_id"])
    form_key = "form-" + uuid4().hex
    assert submitted_with(token, form_key=form_key) == form_key

    # 3) 头与表单都在：请求头优先，表单那一份不生效。
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    conversation_id = conversation["conversation_id"]
    token = token_for(rt, conversation_id, question["question_id"])
    header_key = "header-" + uuid4().hex
    assert submitted_with(token, key=header_key, form_key="form-" + uuid4().hex) == header_key


def test_a_replayed_link_cannot_buy_a_second_write_even_with_a_fresh_key(rt, session):
    """用完的链接换一个新键重放：nonce 已经核销，渠道层在写任何东西之前就收口。"""

    client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    phone = phone_for(rt)

    assert submit_code(phone, token, key=new_key()).status_code == 200
    replay = submit_code(phone, token, key=new_key())

    assert replay.status_code == 200
    assert "text/html" in replay.headers["content-type"]
    assert "已经处理过" in replay.text
    # 模块只收到一次动作，账本里也只有一条回答：第二次提交没有换成第二次出账。
    assert len(spy.actions) == 1
    answers = [
        item
        for item in messages(client, conversation["conversation_id"])["items"]
        if item["message_kind"] == "interrupt_answer"
    ]
    assert len(answers) == 1


def test_a_link_survives_a_wrong_code(rt, session):
    """填错一次验证码不等于链接作废：占用被放回去，同一条链接重试仍然有效。"""

    _client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"])
    phone = phone_for(rt)

    rejected = submit_code(phone, token, code="")
    accepted = submit_code(phone, token)

    assert rejected.status_code == 422
    assert accepted.status_code == 200, accepted.text
    assert "操作已提交" in accepted.text
    assert len(spy.actions) == 1


def test_an_expired_link_cannot_submit(rt, session):
    _client, conversation, question, spy, _rebuild = paged(rt, session)
    token = token_for(rt, conversation["conversation_id"], question["question_id"], ttl=-1)

    response = submit_code(phone_for(rt), token)

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert spy.actions == []


def test_a_token_cannot_confirm_a_conversation_that_is_not_the_callers(rt, session):
    """令牌指向的会话不属于这个人时，状态机的「这不是你的会话」先说话（404）。"""

    _client, _conversation, question, spy, _rebuild = paged(rt, session)
    token = create_mfa_token(
        "user_0001",
        "conv_does_not_exist",
        PENDING_ID,
        question["question_id"],
        rt.settings.secret_key,
        ttl=300,
    )

    response = submit_code(phone_for(rt), token)

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert spy.actions == []


def test_an_unknown_user_gets_a_chinese_page_and_keeps_the_link_alive(rt, session):
    """令牌指向一个不存在的用户：中文错误页，而且这枚 nonce 没被白烧。

    会话换发放在 `try` 之外时，这条路径是唯一一条既不放回 nonce、也不渲染页面的分支：手机
    浏览器拿到 404 的裸 JSON，之后同一条链接再打开是 410、再提交是「已经处理过」——链接被
    永久作废，用户手里那张卡片也就白点了。
    """

    _client, conversation, question, spy, _rebuild = paged(rt, session)
    phone = phone_for(rt)
    ghost = create_mfa_token(
        "user_does_not_exist",
        conversation["conversation_id"],
        PENDING_ID,
        question["question_id"],
        rt.settings.secret_key,
        ttl=300,
    )

    refused = submit_code(phone, ghost)

    # 中文页面，不是 JSON 信封；状态码仍是状态机的那个 404。
    assert refused.status_code == 404
    # 判定"不是 JSON 信封"看的是响应的形状：文案里本来就有 class="error"，不能拿字符串搜。
    assert refused.headers["content-type"].startswith("text/html")
    assert refused.text.lstrip().startswith("<!DOCTYPE")
    assert "用户不存在" in refused.text
    assert spy.actions == []

    # nonce 被放回去了：链接还能打开（不是 410），也还能再提交一次（不是「已经处理过」）。
    reopened = phone.get("/api/channels/mfa", params={"t": ghost})
    assert reopened.status_code == 200, reopened.text
    assert "短信验证码" in reopened.text
    retried = submit_code(phone, ghost)
    assert retried.status_code == 404
    assert "已经处理过" not in retried.text
    assert spy.actions == []


# --- 卡片的结局：锁归还与页面渲染 ---------------------------------------------------------


def test_a_receipt_without_a_held_lock_still_renders_the_turn():
    """回执 / 拒绝那几条路径不为这一轮拿锁，held_lock 就是 None。

    对它 release 是 AttributeError，而那句话在 finally 里：异常会盖掉本该返回的页面，用户拿到
    一个裸 500，而他这一笔其实已经办完了（回执）。
    """

    from types import SimpleNamespace

    from app.api.channels import _run_turn

    class StubTurns:
        def stream(self, user_id, session_id, plan, *, naming=None):
            del user_id, session_id, plan, naming

            async def events():
                yield SimpleNamespace(event="reply_delta", data={"text": "半句"})
                yield SimpleNamespace(event="final", data={"text": "已经办完了"})

            return events()

    result = SimpleNamespace(outcome=object(), naming=None, status="receipt", held_lock=None)
    rt_stub = SimpleNamespace(turns=StubTurns())

    text = asyncio.run(_run_turn(rt_stub, {"user_id": "u", "session_id": "s"}, result))

    assert text == "已经办完了"


# --- POST /api/channels/binding-code -----------------------------------------------------


def test_the_binding_code_is_a_six_digit_one_time_code_for_the_logged_in_user(rt, session, caplog):
    client = TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )

    with caplog.at_level("DEBUG"):
        response = client.post("/api/channels/binding-code")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["code"]) == 6 and data["code"].isdigit()
    assert data["expires_in"] == DEFAULT_CODE_TTL_SECONDS
    # 绑定码的明文只出现一次：它不进日志。
    assert data["code"] not in caplog.text
    # 它真能核销：用的是这个应用自己那一个 ChannelBindingManager。
    binding = client.app.state.channel_bindings.redeem_code(
        data["code"], "feishu", "ou_binding_code_test"
    )
    assert binding is not None and binding.user_id == "user_0001"


def test_the_binding_code_requires_a_session(rt):
    anonymous = TestClient(create_app(rt.settings, runtime=rt))

    response = anonymous.post("/api/channels/binding-code")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_REQUIRED"


# --- 既有接缝 ----------------------------------------------------------------------------


def test_the_decide_route_keeps_the_names_callers_reach_through_it():
    """R3：app.api.routes 仍然导出这三个名字——渠道层没有把它们搬走。"""

    from app.api.routes import (  # noqa: F401
        _authorize_card_action_if_needed,
        _click_matches,
        _requires_code,
    )


# --- 手机页面的结论要回到聊天卡片上 ------------------------------------------------------


class ChatChannel:
    """替身通道：像 dispatcher 一样返回消息 id，并记下每次原地更新打的是哪一条。"""

    def __init__(self) -> None:
        self.payloads: list = []
        self.updates: list[str] = []

    async def __call__(self, payload, *, update_message_id=None):
        self.payloads.append(payload)
        if update_message_id is not None:
            self.updates.append(update_message_id)
        return "om_" + str(len(self.payloads))

    def dumped(self) -> str:
        return json.dumps(self.payloads[-1], ensure_ascii=False)


def test_the_phone_submission_updates_the_chat_card(rt, session):
    """手机上办完之后，聊天里那条「等待您的确认」要变成结论——否则用户以为还没提交。"""

    clear_code_cards()
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    conversation_id = conversation["conversation_id"]
    token = token_for(rt, conversation_id, question["question_id"])
    channel = ChatChannel()
    assert remember_code_card(
        conversation_id=conversation_id,
        pending_id=PENDING_ID,
        send_fn=channel,
        message_id="om_phone_card",
        renderer=FeishuCardRenderer(),
        title="银行智能助手",
    )

    response = submit_code(phone_for(rt), token)

    assert response.status_code == 200, response.text
    # 更新的是那条卡本身，不是另起一条：手机上翻回去看到的就是结论。
    assert channel.updates == ["om_phone_card"]
    dumped = channel.dumped()
    assert "已完成" in dumped
    assert "正在生成" not in dumped
    clear_code_cards()


def test_a_second_submission_does_not_touch_the_card_again(rt, session):
    """同一条链接再提交一次只落回执：卡上已经写好的结论不被改第二次。"""

    clear_code_cards()
    _client, conversation, question, _spy, _rebuild = paged(rt, session)
    conversation_id = conversation["conversation_id"]
    token = token_for(rt, conversation_id, question["question_id"])
    channel = ChatChannel()
    assert remember_code_card(
        conversation_id=conversation_id,
        pending_id=PENDING_ID,
        send_fn=channel,
        message_id="om_phone_card",
        renderer=FeishuCardRenderer(),
        title="银行智能助手",
    )

    first = submit_code(phone_for(rt), token, key="mfa-once")
    second = submit_code(phone_for(rt), token, key="mfa-once")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    # 写回只有一次：第二次是回执，卡片已经说完了，不该被再改一遍。
    assert channel.updates == ["om_phone_card"]
    clear_code_cards()
