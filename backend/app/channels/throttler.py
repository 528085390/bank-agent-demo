"""打字机流式节流：一段正在生成的回复，什么时候值得往飞书推一次 PATCH。

飞书对消息编辑有频率限制，而模型的 `reply_delta` 是几个字一段地来的。来一段推一次，几百毫秒
内就能把编辑接口打满，撞上频控之后卡片会停在半句话上不再更新——用户看到的是"机器人卡住了"。
所以推送时机不能由"事件到达"决定，得由两个闸共同决定：

* **间隔闸**（默认 1500 ms）：距上次推送不足一个间隔，一律不推。这是编辑频率的硬约束。
* **增量闸**（默认 30 字符）：距上次**推送出去**的内容增长不足 30 个字符，不推。避免为几个字
  发一次 PATCH 紧接着又发一次，也少撞几次 2200（卡片更新冲突）。

两个闸都过才推；`is_final=True`（收到 final / interrupt）绕过两个闸——流结束时卡片必须落到最终
态，那是唯一一次"必须发"的推送。

调用方必须遵守的协议：

1. `should_update()` 是**纯查询**，不改状态；只有真的把内容发出去了，才调 `record_update()`。
   反过来用（先记账再发）的话，一次被网络异常吞掉的 PATCH 会让节流器以为内容已经送达，卡片
   自此永远落后一段，而且再也追不上（增量按"上次推送的内容"算）。
2. 一个 turn 一个实例：它记着"上次推出去的是哪段文字、什么时候推的"，跨 turn 复用等于把上一轮
   的基准当成这一轮的起点。
3. 它**不持有历史**：只有当前文本与一个时刻，常驻进程里不会随对话增长。
4. 2200 冲突的指数退避重试不在这里。这一层只回答"该不该发"；发失败怎么重试由 dispatcher 负责，
   否则同一句内容会被两处分别重发。
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: 刷新间隔（毫秒），设计规格 §3.4.2 的 INTERVAL_MS。
DEFAULT_INTERVAL_MS = 1500

#: 最小增量（字符数），设计规格 §3.4.2 的 MIN_DELTA_CHARS。
DEFAULT_MIN_DELTA_CHARS = 30


class StreamThrottler:
    """一次流式回答的推送节流器：只回答"现在该不该发"，不发、不重试、不记历史。"""

    __slots__ = (
        "_clock",
        "_interval_sec",
        "_last_at",
        "_last_text",
        "_max_text_chars",
        "_min_delta_chars",
    )

    def __init__(
        self,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        min_delta_chars: int = DEFAULT_MIN_DELTA_CHARS,
        *,
        max_text_chars: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`clock` 是单调秒，单列一个参数只为让用例能拨表，而不是真等 1.5 秒。

        生产上保持默认的 `time.monotonic`：墙钟会在 NTP 校时前后跳，一跳就可能让间隔闸
        卡住整整一个跳变幅度。

        `max_text_chars` 必须传渲染器的正文上限（飞书是 `render.feishu.MAX_TEXT_CHARS`）。
        渲染器会把超长正文截断，**截断之后**两张不同的长回答会长得一模一样；而这一层若按
        未截断的全文长度算增量，就会在「卡片内容一个字都没变」的情况下每 1.5 秒放行一次
        PATCH——白撞频控与 2200 冲突，直到流结束。两边口径必须一致：这里也按截断后的长度算。
        """

        if interval_ms < 0:
            raise ValueError("interval_ms 必须 >= 0：负间隔等于把频率闸拆了")
        if min_delta_chars < 0:
            raise ValueError("min_delta_chars 必须 >= 0：负增量等于把增量闸拆了")
        if max_text_chars is not None and max_text_chars < 1:
            raise ValueError("max_text_chars 必须 >= 1 或留空：0 等于把每段正文都截成空串")
        self._interval_sec = interval_ms / 1000.0
        self._min_delta_chars = min_delta_chars
        self._max_text_chars = max_text_chars
        self._clock = clock
        self._last_text: str | None = None
        self._last_at = 0.0

    @property
    def interval_ms(self) -> int:
        """当前刷新间隔（毫秒）。"""

        return round(self._interval_sec * 1000.0)

    @property
    def min_delta_chars(self) -> int:
        """当前最小增量（字符）。"""

        return self._min_delta_chars

    @property
    def max_text_chars(self) -> int | None:
        """正文可见上限（与渲染器的截断口径一致）；`None` 表示不截断。"""

        return self._max_text_chars

    @property
    def last_text(self) -> str | None:
        """上一次真正推送出去的内容；`None` 表示还一段都还没发过。"""

        return self._last_text

    def should_update(self, new_text: str, is_final: bool = False) -> bool:
        """现在该不该把 `new_text` 推给飞书。

        首个片段直接放行（没有"上次"可比，用户不该对着空卡片等首字）；文字与上次推送完全相同
        时永不放行（除了 final——那一次还要把"生成中"固化成最终态）；其余情况两个闸都过才放行。

        文本变短（重新生成、归一化）按**全文长度**算增量：负的增量说明这段文字被整体换掉了，
        长度本身就是新的信息量，不能判成"没变化"。
        """

        if is_final:
            return True
        if self._last_text is None:
            return True
        visible = self._visible(new_text)
        if visible == self._last_text:
            return False
        if self._clock() - self._last_at < self._interval_sec:
            return False
        return self._growth(visible) >= self._min_delta_chars

    def record_update(self, text: str) -> None:
        """记下"这段内容已经推出去了"。只在推送成功后调用，判定逻辑全在 `should_update`。"""

        self._last_text = self._visible(text)
        self._last_at = self._clock()

    def _visible(self, text: str) -> str:
        """用户真正看得到的那一段（渲染器会截断，见 `max_text_chars`）。"""

        if self._max_text_chars is None:
            return text
        return text[: self._max_text_chars]

    def _growth(self, new_text: str) -> int:
        """相对上次推送的净增长字符数；变短则按全文长度算（见 `should_update`）。"""

        previous = len(self._last_text or "")
        delta = len(new_text) - previous
        return len(new_text) if delta < 0 else delta
