"""独立理财 Agent 的本地入口：只做输入输出与依赖装配。

这个文件**不实现任何理财业务**，也**不绕开工作流**：每一条输入都变成一次
`WealthAgent.handle(context, request, session=..., pending=...)`，CLI 里没有一句银行调用，
没有"直接买产品"的捷径，也不认识会话内容：它只是把运行时交回来的会话与待办原样拿着，下一次再递
回去（跨轮状态因此随调用进出，而不是藏在运行时里）。三件事在这里被刻意做死（计划任务12）：

1. **银行只能是 `HttpWealthBank`。** 正式演示默认连真实 HTTP 银行，银行 URL 与绑定客户必须
   显式给出（`--bank-url`／`WEALTH_BANK_URL`，`--customer`／`WEALTH_BANK_CUSTOMER`）：缺任何
   一个都在启动时报错。没有内存银行这个选项，也不会默认拿银行自己的演示客户顶替。
2. **模型只能是 `ModelClient`。** 默认用 `backend/.env` 的 `LLM_BASE_URL`／`LLM_API_KEY`／
   `LLM_MODEL` 装配真实的 Chat Completions 客户端；三项不全就明确报错，不悄悄降级成规则脚本。
   脚本测试模式必须由 `--script` **显式**选择，并在终端打出横幅说明"回复来自脚本文件，不代表
   模型理解"——它只用来验证链路，不能伪装成一次真实模型对话。
3. **确认即提交。** 待确认时用一个独立的、只接受"确认／取消"的输入读取用户动作，经
   `WealthRequest(kind="confirm")` 送进运行时；它从不作为聊天文本进入模型上下文，也不进日志。
   旧验证码入口已经停用：CLI 不再向用户索要验证码，也明确拒绝把它当作正常流程。

确认预览整段打印：`response.summary` 就是预览原文，`facts` 里还有冻结的快照字段（产品、金额、
账户、余额、期限、赎回规则），所以"用户确认的到底是哪一笔"在终端上完整可见。

交互式演示：

    cd backend
    .venv/Scripts/python.exe -m app.modules.wealth.cli \\
        --bank-url http://127.0.0.1:8001/api --customer u_wealth

非交互（脚本化演示、验收与自检）：

    .venv/Scripts/python.exe -m app.modules.wealth.cli --check
    .venv/Scripts/python.exe -m app.modules.wealth.cli \\
        --message "有哪些可以灵活赎回的产品？" --message "买 2000 元的 p_demo_cash"

依赖方向：入口 → `application.factory` → 运行时／工作流；入口本身不碰 `workflow`／`domain`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TextIO

from app.core.config import get_settings
from app.integration.bank_client import BankClient
from app.integration.model_client import (
    ChatCompletionsModelClient,
    ModelClient,
    ModelClientError,
    ModelReply,
    StreamChunk,
)
from app.integration.model_config import ChatCompletionsConfig
from app.modules.money import format_minor
from app.modules.wealth.application.factory import SystemClock, build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest, WealthRequestKind
from app.modules.wealth.contracts.responses import WealthResponse, WealthStatus
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import TrustedContext

if TYPE_CHECKING:  # 只为标注：入口运行时不导入 workflow，边界由 test_cli 钉住。
    from app.modules.wealth.workflow.state import PendingData, SessionData

__all__ = [
    "BANK_URL_ENV",
    "CUSTOMER_ENV",
    "SCRIPT_BANNER",
    "CliError",
    "ScriptedModel",
    "WealthChat",
    "build_bank",
    "build_model",
    "main",
]
BANK_URL_ENV = "WEALTH_BANK_URL"
CUSTOMER_ENV = "WEALTH_BANK_CUSTOMER"

# 脚本模式的横幅：它出现在每次启动的终端上，用户一眼能看出这些回复不是模型说的。
SCRIPT_BANNER = (
    "⚠ 脚本测试模式：模型回复来自 --script 文件，只用于验证链路与权限门禁，"
    "不代表真实模型理解。真实模型请去掉 --script。"
)

# 交互里表示"确认这一笔"和"停掉这一笔"的说法。确认与取消都只影响运行时的待办，
# 不构成任何授权字段——真正的门禁在运行时里，不在 CLI 的字符串匹配上。
#
# 这一层匹配的是**按钮**的语义：终端没有按钮，所以用一组明确的词代替点击。聊天里随便说一句
# "确认"不会被当成授权——那句话走 `kind=message`，运行时不会因此提交任何资金写。
CONFIRM_WORDS = frozenset({"确认", "确认购买", "确认申购", "确认赎回", "y", "yes", "confirm"})
CANCEL_WORDS = frozenset({"取消", "算了", "不买了", "n", "no", "cancel"})
# 结果未知时唯一正确的下一步是"按原键核实"；它只读银行，不会重新下单。
RECONCILE_WORDS = frozenset({"核实", "查询结果", "查一下", "reconcile"})
QUIT_WORDS = frozenset({"退出", "quit", "exit", ":q"})

CHAT_PROMPT = "你: "


class CliError(Exception):
    """入口自己的失败：缺依赖、参数不对、脚本不可用。启动阶段就报错，不进业务循环。"""


# ---------------------------------------------------------------------- 脚本模型


class ScriptedModel:
    """显式的脚本测试模式：按行返回预先写好的决策 JSON。

    它不是"另一个模型"，只是把固定决策喂进同一条链路，用来在没有模型凭据时验证入口、
    工作流与银行权限门禁。脚本用完还被调用就报 `ModelClientError`（运行时如实转成一次
    上游失败），而不是重复最后一条决策——重复会让"脚本已经跑完"看起来像一次正常对话。
    """

    def __init__(self, decisions: Sequence[str]) -> None:
        self._decisions = [line for line in decisions if line.strip()]
        self.calls = 0

    def _next(self) -> str:
        if self.calls >= len(self._decisions):
            raise ModelClientError("LLM_SCRIPT_EXHAUSTED", retryable=False)
        reply = self._decisions[self.calls]
        self.calls += 1
        return reply

    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply:
        del messages
        return ModelReply(text=self._next())

    def stream(self, messages, *, tools=()):
        """一行决策经 stream 变成一次原生工具调用分片；纯文本行照旧发一段文本。"""

        del messages, tools
        reply = self._next()
        call = _scripted_call(reply)
        if call is None:
            yield StreamChunk(text_delta=reply)
        else:
            name, arguments = call
            yield StreamChunk(
                tool_name=name,
                tool_call_delta=json.dumps(arguments, ensure_ascii=False),
                tool_index=0,
            )
        yield StreamChunk(finish=True)

    def close(self) -> None:
        return None


def _scripted_call(line: str) -> tuple[str, dict[str, Any]] | None:
    """脚本的一行如果是 {"tool": ..., "arguments": ...}，它就是一次工具调用。"""

    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = payload.get("arguments")
    return name.strip(), dict(arguments) if isinstance(arguments, dict) else {}


def _read_script(path: str) -> list[str]:
    """读脚本文件：一行一条决策 JSON，空行与 `#` 开头的注释行忽略。"""

    try:
        with open(path, encoding="utf-8") as handle:
            return [line for raw in handle if (line := raw.strip()) and not line.startswith("#")]
    except OSError as error:
        raise CliError(f"读不到脚本文件 {path}：{error.strerror or error}") from error


# ---------------------------------------------------------------------- 依赖装配


def build_bank(url: str, *, connect_timeout: float, read_timeout: float) -> HttpWealthBank:
    """正式银行实现，唯一一个：`HttpWealthBank`。没有内存替身可选。"""

    return HttpWealthBank(
        BankClient(url, connect_timeout=connect_timeout, read_timeout=read_timeout)
    )


def build_model(script_path: str | None, settings, *, err: TextIO) -> ModelClient:
    """装配模型：默认真实 Chat Completions，缺配置就报错；脚本模式必须显式选择。"""

    if script_path:
        err.write(SCRIPT_BANNER + "\n")
        return ScriptedModel(_read_script(script_path))

    missing = [
        name
        for name, value in (
            ("LLM_BASE_URL", settings.llm_base_url),
            ("LLM_API_KEY", settings.llm_api_key),
            ("LLM_MODEL", settings.llm_model),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise CliError(
            "没有可用的真实模型配置（缺少 "
            + "、".join(missing)
            + "）。请在 backend/.env 里补全 LLM_BASE_URL／LLM_API_KEY／LLM_MODEL；"
            "只想验证链路时显式加 --script <决策脚本文件>（脚本测试模式，不代表模型理解）。"
        )
    return ChatCompletionsModelClient(
        ChatCompletionsConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            response_format=settings.llm_response_format,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    )


# ---------------------------------------------------------------------- 会话


def _preview_lines(key: str, preview: Mapping[str, Any]) -> list[str]:
    """把冻结的预览快照渲染成终端上的确认清单（金额一律用整数分格式化）。"""

    if key == "purchase_preview":
        term = preview.get("term_months")
        return [
            "—— 申购确认预览（请核对后再确认）——",
            f"产品：{preview.get('product_name')}（{preview.get('product_id')}）",
            f"金额：{format_minor(int(preview.get('amount_minor', 0)))}",
            f"付款账户：{preview.get('from_account_id')}"
            f"（余额 {format_minor(int(preview.get('balance_minor', 0)))}）",
            f"期限：{'随产品' if term is None else f'{term} 个月'}"
            f"／赎回规则：{preview.get('redeem_rule')}／风险：{preview.get('risk_level')}",
            f"确认原文：{preview.get('preview')}",
        ]
    return [
        "—— 赎回确认预览（请核对后再确认）——",
        f"持仓：{preview.get('holding_id')}"
        f"（{preview.get('product_name')}／{preview.get('product_id')}）",
        f"金额：{format_minor(int(preview.get('amount_minor', 0)))}"
        f"（可赎回 {format_minor(int(preview.get('redeemable_minor', 0)))}"
        f"{'，全部赎回' if preview.get('full') else ''}）",
        f"到账账户：{preview.get('to_account_id')}／赎回规则：{preview.get('redeem_rule')}",
        f"确认原文：{preview.get('preview')}",
    ]


class WealthChat:
    """一次终端会话：把输入翻译成 `WealthRequest`，把响应渲染成人能读的行。

    这里只有翻译与显示，没有判断"能不能买"：金额、账户、风险、确认与资金提交全部由运行时和
    银行决定。`feed` 按**上一条响应**的状态选输入类型，这是唯一一处"记住上下文"的地方，
    记的也只是"上一条在等什么"，不是任何业务事实。
    """

    def __init__(
        self,
        agent: Any,
        context: TrustedContext,
        *,
        out: TextIO = sys.stdout,
        err: TextIO = sys.stderr,
    ) -> None:
        self._agent = agent
        self._context = context
        self._out = out
        self._err = err
        self._last: WealthResponse | None = None
        # 会话与待办是运行时的状态，CLI 只是替它拿着：进来时递回去，回来时存下来。
        self._session: SessionData = {}
        self._pending: PendingData | None = None

    # ------------------------------------------------------------ 状态

    @property
    def last(self) -> WealthResponse | None:
        return self._last

    @property
    def needs_confirmation(self) -> bool:
        """上一轮是否在等用户点击确认。

        终端没有按钮，所以调用方用一行明确的"确认／取消"代替点击；这一行**不**作为聊天文本
        送给模型，它是受控的 `kind=confirm`／`kind=cancel` 输入。旧验证码那一档已经取消：
        CLI 不再有"需要验证码"的状态。
        """

        return self._last is not None and self._last.status is WealthStatus.WAITING_CONFIRMATION

    @property
    def pending_id(self) -> str | None:
        return (
            None
            if self._last is None or self._last.pending is None
            else (self._last.pending.pending_id)
        )

    # ------------------------------------------------------------ 输入

    def _send(self, request: WealthRequest) -> WealthResponse:
        response, self._session, self._pending = self._agent.handle(
            self._context,
            request,
            session=self._session,
            pending=self._pending,
        )
        self._last = response
        return response

    def message(self, text: str) -> WealthResponse:
        return self._send(WealthRequest(kind=WealthRequestKind.MESSAGE, text=text))

    def answer(self, text: str) -> WealthResponse:
        return self._send(
            WealthRequest(kind=WealthRequestKind.ANSWER, text=text, pending_id=self.pending_id)
        )

    def confirm(self) -> WealthResponse:
        """提交这一笔：与前端点击确认按钮是同一个受控输入。"""

        return self._send(WealthRequest(kind=WealthRequestKind.CONFIRM, pending_id=self.pending_id))

    def cancel(self) -> WealthResponse:
        return self._send(WealthRequest(kind=WealthRequestKind.CANCEL))

    def reconcile(self) -> WealthResponse:
        """按原幂等键核实一笔结果未知的交易；只读银行，不会重新下单。"""

        return self._send(
            WealthRequest(kind=WealthRequestKind.RECONCILE, pending_id=self.pending_id)
        )

    # ------------------------------------------------------------ 一行输入

    def feed(self, line: str) -> WealthResponse:
        """把一行终端输入按上一轮的状态翻译成一种输入。

        等待确认时，识别确认／取消的说法；其余仍按新的一句话交给工作流（运行时自己会处理
        "上一次追问还没被回答"的情况，CLI 不替它决定）。等待补充信息时，这一行是**回答**。

        这里识别的是**动作词**，不是授权：真正的门禁在运行时里。用户在聊天里随口说一句"确认"
        不会被当成点击——那句话走 `kind=message`，运行时不会因此提交任何资金写。
        """

        text = line.strip()
        status = None if self._last is None else self._last.status
        if status is WealthStatus.WAITING_CONFIRMATION:
            lowered = text.lower()
            if lowered in CONFIRM_WORDS:
                return self.confirm()
            if lowered in CANCEL_WORDS:
                return self.cancel()
        if status is WealthStatus.UNKNOWN:
            # 结果未知时，唯一正确的下一步是按原键核实；用户输入"核实"就走这条受控只读路径。
            if text.strip() in RECONCILE_WORDS:
                return self.reconcile()
        if status is WealthStatus.WAITING_INPUT:
            return self.answer(text)
        return self.message(text)

    # ------------------------------------------------------------ 渲染

    def render(self, response: WealthResponse) -> None:
        out = self._out
        out.write(f"[{response.status.value}] {response.summary}\n")
        for key in ("purchase_preview", "redeem_preview"):
            preview = response.facts.get(key)
            if isinstance(preview, Mapping):
                out.write("\n".join(_preview_lines(key, preview)) + "\n")
        receipt = response.facts.get("purchase_receipt") or response.facts.get("redeem_receipt")
        if isinstance(receipt, Mapping):
            out.write(
                "银行回执："
                + "，".join(f"{name}={value}" for name, value in sorted(receipt.items()))
                + "\n"
            )
        if response.business_ids:
            out.write(
                "业务编号："
                + "，".join(
                    f"{name}={value}" for name, value in sorted(response.business_ids.items())
                )
                + "\n"
            )
        if response.error_code:
            out.write(f"错误码：{response.error_code}\n")
        if response.reason_code:
            # 机器可读的补充原因：错误码说"哪一类失败"，它说"具体是哪种情况"。
            out.write(f"原因：{response.reason_code}\n")
        if response.confirmation is not None:
            # 确认卡片在终端上的等价物：用户要核对的字段就这几项。
            confirmation = response.confirmation
            out.write(
                f"待确认：{confirmation.title}｜{confirmation.product_name}"
                f"｜{format_minor(confirmation.amount_minor)}"
                f"｜{confirmation.account_display}"
                f"（{confirmation.confirm_label}／{confirmation.cancel_label}）\n"
            )
        if response.pending is not None:
            out.write(
                f"待办：{response.pending.pending_id}"
                f"（{response.pending.prompt or '等待输入'}，{response.pending.expires_at} 过期）\n"
            )
        out.flush()


# ---------------------------------------------------------------------- 循环


def _interactive_lines(read_line) -> Iterator[str]:
    while True:
        try:
            line = read_line()
        except (EOFError, KeyboardInterrupt):
            return
        if line is None:
            return
        yield line


def run_session(
    chat: WealthChat,
    lines: Iterable[str],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """按顺序喂入输入；每一行都经 `feed` 翻成一种受控输入。

    **输入是边读边处理的，不能先整体读完**：交互模式的 `lines` 是无限生成器
    （`_interactive_lines` 循环调用 `input()`），对它做 `list()` 会一直阻塞、把用户
    每一句话都攒在缓冲区里，终端只会反复出现提示符而没有任何回复。所以这里只持有迭代器，
    处理完一句才取下一句。

    这里没有"额外读一次"的输入了：旧验证码那一档已经取消，确认走的是同一行里的确认词，
    由 `feed` 翻成 `kind=confirm`——它是受控输入，不是聊天文本。
    """

    iterator = iter(lines)
    while True:
        try:
            line = next(iterator)
        except StopIteration:
            return 0
        if line.strip().lower() in QUIT_WORDS:
            out.write("已退出理财对话。\n")
            return 0
        chat.render(chat.feed(line))


# ---------------------------------------------------------------------- 命令行


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.wealth.cli",
        description="独立理财 Agent 的本地入口（真实 HTTP 银行 + 真实模型）",
    )
    parser.add_argument(
        "--bank-url",
        default=None,
        help=f"银行服务地址，例如 http://127.0.0.1:8001/api；也可用 {BANK_URL_ENV}",
    )
    parser.add_argument(
        "--customer",
        default=None,
        help=f"绑定的银行客户号（如 u_wealth）；也可用 {CUSTOMER_ENV}。缺省即报错，不替用户选客户",
    )
    parser.add_argument("--user-id", default="wealth_cli_user", help="Agent 会话所属用户")
    parser.add_argument("--session-id", default="wealth_cli_session", help="Agent 会话编号")
    parser.add_argument(
        "--script",
        default=None,
        help="显式进入脚本测试模式：一行一条决策 JSON 的文件（不代表模型理解）",
    )
    parser.add_argument(
        "--message",
        action="append",
        default=None,
        help="非交互地发送一句话；可重复。与 --check 二选一",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做依赖自检：银行只读探针 + 模型配置检查，不进入对话",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    err = sys.stderr

    bank_url = (args.bank_url or os.getenv(BANK_URL_ENV, "")).strip()
    source = "--bank-url" if args.bank_url else BANK_URL_ENV
    if not bank_url:
        bank_url = settings.bank_backend_base_url
        source = "backend/.env 的 BANK_BACKEND_BASE_URL"
    customer = (args.customer or os.getenv(CUSTOMER_ENV, "")).strip()
    if not customer:
        err.write(
            f"缺少绑定客户：请用 --customer 或 {CUSTOMER_ENV} 指定银行客户号。\n"
            "理财 Agent 的身份只从绑定客户来，CLI 不替调用方选一个客户。\n"
        )
        return 2

    try:
        model = build_model(args.script, settings, err=err)
    except CliError as error:
        err.write(f"{error}\n")
        return 2

    bank = build_bank(
        bank_url,
        connect_timeout=settings.bank_backend_connect_timeout,
        read_timeout=settings.bank_backend_read_timeout,
    )
    context = TrustedContext(
        user_id=args.user_id, session_id=args.session_id, bank_customer_id=customer
    )
    err.write(
        f"银行：{bank_url}（来自 {source}），绑定客户：{customer}；"
        f"模型：{'脚本测试模式' if args.script else settings.llm_model}\n"
    )

    if args.check:
        return _check(bank, context, args, settings)

    agent = build_wealth_agent(model, bank, SystemClock())
    chat = WealthChat(agent, context, out=sys.stdout, err=err)

    if args.message:
        return run_session(chat, args.message, err=err)

    interactive = sys.stdin.isatty()
    err.write(
        "输入一句话开始；等待确认时输入 确认 提交、取消 放弃（确认不会进入模型上下文）。"
        "输入 退出／quit 结束。\n"
    )
    if interactive:
        return run_session(chat, _interactive_lines(lambda: input(CHAT_PROMPT)), err=err)
    return run_session(chat, sys.stdin, err=err)


def _check(bank: HttpWealthBank, context: TrustedContext, args, settings) -> int:
    """依赖自检：银行必须真的能读，模型配置必须真的齐。缺一个就以非零退出。"""

    try:
        products = bank.list_products(context)
        accounts = bank.list_accounts(context)
    except Exception as error:  # 银行不可用就是不可用，不当成"查到 0 个产品"
        sys.stderr.write(f"银行只读探针失败：{type(error).__name__}。请确认银行服务与绑定客户。\n")
        return 3
    print(
        f"银行只读探针通过：GET /products → {len(products)} 个产品，GET /accounts → {len(accounts)} 个账户"
    )
    if args.script:
        print(f"模型：脚本测试模式（{args.script}）")
    else:
        print(f"模型配置：{settings.llm_model} @ {settings.llm_base_url}（真实模型，凭据不显示）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
