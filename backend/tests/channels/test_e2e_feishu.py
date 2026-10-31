"""飞书桥端到端用例（TC-FS-01 .. TC-FS-08）：从飞书事件进站到记账、卡片、锁的整条链路。

这份文件是 Task 9 的验收面，不是某一层的单测：它把真实的渠道引擎、绑定管理器、防重、
渲染器、dispatcher 与真实的 rt（独立 schema + alembic + seed）接在一起，只在真正要出网的
两处换成替身——飞书客户端（假 client_factory / 注入 send_fn）与银行
（tests/support/domain_harness.FakeBankClient）。

每条用例的 docstring 就是验收口径；用例之间不共享可变状态（rt 每条一份新 schema，绑定、
会话、模型脚本都在用例内部自建），可以单独跑，也可以串行整跑。

不用真实凭据、不联网：send_fn 一律注入，长连接一律用假 client，银行一律用假后端。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.channels.binding import ChannelBindingManager
from app.channels.contracts import CardActionClick, InboundMessage
from app.channels.dedup import ChannelDedup
from app.channels.engine import (
    BIND_FAILED_TEXT,
    BIND_OK_TEXT,
    DUPLICATE_CLICK_TEXT,
    INTERRUPTED_TEXT,
    UNAUTHORIZED_TEXT,
    ChannelEngine,
)
from app.channels.feishu import runner as runner_module
from app.channels.feishu.dispatcher import FeishuDispatcher
from app.channels.feishu.runner import FeishuRunner
from app.channels.mfa import verify_mfa_token
from app.channels.render.feishu import MAX_TEXT_CHARS
from app.channels.throttler import StreamThrottler
from app.core.config import Settings
from app.main import create_app
from app.modules.registry import build_registry
from app.runtime import agent_graph_factory, build_tool_gateway
from app.storage.models import ChannelBinding
from tests.support.domain_harness import FakeBankClient
from tests.support.scripted_model import ScriptedModel, StreamingModel

#: seed() 在每个测试 schema 里写下的演示用户。
USER = "user_0001"
OPEN_ID = "ou_e2e_alice"
OTHER_OPEN_ID = "ou_e2e_mallory"
SECOND_OPEN_ID = "ou_e2e_bob"
THIRD_OPEN_ID = "ou_e2e_carol"
CHAT_ID = "oc_e2e_chat"
CLICK_REPLY = "已按您的确认提交。"
FINAL_ANSWER = "您的活期账户余额为 8,320.00 元。"

# 假的银行：一个活期账户、一个收款人「李四」、POST /transfers 记一笔回执。
ACCOUNTS = [
    {
        "account_id": "a_zhang_current",
        "user_id": "u_zhang",
        "account_type": "current",
        "balance_minor": 832000,
        "available_minor": 800000,
        "currency": "CNY",
        "interest_rate": 0.0,
        "status": "active",
    }
]
CONTACTS = [
    {
        "contact_id": "c_lisi",
        "user_id": "u_zhang",
        "name": "李四",
        "phone": "13800138000",
        "remark": None,
        "recipient_user_id": "u_partner",
        "recipient_account_id": "a_partner_current",
        "birthday": None,
    }
]
HANDOFF = ("call", "handoff", '{"agent_id":"transfer","instruction":"办理转账"}')


# --- 替身与脚手架 -------------------------------------------------------------------


class Clock:
    """可拨的单调时钟：绑定码 TTL 与防爆破锁定都读它。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Outbox:
    """替身外发通道：记下每条 payload，并像真 dispatcher 那样返回一个消息 id。"""

    def __init__(self) -> None:
        self.payloads: list = []
        self.updates: list[str] = []

    async def __call__(self, payload, *, update_message_id=None):
        self.payloads.append(payload)
        if update_message_id is not None:
            self.updates.append(update_message_id)
            # 真飞书更新哪条就回那条的 id：原地刷新因此永远落在同一条消息上。
            return update_message_id
        return "om_%d" % len(self.payloads)

    def texts(self) -> list[str]:
        return [item for item in self.payloads if isinstance(item, str)]

    def cards(self) -> list[dict]:
        return [item for item in self.payloads if isinstance(item, dict)]

    def dump(self) -> str:
        return json.dumps(self.payloads, ensure_ascii=False, default=str)


class ExplodingOutbox:
    """一条都发不出去的通道：飞书抖了、凭据过期了，或者网络超时。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error if error is not None else RuntimeError("feishu is down")
        self.attempts = 0

    async def __call__(self, payload, *, update_message_id=None):
        del payload, update_message_id
        self.attempts += 1
        raise self.error


class EngineSpy:
    """只记录「被调用了几次、参数是什么」的引擎替身（判「引擎根本没被调用」用）。"""

    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []
        self.clicks: list[CardActionClick] = []

    async def handle_message(self, msg, send_fn):
        del send_fn
        self.messages.append(msg)

    async def handle_card_action(self, click, send_fn):
        del send_fn
        self.clicks.append(click)
        return {"body": {"elements": []}}


class RaisingEngine:
    """一个处理时抛异常的引擎：异常绝不能冒回飞书回调。"""

    async def handle_message(self, msg, send_fn):
        del msg, send_fn
        raise RuntimeError("the engine exploded")

    async def handle_card_action(self, click, send_fn):
        del click, send_fn
        raise RuntimeError("the engine exploded")


class TransferBank(FakeBankClient):
    """银行替身：账户、联系人、一次转账回执；每一笔 POST /transfers 都留在 requests 里。"""

    def __init__(self, *, contacts=None) -> None:
        super().__init__()
        self.register("/accounts", ACCOUNTS)
        self.register("/contacts", CONTACTS if contacts is None else contacts)
        self.register("/transfers", {"transaction_id": "t_e2e", "status": "completed"})

    def writes(self) -> list[dict]:
        """真正出账的那几次调用（其余都是只读查询）。"""

        return [
            item
            for item in self.requests
            if item["method"] == "POST" and item["path"] == "/transfers"
        ]


def settings_for(rt, **overrides) -> Settings:
    """渠道用的真实 Settings：白名单只放行 OPEN_ID，显式绑定走 /bind，不自动绑定。

    secret_key 与 channel_mfa_base_url 保留默认值：MFA 短链的验签用的是端点那一侧
    （rt.settings）的密钥，两处必须同一把，否则「卡片上的链接打不开」。
    """

    body = {
        "database_url": rt.settings.database_url,
        "feishu_allow_from": OPEN_ID,
        "feishu_whitelist_enabled": True,
        "feishu_auto_bind": False,
        "feishu_app_id": "cli_e2e",
        "feishu_app_secret": "e2e-app-secret",
    }
    body.update(overrides)
    return Settings(**body)


def channel(rt, *, settings=None, clock=None, boot_time=None, throttler_factory=None):
    """一台真引擎：真绑定管理器 + 真防重（水位线默认在一小时前，事件时间算「新」）。"""

    settings = settings or settings_for(rt)
    mgr = ChannelBindingManager(rt.database.sessions, settings=settings, clock=clock or Clock())
    dedup = ChannelDedup(boot_time=time.time() - 3600.0 if boot_time is None else boot_time)
    engine = ChannelEngine(
        rt=rt,
        settings=settings,
        binding_mgr=mgr,
        dedup=dedup,
        throttler_factory=throttler_factory,
    )
    return engine, mgr, dedup


def eager_throttler() -> StreamThrottler:
    """两道闸都拆掉的节流器：要验的是每次刷新都原地更新同一条消息。"""

    return StreamThrottler(interval_ms=0, min_delta_chars=1, max_text_chars=MAX_TEXT_CHARS)


def install_transfer_agent(rt, bank, model) -> None:
    """把真实转账模块接上假银行：生产注册表 + 真 graph_builder + 脚本化模型。"""

    registry = build_registry()
    gateway = build_tool_gateway(registry, client=bank, audit=rt.audit)
    rt.bank_client = bank
    rt.agent = AgentRuntime(
        model,
        graph_factory=agent_graph_factory(gateway, client=bank),
        checkpointer=InMemorySaver(),
        customer_resolver=rt.identity.bank_customer_id,
    )


def prepare_call(amount: str, *, to_name: str = "李四") -> tuple:
    payload = {"amount_yuan": amount, "to_name": to_name, "from_hint": "活期"}
    return ("call", "transfer_prepare", json.dumps(payload, ensure_ascii=False))


def transfer_script(*, amount: str = "500.00", extra: tuple = ()) -> ScriptedModel:
    """一轮转账的脚本：Supervisor 派给转账域 -> 解析意图 -> （点击那一轮的收尾语）。"""

    return ScriptedModel([HANDOFF, prepare_call(amount), *extra])


def bind(mgr: ChannelBindingManager, open_id: str = OPEN_ID, name: str = "Alice") -> None:
    """显式绑定：拿一枚真码核销，跟用户在飞书里发 /bind 是同一段代码。"""

    code = mgr.generate_binding_code(USER)
    assert mgr.redeem_code(code, "feishu", open_id, name) is not None


def conversation_of(mgr: ChannelBindingManager, open_id: str = OPEN_ID) -> str:
    binding = mgr.get_binding("feishu", open_id)
    assert binding is not None
    return binding.conversation_id


def ledger(rt, conversation_id: str) -> list[dict]:
    return rt.conversations.messages(USER, conversation_id)["items"]


def inbound(
    text: str,
    *,
    message_id: str | None = None,
    event_id: str | None = None,
    sender_id: str = OPEN_ID,
) -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        sender_id=sender_id,
        sender_name="Alice",
        message_id=message_id or "m-%s" % uuid4().hex,
        event_id=event_id or "e-%s" % uuid4().hex,
        text=text,
        create_time_ms=int(time.time() * 1000),
    )


def click(
    *,
    question_id: str,
    pending_id: str,
    decision: str = "confirm",
    sender_id: str = OPEN_ID,
    event_id: str | None = None,
) -> CardActionClick:
    return CardActionClick(
        channel="feishu",
        sender_id=sender_id,
        action_id="act-%s" % uuid4().hex,
        question_id=question_id,
        pending_id=pending_id,
        decision=decision,
        raw_value={"q": question_id, "p": pending_id, "d": decision},
        event_id=event_id or "ev-%s" % uuid4().hex,
        create_time_ms=int(time.time() * 1000),
    )


def message_event(
    *,
    text: str = "给我查一下余额",
    open_id: str = OPEN_ID,
    message_id: str = "om_e2e_1",
    event_id: str = "ev_e2e_1",
    create_time_ms: int | None = None,
    message_type: str = "text",
    content: str | None = None,
) -> dict:
    """一条飞书 im.message.receive_v1 事件（字段形状同 Task 8 的假设）。"""

    stamp = str(int(time.time() * 1000) if create_time_ms is None else create_time_ms)
    header = {"event_id": event_id, "event_type": "im.message.receive_v1", "create_time": stamp}
    return {
        "schema": "2.0",
        "header": header,
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {
                "message_id": message_id,
                "create_time": stamp,
                "chat_id": CHAT_ID,
                "chat_type": "p2p",
                "message_type": message_type,
                "content": (
                    json.dumps({"text": text}, ensure_ascii=False) if content is None else content
                ),
            },
        },
    }


def card_action_event(
    value: dict,
    *,
    open_id: str = OPEN_ID,
    event_id: str | None = None,
    create_time_ms: int | None = None,
) -> dict:
    """一次 card.action.trigger 回调：按钮回传值原样放在 event.action.value 里。"""

    stamp = str(int(time.time() * 1000) if create_time_ms is None else create_time_ms)
    header = {
        "event_id": event_id or "ev_click_%s" % uuid4().hex,
        "event_type": "card.action.trigger",
        "create_time": stamp,
    }
    return {
        "schema": "2.0",
        "header": header,
        "event": {
            "operator": {"open_id": open_id},
            "context": {"open_message_id": "om_card_1", "open_chat_id": CHAT_ID},
            "action": {"value": value, "tag": "button"},
        },
    }


def button_values(card: dict) -> list[dict]:
    """卡片上所有带回调回传值的按钮（behaviors[type=callback].value）。"""

    return [
        element["behaviors"][0]["value"]
        for element in card["body"]["elements"]
        if element.get("tag") == "button" and element["behaviors"][0]["type"] == "callback"
    ]


def url_buttons(card: dict) -> list[str]:
    """卡片上所有跳转按钮的地址（MFA 短链走这个）。"""

    return [
        element["behaviors"][0]["default_url"]
        for element in card["body"]["elements"]
        if element.get("tag") == "button" and element["behaviors"][0]["type"] == "open_url"
    ]


def dump(card) -> str:
    return json.dumps(card, ensure_ascii=False, default=str)


# --- TC-FS-01 飞书长连就绪 --------------------------------------------------------------


class RecordingWsClient:
    """假的长连接客户端：start() 阻塞到 stop()，和真 lark ws Client 一样。

    它只会被 start / stop：长连接是出站的主动连接，没有任何一处要求它监听端口，所以
    「有没有服务端 socket」在这份替身上是可观测的（记录表里只可能出现 start/stop）。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started = threading.Event()
        self.stop_requested = threading.Event()

    def start(self) -> None:
        self.calls.append("start")
        self.started.set()
        self.stop_requested.wait(timeout=10)

    def stop(self) -> None:
        self.calls.append("stop")
        self.stop_requested.set()


async def test_tc_fs_01_long_connection_ready_without_any_listening_port(rt):
    """TC-FS-01 飞书长连就绪。

    验收口径：起长连不阻塞事件循环、is_running 为真、describe() 可读；全程没有任何监听
    端口（长连接只是出站 ws，runner 里不存在监听 / 建服务器的代码路径）；stop() 之后
    is_running 为假，且可以重新启动。
    """

    settings = settings_for(rt)
    clients: list[RecordingWsClient] = []

    def factory(dispatcher, settings_):
        del dispatcher, settings_
        client = RecordingWsClient()
        clients.append(client)
        return client

    runner = FeishuRunner(EngineSpy(), settings, client_factory=factory)

    assert await runner.start() is True
    assert runner.is_running is True
    description = runner.describe()
    assert "feishu_runner" in description
    assert "cli_e2e" in description
    assert "running=True" in description
    # start() 立刻返回、连接在工作线程里：事件循环一次都没被它卡住。
    assert await asyncio.wait_for(asyncio.to_thread(clients[0].started.wait, 5), 10) is True

    # 结构性断言：这个模块里没有任何「监听端口 / 起服务器」的代码路径，只用出站客户端。
    source = inspect.getsource(runner_module)
    for banned in ("start_server", "create_server", "listen(", "bind((", ".bind(", "HTTPServer"):
        assert banned not in source, banned
    # 与代码路径一致的可观测事实：客户端只被要求 start/stop，没有任何 bind/listen 调用。
    assert clients[0].calls == ["start"]

    await runner.stop()
    assert runner.is_running is False
    assert clients[0].calls == ["start", "stop"]
    assert "running=False" in runner.describe()

    await runner.stop()  # 幂等

    # 停掉之后可以再起：换一个新客户端，is_running 重新为真。
    assert await runner.start() is True
    assert runner.is_running is True
    assert len(clients) == 2
    await runner.stop()
    assert runner.is_running is False


class StuckWsClient:
    """假客户端：start() 永不返回，也没有 stop()/_disconnect()。

    真实的 lark ws Client 在断线重连时可以永远不返回；这条用例钉住的是那种客户端
    不能把停机流程（以及 TestClient 的 portal）一起拖死。
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.current_thread()
        self.started.set()
        threading.Event().wait()


async def test_a_client_that_never_returns_cannot_block_shutdown(rt, monkeypatch):
    """lark 的 start() 可能永不返回：工作线程必须是 daemon，stop() 必须能收场。"""

    monkeypatch.setattr(runner_module, "STOP_TIMEOUT_SEC", 0.5)
    settings = settings_for(rt)
    client = StuckWsClient()
    runner = FeishuRunner(EngineSpy(), settings, client_factory=lambda *_: client)

    assert await runner.start() is True
    assert await asyncio.wait_for(asyncio.to_thread(client.started.wait, 5), 10) is True
    assert client.thread is not None
    assert client.thread.daemon is True, "非 daemon 线程会让事件循环关不掉"
    assert client.thread.name == "feishu-ws-client"

    await asyncio.wait_for(runner.stop(), 25)
    assert runner.is_running is False


async def test_the_loop_can_close_even_if_the_client_stays_stuck(rt, monkeypatch):
    """整段生命周期放进 asyncio.run：默认执行器里若留着线程，这里会永远不返回。"""

    monkeypatch.setattr(runner_module, "STOP_TIMEOUT_SEC", 0.5)
    settings = settings_for(rt)

    async def lifecycle() -> None:
        client = StuckWsClient()
        runner = FeishuRunner(EngineSpy(), settings, client_factory=lambda *_: client)
        assert await runner.start() is True
        await runner.stop()

    def run() -> None:
        asyncio.run(lifecycle())

    thread = threading.Thread(target=run, name="lifecycle-check")
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "asyncio.run 没能返回：阻塞线程还在默认执行器里"


# --- TC-FS-02 访问控制 ------------------------------------------------------------------


async def test_tc_fs_02_non_whitelisted_sender_blocked_and_open_id_logged(rt, caplog):
    """TC-FS-02 访问控制。

    验收口径：白名单外 open_id 发文本 -> 引擎一次都不跑（spy 空）、用户侧有拦截结果、日志
    里留下可复制的 open_id（运维加白名单只需要这一串）。

    真实行为分两层，这里都钉住：
      * dispatcher 的闸门先拦，静默 ACK（不回话、不回显 open_id——拒绝不该变成一份可枚举
        的名单探测接口），只记一条带 open_id 的 warning；
      * 引擎那一层（dispatcher 放行、绑定管理器判不过）会给用户一句 UNAUTHORIZED_TEXT。
    """

    settings = settings_for(rt)
    engine = EngineSpy()
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, settings, send_fn=outbox)

    with caplog.at_level(logging.WARNING, logger="app.channels.feishu.dispatcher"):
        handled = await dispatcher.on_message_event(message_event(open_id=OTHER_OPEN_ID))

    assert handled is True  # 已处理：别让飞书重投同一段被拦的事件
    assert engine.messages == []
    assert outbox.payloads == []
    # 可复制的 open_id 就在日志里。
    assert OTHER_OPEN_ID in caplog.text
    assert "feishu_allow_from" in caplog.text

    # 引擎那一层：真正放行到引擎的未授权身份会收到一句人话，且不去碰对话。
    blocked, _mgr, _dedup = channel(rt, settings=settings_for(rt, feishu_allow_from=""))
    reply = Outbox()
    await blocked.handle_message(inbound("给李四转账 500 元"), send_fn=reply)

    assert reply.texts() == [UNAUTHORIZED_TEXT]
    assert reply.cards() == []


# --- TC-FS-03 账号身份绑定 --------------------------------------------------------------


async def test_tc_fs_03_bind_code_links_open_id_and_refuses_reuse_and_expiry(rt):
    """TC-FS-03 账号身份绑定。

    验收口径：先用 ChannelBindingManager.generate_binding_code 造码，飞书侧发 /bind 码
    走 dispatcher + 引擎全链路 -> channel_binding 表里出现该 open_id -> user 的行；同一码
    重复核销被拒；过期码被拒；回执里不出现那串码。
    """

    engine, mgr, dedup = channel(rt)
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, engine._settings, dedup, send_fn=outbox)

    code = mgr.generate_binding_code(USER)
    await dispatcher.on_message_event(
        message_event(text="/bind " + code, message_id="om_bind_1", event_id="ev_bind_1")
    )

    assert outbox.texts() == [BIND_OK_TEXT]
    assert code not in outbox.dump()
    # 表里真的有一行：channel_binding.open_id -> user_0001
    with rt.database.sessions() as db:
        rows = db.scalars(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == OPEN_ID)
        ).all()
    assert [(row.channel, row.user_id) for row in rows] == [("feishu", USER)]

    # 同一枚码重复核销：拒绝（码核销即失效），而且不回显。
    await dispatcher.on_message_event(
        message_event(text="/bind " + code, message_id="om_bind_2", event_id="ev_bind_2")
    )
    assert outbox.texts()[-1] == BIND_FAILED_TEXT
    assert code not in outbox.dump()

    # 过期码：真码 + 拨表越过 10 分钟 TTL。
    clock = Clock()
    expiring = ChannelBindingManager(rt.database.sessions, settings=engine._settings, clock=clock)
    stale = expiring.generate_binding_code(USER)
    clock.advance(601)
    assert expiring.redeem_code(stale, "feishu", OTHER_OPEN_ID, "Mallory") is None
    assert expiring.get_binding("feishu", OTHER_OPEN_ID) is None


# --- TC-FS-04 文本流式问答 --------------------------------------------------------------


async def test_tc_fs_04_streaming_answer_reuses_one_feishu_message(rt):
    """TC-FS-04 文本流式问答。

    验收口径：白名单用户提问 -> 引擎产出卡片更新（至少一次 update_message_id 复用同一条
    消息）、最终卡片正文含预期的回答内容（脚本化模型的固定回复）。
    """

    model = StreamingModel([[FINAL_ANSWER, FINAL_ANSWER]], replies=["飞书问答"])
    install_transfer_agent(rt, TransferBank(), model)
    engine, mgr, dedup = channel(rt, throttler_factory=eager_throttler)
    bind(mgr)
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, engine._settings, dedup, send_fn=outbox)

    await dispatcher.on_message_event(
        message_event(text="查一下我的活期余额", message_id="om_ask_1", event_id="ev_ask_1")
    )

    assert outbox.texts() == []  # 问答全程是卡片，不是一串纯文本
    assert len(outbox.cards()) >= 2, outbox.dump()
    # 中途刷新复用同一条消息：第一条新发拿到 om_1，之后全部是它的原地更新。
    assert outbox.updates, outbox.dump()
    assert set(outbox.updates) == {"om_1"}
    final = outbox.cards()[-1]
    assert FINAL_ANSWER in dump(final)
    assert '"green"' in dump(final)  # 收尾是「已完成」那一档
    # 一轮问答只在账本里留一问一答。
    items = ledger(rt, conversation_of(mgr))
    assert [item["role"] for item in items] == ["user", "assistant"]


# --- TC-FS-05 金融卡片交互（小额无需 MFA） ----------------------------------------------


async def test_tc_fs_05_small_transfer_confirms_once_without_mfa(rt):
    """TC-FS-05 金融卡片交互（小额无需 MFA）。

    验收口径：给李四转账 500 元 -> 收到带 confirm/cancel 按钮的确认卡；模拟卡片点击回调
    （走 dispatcher.on_card_action_event）-> 回调秒级返回「处理中」应答、账本/提交记录里
    恰好一笔执行、卡片最终变成功态、没有第二次出账（同一事件重投也不出第二笔）。
    """

    bank = TransferBank()
    install_transfer_agent(rt, bank, transfer_script(amount="500.00"))
    engine, mgr, dedup = channel(rt)
    bind(mgr)
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, engine._settings, dedup, send_fn=outbox)

    await dispatcher.on_message_event(
        message_event(text="给李四转账 500 元", message_id="om_pay_1", event_id="ev_pay_1")
    )

    card = outbox.cards()[-1]
    assert "操作确认：转账汇款" in dump(card)
    values = button_values(card)
    assert {value["d"] for value in values} == {"confirm", "cancel"}
    assert bank.writes() == []  # 挂卡阶段一个字节都不写

    confirm = next(value for value in values if value["d"] == "confirm")
    started = time.monotonic()
    response = await dispatcher.on_card_action_event(
        card_action_event(confirm, event_id="ev_click_pay_1")
    )
    elapsed = time.monotonic() - started

    # 3 秒应答窗口内先回一张「正在核验并执行…」，真正的授信在后台。
    assert isinstance(response, dict)
    assert "正在核验并执行" in dump(response)
    assert elapsed < 3.0

    await engine.wait_for_background()

    # 恰好一笔出账，而且是带 confirm=True 的那一笔。
    assert len(bank.writes()) == 1, bank.requests
    assert bank.writes()[0]["json"]["confirm"] is True
    assert bank.writes()[0]["json"]["mfa_code"] is None
    # 卡片最终落到成功态，正文是这一轮的结论。
    last = outbox.payloads[-1]
    assert '"green"' in dump(last)
    assert "转账已完成：500.00 元" in dump(last)
    # 结论落在**被点的那张卡**上：context.open_message_id=om_card_1，点一次在对话里只留一条
    # 消息（原地置灰成中间态、原地变成结论），全程没有新开第二条。
    assert "正在核验并执行" in dump(outbox.payloads[1])
    assert set(outbox.updates) == {"om_card_1"}
    assert len(outbox.payloads) == 1 + len(outbox.updates)  # 新建的只有最初那张确认卡
    # 账本：一条 interrupt_answer，没有第二次提交。
    items = ledger(rt, conversation_of(mgr))
    assert [item["role"] for item in items] == ["user", "user", "assistant"]
    assert len([item for item in items if item["message_kind"] == "interrupt_answer"]) == 1

    # 同一事件被飞书重投：不会再出账。
    again = await dispatcher.on_card_action_event(
        card_action_event(confirm, event_id="ev_click_pay_1")
    )
    await engine.wait_for_background()
    assert len(bank.writes()) == 1
    assert DUPLICATE_CLICK_TEXT in dump(again)


# --- TC-FS-06 高额 MFA 短链 --------------------------------------------------------------


async def test_tc_fs_06_high_amount_card_carries_a_working_mfa_link(rt):
    """TC-FS-06 高额 MFA 短链。

    验收口径：高额（5000 元，越过红档阈值）-> 确认卡不含可点的确认回调而含
    /api/channels/mfa?t= 短链；用 TestClient 打 GET 页面（200 中文页）+ POST 提交验证码
    -> 业务执行成功、飞书侧那条会话账本拿到成功结论；同一令牌不能提交第二次（链接用过 410）。
    """

    bank = TransferBank()
    install_transfer_agent(rt, bank, transfer_script(amount="5000.00"))
    engine, mgr, dedup = channel(rt)
    bind(mgr)
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, engine._settings, dedup, send_fn=outbox)

    await dispatcher.on_message_event(
        message_event(text="给李四转账 5000 元", message_id="om_big_1", event_id="ev_big_1")
    )

    card = outbox.cards()[-1]
    rendered = dump(card)
    assert "**【安全核身】**" in rendered
    # 没有可点的确认回调：飞书按钮带不回验证码，那种点击必然被拒。
    assert '"d": "confirm"' not in rendered
    assert [value["d"] for value in button_values(card)] == ["cancel"]
    links = url_buttons(card)
    assert len(links) == 1 and "/api/channels/mfa?t=" in links[0]
    token = links[0].split("/api/channels/mfa?t=", 1)[1]
    payload = verify_mfa_token(token, rt.settings.secret_key)
    assert payload is not None and payload["uid"] == USER
    assert bank.writes() == []

    client = TestClient(create_app(rt.settings, runtime=rt))

    page = client.get("/api/channels/mfa", params={"t": token})
    assert page.status_code == 200
    assert "请输入短信验证码" in page.text

    posted = client.post("/api/channels/mfa/submit", data={"t": token, "code": "123456"})
    assert posted.status_code == 200, posted.text
    assert "操作已提交" in posted.text
    # 业务执行成功：恰好一笔出账，验证码进了银行请求体（也只在那一处）。
    assert len(bank.writes()) == 1
    assert bank.writes()[0]["json"]["mfa_code"] == "123456"
    assert "123456" not in posted.text
    # 飞书侧那条会话（短链指向的就是它）拿到成功结论。
    items = ledger(rt, payload["cid"])
    assert [item["message_kind"] for item in items if item["role"] == "user"] == [
        "message",
        "interrupt_answer",
    ]
    assert items[-1]["role"] == "assistant"
    assert "转账已完成：5,000.00 元" in posted.text

    # 同一令牌不再执行第二次：提交被收口成「已经处理过」，链接本身已经用过（410）。
    second = client.post("/api/channels/mfa/submit", data={"t": token, "code": "123456"})
    assert "已经处理过" in second.text
    assert client.get("/api/channels/mfa", params={"t": token}).status_code == 410
    assert len(bank.writes()) == 1


# --- TC-FS-07 水位丢弃与防重 ------------------------------------------------------------


async def test_tc_fs_07_historical_events_dropped_and_repeats_handled_once(rt):
    """TC-FS-07 水位丢弃与防重。

    验收口径：启动水位之前的历史消息被丢弃（不调引擎）；同一条 message 重投只处理一次；
    同一张卡连点两次第二次被幂等拦截（30 s 窗口内），且账本只有一笔。
    """

    bank = TransferBank()
    install_transfer_agent(rt, bank, transfer_script(amount="500.00", extra=(CLICK_REPLY,)))
    engine, mgr, dedup = channel(rt, boot_time=time.time())
    bind(mgr)
    outbox = Outbox()
    dispatcher = FeishuDispatcher(engine, engine._settings, dedup, send_fn=outbox)

    # 1) 水位线之前（服务起来之前就躺在飞书队列里）的堆积：静默 ACK，一个字都不回。
    stale = int((dedup.boot_time - 60.0) * 1000)
    await dispatcher.on_message_event(
        message_event(
            text="给李四转账 500 元",
            message_id="om_stale_1",
            event_id="ev_stale_1",
            create_time_ms=stale,
        )
    )
    assert outbox.payloads == []
    assert bank.requests == []

    # 2) 同一条消息重投：只跑一轮对话。
    fresh = int(time.time() * 1000)
    event = message_event(
        text="给李四转账 500 元",
        message_id="om_new_1",
        event_id="ev_new_1",
        create_time_ms=fresh,
    )
    await dispatcher.on_message_event(event)
    first = len(outbox.payloads)
    assert first > 0
    await dispatcher.on_message_event(event)
    assert len(outbox.payloads) == first

    # 3) 同一张卡连点两次：第二次同步返回「已经处理过了」，不出第二笔。
    card = outbox.cards()[-1]
    confirm = next(value for value in button_values(card) if value["d"] == "confirm")
    shared = card_action_event(confirm, event_id="ev_click_twice", create_time_ms=fresh)

    await dispatcher.on_card_action_event(shared)
    second = await dispatcher.on_card_action_event(shared)
    await engine.wait_for_background()

    assert len(bank.writes()) == 1
    assert DUPLICATE_CLICK_TEXT in dump(second)
    items = ledger(rt, conversation_of(mgr))
    assert len([item for item in items if item["message_kind"] == "interrupt_answer"]) == 1


# --- TC-FS-08 锁安全防御 ----------------------------------------------------------------


class _TurnsProxy:
    """只替换流的那一段：真 acquire / 真 prepare，stream 换成一条会卡住的流。"""

    def __init__(self, real, stream) -> None:
        self._real = real
        self._stream_fn = stream

    async def acquire(self, conversation_id):
        return await self._real.acquire(conversation_id)

    async def prepare(self, *args, **kwargs):
        return await self._real.prepare(*args, **kwargs)

    def stream(self, user_id, session_id, plan, *, naming=None):
        del user_id, session_id, naming
        return self._stream_fn(plan)


class _RtProxy:
    """rt 的代理：其余属性原样转发，只有 turns 被换掉。"""

    def __init__(self, rt, turns) -> None:
        self._rt = rt
        self.turns = turns

    def __getattr__(self, name):
        return getattr(self._rt, name)


async def test_tc_fs_08_failures_never_leak_out_and_always_release_the_lock(rt):
    """TC-FS-08 锁安全防御。

    验收口径：send_fn 恒抛 / 网络超时 / 流被取消，会话排他锁都必须释放（真 asyncio.Lock +
    真 rt 的 conversation_lock 验证），且异常不外泄到飞书回调（dispatcher 不抛）。
    """

    bank = TransferBank()
    model = ScriptedModel([HANDOFF, prepare_call("500.00"), HANDOFF, prepare_call("500.00")])
    install_transfer_agent(rt, bank, model)
    # 三个身份都在白名单里：这一条验的是投递失败与取消，不是访问控制。
    allow = ",".join([OPEN_ID, SECOND_OPEN_ID, THIRD_OPEN_ID])
    engine, mgr, dedup = channel(rt, settings=settings_for(rt, feishu_allow_from=allow))
    bind(mgr, OPEN_ID, "Alice")

    # (1) send_fn 恒抛：飞书抖了，或者凭据过期了。
    await engine.handle_message(inbound("给李四转账 500 元"), send_fn=ExplodingOutbox())
    assert rt.agent.conversation_lock(conversation_of(mgr)).locked() is False

    # (2) 网络超时：换一个身份（自己的会话），首投抛 TimeoutError。
    bind(mgr, SECOND_OPEN_ID, "Bob")
    await engine.handle_message(
        inbound("给李四转账 500 元", sender_id=SECOND_OPEN_ID),
        send_fn=ExplodingOutbox(TimeoutError("timed out")),
    )
    assert rt.agent.conversation_lock(conversation_of(mgr, SECOND_OPEN_ID)).locked() is False

    # (3) 流被取消：卡住的流 + 外部取消，锁同样要还，用户看到一张「已中断」。
    bind(mgr, THIRD_OPEN_ID, "Carol")

    async def stuck(plan):
        del plan
        yield SimpleNamespace(event="reply_delta", data={"text": "正在生成的前半段"})
        await asyncio.sleep(30)
        yield SimpleNamespace(event="final", data={"text": "不该到这里"})

    proxy = _RtProxy(rt, _TurnsProxy(rt.turns, stuck))
    cancelling = ChannelEngine(rt=proxy, settings=engine._settings, binding_mgr=mgr, dedup=dedup)
    outbox = Outbox()
    task = asyncio.ensure_future(
        cancelling.handle_message(
            inbound("给李四转账 500 元", sender_id=THIRD_OPEN_ID), send_fn=outbox
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    conversation = conversation_of(mgr, THIRD_OPEN_ID)
    assert rt.agent.conversation_lock(conversation).locked() is False
    assert INTERRUPTED_TEXT in dump(outbox.payloads[-1])
    assert outbox.updates == ["om_1"]

    # (4) 异常不外泄：引擎抛，dispatcher 只记日志并安静返回。
    quiet = FeishuDispatcher(RaisingEngine(), engine._settings, send_fn=Outbox())
    handled = await quiet.on_message_event(message_event(message_id="om_boom", event_id="ev_boom"))
    assert handled is False
    assert (
        await quiet.on_card_action_event(
            card_action_event({"q": "q-1", "p": "wp_1", "d": "confirm"})
        )
        is None
    )


# --- 跨实例：绑定码必须能被渠道那一侧核销 ------------------------------------------------


async def test_cross_instance_a_code_from_the_web_endpoint_redeems_through_the_engine_manager(
    rt, session, monkeypatch
):
    """跨实例绑定码：Web 端点生成的码，必须能被**引擎手里那份** manager 核销掉。

    绑定码只存 hmac(进程随机盐, 码) 的摘要，而盐是每个 ChannelBindingManager 现抽的：进程里只要
    存在第二份，网页生成的码在飞书里就永远核销不了。默认 feishu_auto_bind=True 会把这个洞一直
    盖住——用户看到的是"能用了"，其实是自动绑定救的场，显式 /bind 那条路一直是断的。

    这里钉住的是"只有一份"：lifespan 交给 ChannelEngine 的 manager 与 Web 端点用的 manager 是
    **同一个对象**，所以码核销得动，并落进 channel_binding。
    """

    from app.api.channels import channel_bindings_for
    from app.channels import feishu as feishu_package
    from app.channels.feishu.boot import feishu_bridge_for

    channel_settings = settings_for(
        rt, feishu_enabled=True, feishu_allow_from=OTHER_OPEN_ID, feishu_auto_bind=False
    )
    # 生产里 rt.settings 就是渠道那一份：Web 端点与引擎拿到的是同一份配置、同一个管理器。
    monkeypatch.setattr(rt, "settings", channel_settings)
    app = create_app(channel_settings, runtime=rt)
    bindings = channel_bindings_for(app, rt)

    # Web 侧：真的打那个端点拿码，用户拿到手的就是这一串。
    client = TestClient(app, headers={"Authorization": "Bearer " + session["token"]})
    code = client.post("/api/channels/binding-code").json()["data"]["code"]

    # 渠道侧：走 lifespan 的那一段装配（长连与 SDK 换成替身，其余都是真代码）。
    captured: dict = {}

    class StubDispatcher:
        def __init__(self, engine, settings, dedup, **kwargs):
            del settings, dedup, kwargs
            captured["engine"] = engine

    class StubRunner:
        is_running = True

        def __init__(self, dispatcher, settings):
            del dispatcher, settings

        async def start(self):
            return True

        def describe(self):
            return "feishu_runner stub running=True"

    monkeypatch.setattr(feishu_package, "FeishuDispatcher", StubDispatcher)
    monkeypatch.setattr(feishu_package, "FeishuRunner", StubRunner)

    bridge = feishu_bridge_for(app, rt, bindings)
    assert await bridge.start() is True

    assert bridge.runner is not None
    assert app.state.channel_bindings is bindings
    engine = captured["engine"]
    assert engine._binding_mgr is bindings  # 引擎拿的就是 Web 那一份，不是第二个实例

    # 飞书侧 /bind 全链路：引擎 -> 它那份 manager -> 核销网页造的那枚码。
    outbox = Outbox()
    await engine.handle_message(inbound("/bind " + code, sender_id=OTHER_OPEN_ID), send_fn=outbox)

    assert outbox.texts() == [BIND_OK_TEXT]
    assert code not in outbox.dump()
    with rt.database.sessions() as db:
        row = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == OTHER_OPEN_ID)
        )
    assert row is not None
    assert (row.channel, row.user_id) == ("feishu", USER)


# --- 配置自检：手机打不开的短链基址 ------------------------------------------------------


def test_the_local_mfa_base_url_warning_fires_only_when_feishu_is_on(rt, caplog):
    """飞书开着、而短链基址还是本机地址时打一条 warning：手机打不开 127.0.0.1。"""

    from app.core.config import warn_if_mfa_base_url_is_local

    local = settings_for(rt, feishu_enabled=True, channel_mfa_base_url="http://127.0.0.1:8000")
    loopback = settings_for(rt, feishu_enabled=True, channel_mfa_base_url="http://localhost:8000")
    reachable = settings_for(rt, feishu_enabled=True, channel_mfa_base_url="http://10.0.0.5:8000")
    off = settings_for(rt, feishu_enabled=False, channel_mfa_base_url="http://127.0.0.1:8000")

    with caplog.at_level("WARNING", logger="app.core.config"):
        assert warn_if_mfa_base_url_is_local(local) is True
        assert warn_if_mfa_base_url_is_local(loopback) is True
        assert warn_if_mfa_base_url_is_local(reachable) is False
        # 飞书没开就没有"手机点卡片"这回事，别用一条无关的 warning 去教人改配置。
        assert warn_if_mfa_base_url_is_local(off) is False

    assert "channel_mfa_base_url" in caplog.text
    assert "127.0.0.1" in caplog.text
    assert "10.0.0.5" not in caplog.text


class SlowBuildClient:
    """假客户端：建得慢，但建起来之后 start() 就阻塞着（和真实 lark 一样不返回）。"""

    def __init__(self) -> None:
        self.started = threading.Event()

    def start(self) -> None:
        self.started.set()
        threading.Event().wait()


async def test_a_client_that_is_built_late_still_turns_the_bridge_on(rt, monkeypatch):
    """首次 import lark 慢到超过等待窗口时，不能把线程收掉：建好之后状态要自己转回运行。

    这条钉住的是真机上踩到的那种情况：冷启动第一次 import lark-oapi 要十几秒，窗口一过
    旧实现就 abandon 掉任务，客户端随后其实连上了、消息也在回，但网页上的接入状态一直
    显示「未接入」，只能重启。
    """

    monkeypatch.setattr(runner_module, "READY_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(runner_module, "STOP_TIMEOUT_SEC", 0.5)
    settings = settings_for(rt)
    client = SlowBuildClient()

    def slow_factory(*_args):
        time.sleep(0.6)
        return client

    runner = FeishuRunner(EngineSpy(), settings, client_factory=slow_factory)

    assert await runner.start() is False, "窗口内没建完就该如实说没建好"
    assert await asyncio.wait_for(asyncio.to_thread(client.started.wait, 5), 10) is True
    assert runner.is_running is True, "线程还在，就不该报成没在跑"

    await asyncio.wait_for(runner.stop(), 25)
    assert runner.is_running is False
