"""离线验证 CLI 入口：它只能做输入输出与依赖装配，不能绕开工作流。

这个文件不连银行、也不连模型（`tests/wealth/conftest.py` 已经禁掉出站连接）：

- 入口把每种输入翻译成哪种 `WealthRequest`；待确认时"确认"走受控的 `kind=confirm`，
  **旧验证码入口（`kind=verify`）已经停用**：CLI 不再提供它，也不再单独读一次验证码；
- 缺绑定客户、缺真实模型配置时启动即报错，不退化成内存银行或规则脚本；
- 确认预览整段渲染（金额按整数分格式化），让"确认的是哪一笔"在终端上完整可见；
- `--check` 只做只读探针，银行不可用时以非零退出，不把"读到 0 个产品"当成通过。

正式入口的 HTTP 行为由 `HttpWealthBank` 自己保证，不在这里重复测。
"""

from __future__ import annotations

import inspect
import io
import json
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from app.modules.wealth import cli
from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest, WealthRequestKind
from app.modules.wealth.contracts.responses import Pending, WealthResponse, WealthStatus
from app.modules.wealth.workflow.state import PendingData, SessionData
from app.tools.protocol import TrustedContext
from tests.wealth import support

# ---------------------------------------------------------------------- 假 Agent

# 端到端那条用例要用的银行端点与客户事实（与 tests/wealth/test_purchase.py 的同一套样本）。
PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"
PURCHASE = "/products/purchase"
ORDER = "/wealth/orders/order_purchase_1"
SLOTS = {"product_id": "p_demo_cash", "account_id": "a_wealth_main", "amount_minor": 200000}
AMOUNT_MINOR = 200000


@dataclass
class FakeAgent:
    """只记录 `handle` 收到了什么，并按脚本返回响应。

    "入口不绕开工作流"就是这一条断言：CLI 的每一行输入都必须出现在这里，而且是以
    `WealthRequest` 的形式——CLI 里没有第二条通往银行的路径。
    """

    replies: list[WealthResponse] = field(default_factory=list)
    requests: list[WealthRequest] = field(default_factory=list)
    seen: list[tuple[SessionData, PendingData | None]] = field(default_factory=list)

    def handle(
        self,
        context: TrustedContext,
        request: WealthRequest,
        *,
        session: SessionData,
        pending: PendingData | None,
        conversation: Any = None,
    ) -> tuple[WealthResponse, SessionData, PendingData | None]:
        del context, conversation
        self.requests.append(request)
        self.seen.append((session, pending))
        if self.replies:
            response = self.replies.pop(0)
        else:
            response = WealthResponse(status=WealthStatus.COMPLETED, summary="好")
        # 像运行时一样把会话交回调用方：入口手里不留状态，只能靠返回值带下去。
        carried: SessionData = dict(session)
        carried["calls"] = carried.get("calls", 0) + 1
        return response, carried, pending


def _waiting(status: WealthStatus, pending_id: str = "wp_1") -> WealthResponse:
    return WealthResponse(
        status=status,
        summary="请继续",
        pending=Pending(pending_id=pending_id, prompt="请继续"),
    )


def _chat(agent: Any, *, out: io.StringIO | None = None, err: io.StringIO | None = None):
    context = TrustedContext(
        user_id="u_cli", session_id="s_cli", bank_customer_id=support.DEMO_CUSTOMER
    )
    return cli.WealthChat(
        agent,
        context,
        out=out or io.StringIO(),
        err=err or io.StringIO(),
    )


def _stub_reads(double) -> None:
    """预览、写前复核与写后核实要读的银行事实，全部按同一个传输替身预设。"""

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", PRODUCT, data=support.product_payload())
    double.on_sequence("GET", ACCOUNTS, [support.account_payload()])
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))


def _preview_replies() -> tuple[str, ...]:
    """模型先查到产品与账户（编号必须有据），再发起一次申购预览。"""

    return (
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(SLOTS),
    )


def _order_readback() -> dict[str, Any]:
    """写后核实读回的订单：必须与回执和确认过的快照对得上，否则不会被当成成交。"""

    return support.order_payload(
        order_id="order_purchase_1",
        holding_id="h_purchase_1",
        transaction_id="txn_purchase_1",
        amount_minor=AMOUNT_MINOR,
    )


# ---------------------------------------------------------------------- 输入翻译


def test_message_becomes_message_request() -> None:
    agent = FakeAgent()
    chat = _chat(agent)

    chat.feed("有哪些灵活赎回的产品？")

    assert [request.kind for request in agent.requests] == [WealthRequestKind.MESSAGE]
    assert agent.requests[0].text == "有哪些灵活赎回的产品？"


def test_answer_uses_the_pending_of_the_last_response() -> None:
    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_INPUT, "wp_ask")])
    chat = _chat(agent)

    chat.message("我想买理财")
    chat.feed("一万元")

    assert [request.kind for request in agent.requests] == [
        WealthRequestKind.MESSAGE,
        WealthRequestKind.ANSWER,
    ]
    assert agent.requests[1].pending_id == "wp_ask"
    assert agent.requests[1].text == "一万元"


@pytest.mark.parametrize("line", ["确认", "确认购买", "y", "yes", "confirm"])
def test_confirmation_words_become_confirm(line: str) -> None:
    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_CONFIRMATION, "wp_buy")])
    chat = _chat(agent)

    chat.message("买两千")
    chat.feed(line)

    assert agent.requests[-1].kind is WealthRequestKind.CONFIRM
    assert agent.requests[-1].pending_id == "wp_buy"


@pytest.mark.parametrize("line", ["取消", "算了", "n", "cancel"])
def test_cancel_words_become_cancel(line: str) -> None:
    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_CONFIRMATION, "wp_buy")])
    chat = _chat(agent)

    chat.message("买两千")
    chat.feed(line)

    assert agent.requests[-1].kind is WealthRequestKind.CANCEL


def test_other_words_while_waiting_confirmation_go_to_the_workflow() -> None:
    """等待确认时说了别的：仍按一句话交给工作流，由运行时决定是否放弃旧任务。"""

    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_CONFIRMATION, "wp_buy")])
    chat = _chat(agent)

    chat.message("买两千")
    chat.feed("先看看稳健的产品")

    assert agent.requests[-1].kind is WealthRequestKind.MESSAGE
    assert agent.requests[-1].text == "先看看稳健的产品"


# ------------------------------------------------- 确认即提交（旧验证码入口已停用）


def test_the_retired_verify_entry_is_not_offered_at_all() -> None:
    """旧验证码入口在 CLI 这一侧根本不存在：没有 verify 输入，也没有"单独读一次验证码"。

    点击确认取代了"确认 + 输入验证码"。旧用例断言的是"验证码被单独读、绝不作为聊天文本"——
    那件事现在无从发生：不是换了一种读法，而是这条输入连同它读的秘密一起从入口消失了。
    """

    assert not hasattr(cli.WealthChat, "verify")
    # 状态标志也跟着改名：等的是**确认**，不是验证码。
    assert not hasattr(cli.WealthChat, "needs_code")
    assert hasattr(cli.WealthChat, "needs_confirmation")
    for name in ("CODE_PROMPT", "_read_code_hidden", "_read_code_line"):
        assert not hasattr(cli, name), f"{name} 是旧验证码交互的残留"

    # `run_session` 不再有"额外读一次"的参数：每一行都只经 `feed` 翻译。
    parameters = inspect.signature(cli.run_session).parameters
    assert "read_code" not in parameters


def test_a_confirmation_word_submits_and_no_verification_code_is_ever_read() -> None:
    """待确认时输入"确认"就是提交：CLI 发一次 `kind=confirm`，并且从不读验证码。"""

    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_CONFIRMATION, "wp_buy")])
    out = io.StringIO()
    chat = _chat(agent, out=out)

    chat.message("买两千")
    exit_code = cli.run_session(chat, ["确认"], out=out, err=io.StringIO())

    assert exit_code == 0
    assert [request.kind for request in agent.requests] == [
        WealthRequestKind.MESSAGE,
        WealthRequestKind.CONFIRM,
    ]
    confirm = agent.requests[-1]
    # 确认作用在**那一个**待办上：pending_id 来自上一条响应，不是猜的。
    assert confirm.pending_id == "wp_buy"
    # 确认输入不带任何验证码字段——不是"这次没填"，而是这条路径上根本没有它。
    assert confirm.verification_code is None
    assert all(request.verification_code is None for request in agent.requests)
    assert WealthRequestKind.VERIFY not in [request.kind for request in agent.requests]
    # 终端上也没有任何要用户输入验证码的提示。
    assert "验证码" not in out.getvalue()


def test_a_confirm_entered_in_the_terminal_reaches_the_bank(double, clock) -> None:
    """终端上的"确认"最终真的变成一次资金写：CLI 只翻译输入，提交由运行时完成。

    用声明支持点击确认的适配器替身（`bank` 夹具是真实的 `HttpWealthBank`，它回答 False，
    确认会在发出任何资金请求之前被拒绝——那是另一条用例的事）。写请求里没有验证码字段。
    """

    _stub_reads(double)
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on("GET", ORDER, data=_order_readback())
    model = support.ScriptModel(*_preview_replies())
    agent = build_wealth_agent(model, support.ClickConfirmationBank(double.client()), clock)
    chat = _chat(agent)

    exit_code = cli.run_session(
        chat,
        ["买 2000 元的演示灵活理财，用主账户", "确认"],
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert exit_code == 0
    posts = [call for call in double.calls if call.method == "POST"]
    assert [call.path for call in posts] == [PURCHASE]
    assert "mfa_code" not in (posts[0].body or {})
    assert posts[0].body is not None
    assert posts[0].body["confirm"] is True
    assert chat.last is not None
    assert chat.last.status is WealthStatus.COMPLETED


def test_input_ending_while_a_confirmation_is_pending_submits_nothing() -> None:
    """输入在待确认时结束：CLI 不会替用户确认，一次写请求也不会发出。

    旧用例断言的是"读不到验证码就结束会话、不提交"。验证码那一档取消之后，同一条不变量仍然
    成立：**待确认的待办不会因为输入结束而被提交**，它只是留着（到期作废）。
    """

    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_CONFIRMATION, "wp_buy")])
    chat = _chat(agent)
    chat.message("买两千")

    exit_code = cli.run_session(chat, [], out=io.StringIO(), err=io.StringIO())

    assert exit_code == 0
    kinds = [request.kind for request in agent.requests]
    assert kinds == [WealthRequestKind.MESSAGE]
    assert WealthRequestKind.CONFIRM not in kinds
    assert WealthRequestKind.VERIFY not in kinds


def test_the_entry_point_keeps_no_business_logic_and_parses_no_model_output() -> None:
    """入口只翻译输入：它不解析模型输出、不碰工作流，也不认识任何业务规则。

    模型输出（工具调用）由 `llm` 层解析，理财流程由 `workflow`／`application` 负责；入口一旦
    导入它们，就等于在 CLI 里长出了第二条通往银行的路径。这里按导入面钉住这条边界。
    """

    imported = {name for name, value in vars(cli).items() if isinstance(value, ModuleType)}
    assert {"decisions", "toolcalls", "workflow", "rules", "runtime"}.isdisjoint(imported)
    # 直接导入其中的函数／类同样不行：这几个名字是"入口开始自己做业务"的入口点。
    for name in (
        "parse_decision",
        "tool_table",
        "build_read_graph",
        "invoke_read_workflow",
        "build_wealth_graph",
    ):
        assert not hasattr(cli, name), f"入口不应直接持有 {name}"
    # 入口自己也没有任何"直接下单"的方法：只有翻译与渲染。
    assert not hasattr(cli.WealthChat, "purchase")
    assert not hasattr(cli.WealthChat, "redeem")


def test_the_cli_hands_the_session_back_on_the_next_turn() -> None:
    """跨轮状态不在入口里：它只是把运行时交回来的会话原样递给下一次调用。"""

    agent = FakeAgent()
    chat = _chat(agent)

    chat.message("第一句")
    chat.message("第二句")

    assert [session.get("calls") for session, _ in agent.seen] == [None, 1]


def test_quit_word_ends_the_session() -> None:
    agent = FakeAgent()
    chat = _chat(agent)

    assert cli.run_session(chat, ["退出"], out=io.StringIO(), err=io.StringIO()) == 0
    assert agent.requests == []


# ------------------------------------------------------- 输入必须边读边处理


def test_interactive_line_is_processed_before_the_next_prompt() -> None:
    """交互模式：一句话输入后要立刻处理，不能先把输入读完。

    回归 bug：`run_session` 曾对输入做 `list(lines)`。交互输入是**无限生成器**
    （`_interactive_lines` 循环调用 `input()`），`list()` 因此永不返回，用户每句话都被
    攒进缓冲区，终端只反复出现提示符、看不到任何回复，直到 Ctrl+C 才一次性回放。
    """

    agent = FakeAgent()
    out = io.StringIO()
    chat = _chat(agent, out=out)
    handled_before_next_read: list[int] = []
    reads = 0

    def read_line() -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            return "有哪些可以灵活赎回的产品？"
        # 第二次读输入之前，第一句话必须已经被交给 Agent 并渲染过。
        handled_before_next_read.append(len(agent.requests))
        raise KeyboardInterrupt

    exit_code = cli.run_session(
        chat,
        cli._interactive_lines(read_line),
        out=out,
        err=io.StringIO(),
    )

    assert exit_code == 0
    assert handled_before_next_read == [1]
    assert agent.requests[0].text == "有哪些可以灵活赎回的产品？"
    assert "好" in out.getvalue()


def test_stream_input_is_processed_line_by_line_without_swallowing_a_line() -> None:
    """从输入流读时同样要边读边处理：待确认那一行必须被当成确认，不能被丢掉或当成聊天。"""

    agent = FakeAgent(replies=[_waiting(WealthStatus.WAITING_CONFIRMATION, "wp_buy")])
    chat = _chat(agent)
    stream = io.StringIO("买两千元的演示灵活理财\n确认\n")

    exit_code = cli.run_session(chat, stream, out=io.StringIO(), err=io.StringIO())

    assert exit_code == 0
    assert [request.kind for request in agent.requests] == [
        WealthRequestKind.MESSAGE,
        WealthRequestKind.CONFIRM,
    ]
    assert agent.requests[-1].pending_id == "wp_buy"
    assert agent.requests[-1].verification_code is None


# ---------------------------------------------------------------------- 渲染


def test_confirmation_preview_is_rendered_in_full() -> None:
    """终端要显示完整的确认预览：产品、金额、账户、余额与原文都打印出来。"""

    preview = {
        "product_id": "p_demo_cash",
        "product_name": "演示灵活理财",
        "from_account_id": "a_wealth_main",
        "amount_minor": 200000,
        "term_months": 0,
        "risk_level": "low",
        "redeem_rule": "flexible",
        "balance_minor": 2000000,
        "preview": "确认申购：演示灵活理财 2,000.00 元，从 a_wealth_main 扣款。",
    }
    agent = FakeAgent(
        replies=[
            WealthResponse(
                status=WealthStatus.WAITING_CONFIRMATION,
                summary=preview["preview"],
                facts={"purchase_preview": preview},
                pending=Pending(pending_id="wp_buy", prompt=preview["preview"]),
            )
        ]
    )
    out = io.StringIO()
    chat = _chat(agent, out=out)

    chat.render(chat.message("买两千"))

    rendered = out.getvalue()
    assert "申购确认预览" in rendered
    assert "2,000.00 元" in rendered
    assert "20,000.00 元" in rendered
    assert "a_wealth_main" in rendered
    assert "p_demo_cash" in rendered


def test_receipt_and_business_ids_are_rendered() -> None:
    agent = FakeAgent(
        replies=[
            WealthResponse(
                status=WealthStatus.COMPLETED,
                summary="模拟申购已完成",
                facts={"purchase_receipt": {"order_id": "order_1", "status": "completed"}},
                business_ids={"order_id": "order_1", "holding_id": "h_1"},
            )
        ]
    )
    out = io.StringIO()
    chat = _chat(agent, out=out)

    chat.render(chat.message("买两千"))

    rendered = out.getvalue()
    assert "order_id=order_1" in rendered
    assert "holding_id=h_1" in rendered
    assert "银行回执" in rendered


# ---------------------------------------------------------------------- 模型装配


def test_missing_real_model_config_is_a_loud_error() -> None:
    err = io.StringIO()
    settings = SimpleNamespace(
        llm_base_url="", llm_api_key="", llm_model="", llm_response_format="json_schema"
    )

    with pytest.raises(cli.CliError) as error:
        cli.build_model(None, settings, err=err)

    message = str(error.value)
    assert "LLM_BASE_URL" in message and "LLM_API_KEY" in message and "LLM_MODEL" in message
    assert "--script" in message


def test_script_mode_prints_a_banner_that_says_it_is_not_a_model(tmp_path) -> None:
    script = tmp_path / "decisions.jsonl"
    # 脚本里的决策就是模型该输出的那条工具调用：一行一个 `{"tool": ..., "arguments": ...}`。
    call = support.call_read("wealth_products")
    decision = json.dumps({"tool": call.name, "arguments": call.arguments}, ensure_ascii=False)
    script.write_text(f"# 注释行\n\n{decision}\n", encoding="utf-8")
    err = io.StringIO()

    model = cli.build_model(str(script), SimpleNamespace(), err=err)

    assert cli.SCRIPT_BANNER in err.getvalue()
    assert isinstance(model, cli.ScriptedModel)
    # 空行与注释行不是决策：脚本里只有一条。
    assert model.complete(()) == cli.ModelReply(text=decision)


def test_exhausted_script_fails_closed() -> None:
    from app.integration.model_client import ModelClientError

    model = cli.ScriptedModel([json.dumps({"tool": "finish", "arguments": {"text": "好"}})])
    model.complete(())

    with pytest.raises(ModelClientError):
        model.complete(())


def test_script_mode_streams_a_tool_call() -> None:
    """脚本的一行决策经 stream 变成一次原生工具调用分片，而不是一段 JSON 文本。"""

    model = cli.ScriptedModel([json.dumps({"tool": "wealth_products", "arguments": {}})])

    chunks = list(model.stream((), tools=()))

    assert chunks[0].tool_name == "wealth_products"
    assert json.loads(chunks[0].tool_call_delta) == {}
    assert chunks[-1].finish is True


def test_unreadable_script_is_reported_at_startup(tmp_path) -> None:
    with pytest.raises(cli.CliError):
        cli._read_script(str(tmp_path / "nope.jsonl"))


# ---------------------------------------------------------------------- 启动检查


def test_main_requires_an_explicit_customer(monkeypatch, capsys) -> None:
    """没有绑定客户就退出，而且一次银行请求都不发。"""

    calls: list[str] = []

    def forbidden(*args, **kwargs):
        del args, kwargs
        calls.append("bank")
        raise AssertionError("缺绑定客户时不得构造银行客户端")

    monkeypatch.setattr(cli, "build_bank", forbidden)
    monkeypatch.delenv(cli.CUSTOMER_ENV, raising=False)

    exit_code = cli.main(["--check", "--bank-url", support.BANK_BASE_URL])

    assert exit_code == 2
    assert calls == []
    assert "缺少绑定客户" in capsys.readouterr().err


def test_check_probe_reports_the_bank_and_the_model(monkeypatch, capsys, double) -> None:
    double.on("GET", "/products", data=support.wealth_demo_products())
    double.on("GET", "/accounts", data=[support.account_payload()])
    monkeypatch.setattr(cli, "build_bank", lambda *a, **k: _bank_of(double))
    monkeypatch.setattr(cli, "build_model", _stub_model)

    exit_code = cli.main(
        ["--check", "--bank-url", support.BANK_BASE_URL, "--customer", support.DEMO_CUSTOMER]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "只读探针通过" in captured.out
    assert "4 个产品" in captured.out


def test_check_probe_fails_when_the_bank_is_down(monkeypatch, capsys, double) -> None:
    double.on_failure("GET", "/products", httpx.ConnectError("refused"))
    monkeypatch.setattr(cli, "build_bank", lambda *a, **k: _bank_of(double))
    monkeypatch.setattr(cli, "build_model", _stub_model)

    exit_code = cli.main(
        ["--check", "--bank-url", support.BANK_BASE_URL, "--customer", support.DEMO_CUSTOMER]
    )

    assert exit_code == 3
    assert "银行只读探针失败" in capsys.readouterr().err


def _stub_model(*args, **kwargs):
    """自检不调用模型：这里只需要"模型装配成功"这一件事，不需要真的模型。"""

    del args, kwargs
    return _NeverModel()


class _NeverModel:
    def complete(self, messages: Any) -> Any:
        del messages
        raise AssertionError("自检不应调用模型")

    def close(self) -> None:
        return None


def _bank_of(double: support.BankDouble):
    from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank

    return HttpWealthBank(double.client())


def test_default_bank_url_comes_from_settings(monkeypatch, capsys) -> None:
    """没给 --bank-url 时用配置里的银行地址，并且把它打印出来（不静默）。"""

    seen: list[str] = []

    def fake_build(url: str, **kwargs):
        del kwargs
        seen.append(url)
        return _DeadBank()

    monkeypatch.setattr(cli, "build_bank", fake_build)
    monkeypatch.setattr(cli, "build_model", _stub_model)
    monkeypatch.delenv(cli.BANK_URL_ENV, raising=False)

    from app.core.config import get_settings

    exit_code = cli.main(["--check", "--customer", support.DEMO_CUSTOMER])

    # 入口拿到的就是配置里的那个地址；银行探针失败因此以 3 退出，而不是"读到 0 个产品"。
    assert seen == [get_settings().bank_backend_base_url]
    assert exit_code == 3
    assert "只读探针失败" in capsys.readouterr().err


class _DeadBank:
    """只读探针一定失败的银行替身：用来断言"读不到"不是"读到 0 个"。"""

    def list_products(self, context: Any) -> Any:
        del context
        raise RuntimeError("bank is down")

    def list_accounts(self, context: Any) -> Any:
        del context
        raise RuntimeError("bank is down")
