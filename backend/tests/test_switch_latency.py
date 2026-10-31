"""一次读取只 checkout 一次连接池；登录态每次请求都查库，没有缓存捷径。

这里数的是 checkout 次数与 SQL 条数，不是墙钟耗时：数据库在公网另一头，绝对耗时本来就慢
而且抖动，所以真正要守住的不变量是这两个计数，而不是一条跑不快的耗时断言。

认证那一段是本轮刻意反向的：`Identity._cache` 已经删掉，因为一旦存在撤销路径，进程内缓存
就变成"退出登录了但某个 worker 还认"的漏洞。所以这里断言的不是"第二次不查库"，而是
"第二次仍然查库，并且撤销/过期立刻生效"。
"""

from datetime import timedelta

from sqlalchemy import event

import app.identity.service as identity_service
from app.conversation.service import request_hash


class Checkouts:
    """引擎级的连接池 checkout / SQL 计数器。

    checkout 是连接池事件，每从池里取一次连接就触发一次——即使取到的是同一个底层连接。
    所以它正好等于"这个请求付了几次跨公网复用连接的固定成本"，与耗时无关。
    """

    def __init__(self, engine) -> None:
        self.checkouts = 0
        self.queries = 0
        event.listen(engine, "checkout", self._on_checkout)
        event.listen(engine, "before_cursor_execute", self._on_query)

    def _on_checkout(self, *args) -> None:
        del args
        self.checkouts += 1

    def _on_query(self, *args) -> None:
        del args
        self.queries += 1

    def reset(self) -> None:
        self.checkouts = 0
        self.queries = 0


def shift_identity_clock(monkeypatch, delta: timedelta) -> None:
    """把 identity 模块看到的"现在"整体前移，用来在毫秒内复现"缓存期间会话过期"。

    只替换 app.identity.service 里的 datetime，不动数据库里的 expires_at：真实过期本来就是
    "墙钟走过 expires_at"，把它往前拨是最贴近真实的一种造法。
    """

    real_datetime = identity_service.datetime

    class ShiftedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + delta

    monkeypatch.setattr(identity_service, "datetime", ShiftedDatetime)


# --- 读路径：一次请求只 checkout 一次 -----------------------------------------------


def test_reading_one_conversation_checkouts_once(rt, session):
    """T-7：所有权校验与消息计数共用同一个 session，读一次只 checkout 一次。"""

    created = rt.conversations.create(session["user_id"], "转账")
    counter = Checkouts(rt.database.engine)

    view = rt.conversations.get(session["user_id"], created.conversation_id)

    assert view.conversation_id == created.conversation_id
    assert counter.checkouts == 1, "get() 只该从池里取一次连接"
    assert counter.queries == 2, "两条 SQL（所有权 + 消息计数）一条都不能少"


def test_reading_a_ledger_page_checkouts_once(rt, session):
    """T-8：所有权校验与分页查询共用同一个 session，翻一页只 checkout 一次。"""

    created = rt.conversations.create(session["user_id"], "账本")
    rt.conversations.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="switch-latency-1",
        expected_hash=request_hash(created.conversation_id, "第一句", None),
        content="第一句",
    )
    counter = Checkouts(rt.database.engine)

    page = rt.conversations.messages(session["user_id"], created.conversation_id)

    assert [item["content"] for item in page["items"]] == ["第一句"]
    assert counter.checkouts == 1, "messages() 只该从池里取一次连接"
    assert counter.queries == 2, "两条 SQL（所有权 + 分页）一条都不能少"


def test_require_writable_checkouts_once(rt, session):
    """require_writable() 曾经和 get() 一样开两个 session，这里锁住它只开一个。"""

    created = rt.conversations.create(session["user_id"], "可写")
    counter = Checkouts(rt.database.engine)

    view = rt.conversations.require_writable(session["user_id"], created.conversation_id)

    assert view.conversation_id == created.conversation_id
    assert counter.checkouts == 1, "require_writable() 只该从池里取一次连接"
    assert counter.queries == 2, "两条 SQL（所有权 + 消息计数）一条都不能少"


