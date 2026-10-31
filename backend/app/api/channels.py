"""渠道面的 HTTP 端点：手机核身页、核身提交，以及 Web 端的绑定码。

三个端点的共同前提是「渠道层不自己实现业务」。提交那一端只做四件事：验令牌、占用 nonce、
把四元组交给 `app.api.pending_actions.execute_pending_action_decision`，再把结果渲染成一张中文
页面。授信、幂等、点击绑定、验证码规则、银行授权和这一轮的锁全在那一个函数里——与 Web 端
的 `POST /api/conversations/{id}/pending-actions/{pid}/decide` 是同一段代码，不是第二份实现。
账本里因此也只有一套语义：同一笔操作在哪儿被确认，落下的都是同一条 `interrupt_answer`。

**页面为什么是服务端渲染的 HTML。** 短链是在手机浏览器里打开的，没有 cookie、没有
`X-Session-Id`，也不该有：那条链接本身就是凭据。所以这里不用 JSON 信封，返回的是能直接读
的页面；出错也必须是人话页面（链接失效、已经用过、验证码不对），不能是 500，也不能是把
`422` 的原样 JSON 丢给浏览器。

**验证码的可见范围。** 它从表单进、进 `verification_code` 参数、进模块、进银行的这一次写
请求，然后就没了：不写进页面（成功页只有助手最后那句话）、不写进账本、不写进审计、不进
`client_context`（那会被哈希进请求指纹并原样回放）。所以本文档里也没有任何一行日志会带它。

**幂等键怎么来的。** 提交必须带键（`execute_pending_action_decision` 没有键就直接 400），取的
顺序是：`Idempotency-Key` 请求头 → 表单字段 `idempotency_key` → 令牌里的 nonce 派生。派生的那
一档是给浏览器用的：一个纯 HTML 表单发不出请求头，而 nonce 是服务端签的、一次性、且只指向
这一笔操作，拿它当键与客户端自己 mint 一个等价。

**页面一律带硬化头。** 这几张页面里的凭据就是 URL 与隐藏字段里的那枚令牌，所以每个响应都带
`Cache-Control: no-store, private`（谁都不许把它留下来）与 `Referrer-Policy: no-referrer`
（用户从页面点出去时，令牌不会挂在 `Referer` 上泄给第三方）。
"""

from __future__ import annotations

import logging
import threading
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.api.pending_actions import execute_pending_action_decision
from app.api.routes import envelope, identity, runtime
from app.channels.binding import DEFAULT_CODE_TTL_SECONDS, ChannelBindingManager
from app.channels.code_cards import report_code_outcome
from app.channels.mfa import (
    nonce_already_spent,
    release_mfa_nonce,
    spend_mfa_nonce,
    verify_mfa_token,
)
from app.core.errors import AppError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])

#: 一个应用一个绑定管理器：绑定码只存「hmac(进程随机盐, 码)」的摘要，两个管理器各有各的盐、
#: 互不相认——网页生成的码在飞书里就永远核销不了。所以数量必须是**一**：Web 端点与渠道引擎
#: 拿到的必须是同一个对象。多个线程可能同时走到创建这一段，锁只护这一下。
_manager_lock = threading.Lock()


def channel_bindings_for(app: Any, rt: Any) -> ChannelBindingManager:
    """进程内**唯一**的那一份绑定管理器，挂在 app.state.channel_bindings 上。

    启动时由 app.main 先建一次，渠道引擎与 Web 端点之后都从这里取——它们必须是同一个对象。
    feishu_enabled 为假时启动路径照样会建它（Web 的 binding-code 端点本来就要用）；万一有一条
    没有 lifespan 的路径（例如只拿 TestClient 直接打请求）先走到这里，就在锁里补建一份并同样
    写回 app.state，于是后续无论谁来取都是这一份。
    """

    manager = getattr(app.state, "channel_bindings", None)
    if manager is not None:
        return manager
    with _manager_lock:
        manager = getattr(app.state, "channel_bindings", None)
        if manager is None:
            manager = ChannelBindingManager(rt.database.sessions, settings=rt.settings)
            app.state.channel_bindings = manager
        return manager


def channel_bindings(request: Request) -> ChannelBindingManager:
    """这个请求所属应用的那一份绑定管理器：只从 app.state 取，绝不另建第二份。"""

    return channel_bindings_for(request.app, runtime(request))


# --- 绑定码 ------------------------------------------------------------------------------


@router.post("/binding-code")
async def create_binding_code(request: Request):
    """给当前登录用户发一枚一次性绑定码。

    用户拿到六位数字之后在飞书里发 `/bind 123456`，渠道就把那个平台身份绑到他身上
    （`ChannelBindingManager.redeem_code`）。这里只负责"发给谁"和"活多久"：身份取自当前会话，
    码由管理器生成，明文只在这次响应里出现一次——不进日志、不进审计，也不回显到任何页面。
    """

    who = await identity(request)
    code = await run_in_threadpool(
        channel_bindings(request).generate_binding_code, who["user_id"], DEFAULT_CODE_TTL_SECONDS
    )
    return envelope(request, {"code": code, "expires_in": DEFAULT_CODE_TTL_SECONDS})


# --- 手机核身页 --------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(160deg, #0b3b6f 0%, #14639c 48%, #e8eef6 48%, #f6f8fb 100%);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  padding: 24px 16px; color: #12263a;
}
main.card {
  width: 100%; max-width: 420px; background: #fff; border-radius: 18px;
  box-shadow: 0 18px 44px rgba(9, 34, 61, .22); padding: 28px 24px 26px;
}
p.bank { margin: 0 0 18px; font-size: 12px; letter-spacing: .16em; color: #7b8fa6; text-transform: uppercase; }
h1 { margin: 0 0 8px; font-size: 21px; line-height: 1.35; font-weight: 600; }
h1.error { color: #b3261e; }
p.hint { margin: 0 0 22px; font-size: 14px; line-height: 1.6; color: #5b6b7c; }
label { display: block; font-size: 13px; color: #40566d; margin-bottom: 8px; }
input[type=text] {
  width: 100%; height: 52px; padding: 0 14px; font-size: 22px; letter-spacing: .34em;
  border: 1px solid #cfdae6; border-radius: 12px; background: #fbfcfe; color: #12263a;
}
input[type=text]:focus { outline: none; border-color: #14639c; box-shadow: 0 0 0 3px rgba(20, 99, 156, .14); }
button {
  width: 100%; height: 50px; margin-top: 20px; border: 0; border-radius: 12px;
  background: #14639c; color: #fff; font-size: 16px; font-weight: 600; letter-spacing: .04em;
}
button:active { background: #0b3b6f; }
p.foot { margin: 20px 0 0; font-size: 12px; line-height: 1.6; color: #8b9aa9; }
"""


def _page(*, title: str, hint: str, body: str = "", tone: str = "") -> str:
    """一张自足的移动端页面：没有外部资源、没有脚本、不含任何服务端数据。"""

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>" + escape(title) + "</title>\n<style>" + _STYLE + "</style>\n</head>\n<body>\n"
        '<main class="card">\n<p class="bank">演示银行 · 手机核身</p>\n'
        '<h1 class="' + escape(tone) + '">' + escape(title) + "</h1>\n"
        '<p class="hint">'
        + escape(hint)
        + "</p>\n"
        + body
        + '\n<p class="foot">为保障资金安全，请勿把这条链接转发给他人。</p>\n</main>\n</body>\n</html>\n'
    )


#: 每一张页面都带的硬化头。页面 URL 与隐藏字段里都是**凭据**（那条短链本身就是），所以它既
#: 不能被任何缓存留住（`no-store`，且 `private` 表明"只属于这一位用户"，中间的共享缓存别接
#: 手），也不能在用户点出去时把 URL 里的令牌通过 `Referer` 泄给第三方站点。
_PAGE_HEADERS = {"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"}


def _page_response(content: str, status: int = 200) -> HTMLResponse:
    """一张页面响应：正文加硬化头。**本模块所有页面都从这里出去**，别绕开它直接建响应。"""

    return HTMLResponse(content, status_code=status, headers=_PAGE_HEADERS)


def _error_page(status: int, title: str, hint: str) -> HTMLResponse:
    """人话错误页：状态码保留，但正文是能读的中文，而不是一个 JSON 信封。"""

    return _page_response(_page(title=title, hint=hint, tone="error"), status)


def _form(token: str) -> str:
    """验证码表单。令牌放在隐藏字段里——页面除了它不携带任何服务端 id。"""

    return (
        '<form method="post" action="/api/channels/mfa/submit">'
        '<input type="hidden" name="t" value="' + escape(token) + '">'
        '<label for="code">短信验证码</label>'
        '<input id="code" name="code" type="text" inputmode="numeric" maxlength="6"'
        ' autocomplete="one-time-code" placeholder="请输入 6 位短信验证码" required>'
        '<button type="submit">提交验证</button>'
        "</form>"
    )


_LINK_GONE_HINT = "请回到飞书重新点击确认卡片，获取一条新的验证链接。"


@router.get("/mfa", response_class=HTMLResponse)
def mfa_page(request: Request, t: str | None = None):
    """手机打开的那一页：验令牌、给验证码框。

    看页面**不**消耗令牌（用户可以刷新），只有提交才核销；这里只顺带告诉用户"这条链接已经
    用过了"，免得他白填一次验证码。
    """

    payload = verify_mfa_token(t, runtime(request).settings.secret_key)
    if payload is None:
        return _error_page(400, "这条链接已失效", _LINK_GONE_HINT)
    if nonce_already_spent(payload["nonce"]):
        return _error_page(
            410,
            "这条链接已经用过了",
            "每一张确认卡片只能验证一次。如果还需要操作，请在飞书里重新发起。",
        )
    return _page_response(
        _page(
            title="请输入短信验证码",
            hint="这笔操作需要短信验证码才能继续。请填写银行发送到您手机上的 6 位数字。",
            body=_form(t or ""),
        )
    )


# --- 手机提交 ----------------------------------------------------------------------------


async def _submission_body(request: Request) -> dict[str, str]:
    """表单或 JSON 都收：手机页面发的是 urlencoded 表单，脚本客户端发 JSON 也行。

    解析失败按空 body 处理，让后面的"令牌无效"给出那句人话——一个格式错的 POST 不该变成 500。
    """

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            raw = await request.json()
        except ValueError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(name): value for name, value in raw.items() if isinstance(value, str)}
    try:
        form = await request.form()
    except Exception:  # 畸形 body：交给"令牌无效"那条分支说话，不在这里 500
        return {}
    return {str(name): str(value) for name, value in form.items()}


def _submitted_key(request: Request, body: dict[str, str], nonce: str) -> str:
    """这次提交的幂等键：请求头 → 表单字段 → 由 nonce 派生（见模块文档）。"""

    for value in (request.headers.get("Idempotency-Key"), body.get("idempotency_key")):
        if value and value.strip():
            return value.strip()
    return "mfa-" + nonce


async def _run_turn(rt, who: dict, result) -> str:
    """把这一轮跑完，并把助手最后那句话取回来给页面显示。

    锁是调用方的：`status == "streaming"` 时 `held_lock` 由这里拥有，所以无论流正常结束、中途
    抛异常还是页面不再需要它，都在 `finally` 里释放；自动命名是这一轮唯一允许的一次，等锁
    放开之后再跑（设计规范 S-2.1）。
    """

    turns = rt.turns
    final_text = ""
    try:
        source = turns.stream(
            who["user_id"], who["session_id"], result.outcome, naming=result.naming
        )
        async for event in source:
            if event.event == "final":
                text = (event.data or {}).get("text")
                if isinstance(text, str):
                    final_text = text
    finally:
        # 回执 / 拒绝那几条路径不为这一轮拿锁（held_lock 是 None），对它们 release 就是一次
        # AttributeError——在 finally 里抛出去会盖掉本来要返回的那张页面。
        if result.held_lock is not None:
            result.held_lock.release()
        if result.naming is not None:
            await result.naming.run()
    return final_text


async def _write_back_code_card(payload: dict[str, str], text: str) -> None:
    """手机这边办完了，顺手把聊天里那条确认卡也改成结论。

    卡片由渠道进程发出、由它更新，这个端点只借道 code_cards 里记的那条句柄；所以整段都是
    尽力而为——写不回去（进程重启、卡片被删）只记一条 warning，页面该给的结论一个字都不少。
    """

    try:
        await report_code_outcome(payload["cid"], payload["pid"], text, ok=True)
    except Exception:
        logger.warning("could not write the MFA outcome back to the channel card", exc_info=True)


@router.post("/mfa/submit", response_class=HTMLResponse)
async def submit_mfa(request: Request):
    """手机页面的提交：验链接、核销 nonce，然后交给与 Web 端同一段授信状态机。

    请求体（`application/x-www-form-urlencoded` 表单，或 `application/json`）：

    - `t`：短链令牌（也可写成 `token`）；
    - `code`：用户收到的短信验证码（也可写成 `verification_code`）；
    - `idempotency_key`：可选，幂等键；缺省时依次看 `Idempotency-Key` 请求头、再由令牌里的
      nonce 派生。

    返回：成功是中文结果页（含助手最后那句话）；验证码错、卡片已经不在等状态机拒绝按原状态码
    返回中文错误页；链接失效 400；链接已经用过 200（页面说明"已经处理过"——同一枚 nonce 不会
    换来第二次授权，所以也谈不上第二笔出账）。
    """

    rt = runtime(request)
    body = await _submission_body(request)
    token = (body.get("t") or body.get("token") or "").strip()
    payload = verify_mfa_token(token, rt.settings.secret_key)
    if payload is None:
        return _error_page(400, "这条链接已失效", _LINK_GONE_HINT)
    if not spend_mfa_nonce(payload["nonce"], payload["exp"]):
        # 这枚 nonce 已经被占用：链接被转发、被重放、或用户把页面提交了两次。这里直接收口，
        # 一个字节都不写——重复提交拿到的是"已经处理过"，而不是第二笔出账。
        return _page_response(
            _page(
                title="这笔操作已经处理过",
                hint="这条验证链接此前已经完成过一次提交，系统没有重复执行。",
            )
        )
    key = _submitted_key(request, body, payload["nonce"])
    code = (body.get("code") or body.get("verification_code") or "").strip()
    try:
        # 会话换发也在 try 里：令牌指向一个已经不存在的用户时，绑定管理器抛的是
        # AppError(NOT_FOUND)，这条路径必须和其它业务拒绝走同一个出口（放回 nonce + 中文
        # 错误页）。放在 try 之外时它是唯一一条既不还 nonce 也不渲染页面的分支——手机浏览器
        # 拿到裸 JSON，而且这枚令牌从此换不来第二次尝试（再打开 410、再提交"已经处理过"）。
        who = await run_in_threadpool(
            channel_bindings(request).get_or_create_channel_session, payload["uid"]
        )
        result = await execute_pending_action_decision(
            rt=rt,
            who=who,
            conversation_id=payload["cid"],
            pending_id=payload["pid"],
            question_id=payload["qid"],
            decision="confirm",
            verification_code=code or None,
            idempotency_key=key,
        )
    except AppError as error:
        # 这次没成（验证码不对、卡片已经不在、会话不是这个人的）：把占用的 nonce 放回去，
        # 用户拿同一条链接重试仍然有效——作废点是"成功提交一次"，不是"试过一次"。
        release_mfa_nonce(payload["nonce"])
        return _error_page(error.status, "操作没有完成", error.message)
    except BaseException:
        # 不是业务拒绝（例如持久化挂了）：链接同样放回去，下一次打开还能再试。
        release_mfa_nonce(payload["nonce"])
        raise
    if result.status == "receipt":
        # 这次提交的键此前已经被花掉了：状态机给的是回执，没有第二次出账。
        return _page_response(
            _page(
                title="这笔操作已经处理过",
                hint="这条验证链接此前的提交已经完成，系统没有重复执行。",
            )
        )
    try:
        final_text = await _run_turn(rt, who, result)
    except AppError as error:
        # 这一轮已经开始写了（用户那句话与它的幂等键都已落库），但助手那句没能取回。
        # 说实话比给浏览器一个裸 500 更有用：动作已经提交，结果要回到会话里看。
        # 这时**不**放回 nonce——它们对应的提交已经生效，重试只会拿到回执。
        return _error_page(
            error.status,
            "操作已提交，但结果没能取回",
            error.message or "请回到会话里查看这笔操作的结果。",
        )
    await _write_back_code_card(payload, final_text)
    return _page_response(
        _page(
            title="操作已提交",
            hint=final_text or "这笔操作已经确认，结果可以在会话里查看。",
        )
    )
