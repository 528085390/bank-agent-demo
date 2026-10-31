"""启动水位线与消息 / 卡片回调防重：渠道进站的第一道闸。

两个最容易出错的数：飞书事件的 `create_time` 是 epoch **毫秒**，而进程启动时间
`time.time()` 是 epoch **秒**。少乘一次 1000 会把每条新消息都判成历史（机器人从此
一声不吭），多乘一次则会把重启前的离线堆积全放进来（一轮重放风暴）。所以换算只在一
处做：先把水位线折成毫秒，再和整型毫秒直接比。反方向的错也拦着：真有人把 10 位数的
秒值当毫秒传进来，`is_historical` 会抛 `ValueError` 而不是静默判成「全是历史」。

防重缓存必须是**有界**的。卡片按钮狂击、飞书网络重投都在持续产生新键，常驻进程里一个
只增不减的 dict 就是慢性内存事故。这里给每个键一个到期时间（消息 60 s、卡片 30 s，
取自设计规格 §3.2），容量满时先丢最久没被命中的那个。

**键由本类生成，调用方不许自己拼。** `message_keys` / `action_key` 是唯一的键来源：
调用方手搓一个 per-click 的键（例如拿 `action_id`、或干脆 `uuid4()`），防重就永远不命中，
狂点三十秒会变成三十秒的重复出账。这条不是风格问题，是正确性问题。

**同一个进程里必须共享同一个 `ChannelDedup` 实例。** 每个实例各有一份水位线与键表，
new 两个等于两套防重各记各的，等于没有防重；同一把键在 A 实例记过、B 实例照样放行。
该实例在 Task 8 的 lifespan 里建**一次**（`time.time()` 就是那一刻的启动时刻），随应用
状态传给 dispatcher 与 engine，回调里不许就地 new 一个。

调用约定（本类答不了的那半个语义）：它只回答「这个键我见过」，**不知道业务有没有处理成功**。
所以顺序必须是 先判重 → 处理 → 成功（什么也不做，键会自己过期）/ **失败就撤**：
消息用 `forget_message`、点击用 `forget_click`、手里只有裸键时才用底层的 `forget`。
少了最后一步，一次失败就把这条消息永久吞掉——飞书重投回来仍被认成重复，静默 ACK，用户的
消息凭空消失。

线程 / 协程安全：方法本身是同步的，没有 await 让出点，但 lark-oapi 的 `ws.Client.start()`
按规格跑在 `asyncio.to_thread` 的工作线程里，回调落点不保证在事件循环线程；更重要的是
「先查后写」不是原子操作——两次并发狂击都可能在对方写入前查到「没见过」，那正是重复出账
的窗口。因此每次判定都包在一把 `threading.Lock` 里。

锁内**不保证** O(1)：命中是两次哈希操作，但容量满时 `_make_room` 会扫一遍全部条目清掉
过期键（O(n)，n 上限 `DEFAULT_MAX_ENTRIES` = 2000），这一步也在锁里。2000 次字典遍历
远小于一次飞书 API 往返，可以接受；但别把它当 O(1) 去在超热路径上反复调，也别在持锁期间
做 I/O——那才是真的会把回调线程卡住。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from app.channels.contracts import CardActionClick, InboundMessage

#: 水位线容差：飞书云端与本机时钟允许的偏差（设计评审 Review Focus 第 1 条）。
DEFAULT_TOLERANCE_SEC = 5.0

#: 消息防重重投窗口；飞书的失败重试以秒级为主，60 s 足够覆盖。
DEFAULT_MESSAGE_TTL_SEC = 60.0

#: 卡片按钮防重窗口；手机端狂击发生在数百毫秒内，30 s 已远超需求。
DEFAULT_ACTION_TTL_SEC = 30.0

#: 单个域的容量上限（两个域各一份额度，互不吃对方的余量）。
DEFAULT_MAX_ENTRIES = 2000

#: 比这更小的正数毫秒时间戳一律当作「传错单位」：100_000_000_000 ms 是 1973-03，
#: 1969 年至今任何真实的 epoch 毫秒都远大于它，而 10 位数的秒值一定小于它。
_MIN_PLAUSIBLE_EPOCH_MS = 100_000_000_000


class _DedupWindow:
    """一个有界的「最近见过」窗口：键在 [插入时刻, 插入时刻 + ttl) 内算重复。

    `OrderedDict` 的迭代顺序就是淘汰顺序——命中时 `move_to_end` 把热键排到最新，容量
    满时从头部丢最久没被用过的键，于是「LRU 淘汰」和「按插入时间清理过期」共用同一份顺序。

    TTL 用「首次出现 + ttl」的固定窗口，而不是每次命中都重置：滑动窗口会让一个被反复
    重投的键永远不过期，而反复重投恰恰是这里最想拦住的那类流量。
    """

    __slots__ = ("_entries", "_max_entries", "_ttl_sec")

    def __init__(self, ttl_sec: float, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries 必须 >= 1，否则容量淘汰会立刻清空窗口")
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._ttl_sec = ttl_sec
        self._max_entries = max_entries

    def seen(self, key: str, now: float) -> bool:
        """这个键是否已在窗口内出现过；没出现过就记账并返回 False。"""

        expires_at = self._entries.get(key)
        if expires_at is not None:
            if expires_at > now:
                self._entries.move_to_end(key)
                return True
            # 过期即视为从没见过：允许它作为「新事件」重新入队。
            del self._entries[key]

        if len(self._entries) >= self._max_entries:
            self._make_room(now)
        self._entries[key] = now + self._ttl_sec
        return False

    def forget(self, key: str) -> None:
        """把键抹掉（不存在就什么也不做）：业务失败后让重投能被当成新事件。"""

        self._entries.pop(key, None)

    def _make_room(self, now: float) -> None:
        """腾位置：先把已经过期的清掉（顺手，不必等它们被访问），再从最旧的开始挤。"""

        self._entries = OrderedDict(
            (key, expires_at) for key, expires_at in self._entries.items() if expires_at > now
        )
        while len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)


class ChannelDedup:
    """启动水位判定 + 消息 / 卡片回调去重（进程内**共享唯一**实例，由渠道引擎持有）。

    两个域各有一份独立的窗口与额度：消息重投和按钮狂击是两种危害，用同一把键也不该
    互相抵消——一条消息被投两次不能顺带把同 id 的按钮点击判成重复，反之亦然。
    """

    __slots__ = (
        "_action_ttl_sec",
        "_action_window",
        "_boot_time",
        "_clock",
        "_lock",
        "_max_entries",
        "_message_ttl_sec",
        "_message_window",
        "_tolerance_sec",
    )

    def __init__(
        self,
        boot_time: float,
        tolerance_sec: float = DEFAULT_TOLERANCE_SEC,
        *,
        message_ttl_sec: float = DEFAULT_MESSAGE_TTL_SEC,
        action_ttl_sec: float = DEFAULT_ACTION_TTL_SEC,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`boot_time` 传 `time.time()`（epoch 秒），与事件时间同一坐标系。

        `clock` 是防重窗口用的单调时钟，单列一个参数是为了让 TTL 用例能拨表，不必真等
        60 秒；生产上保持默认的 `time.monotonic`——它不受系统校时跳变影响，而
        `time.time()` 会在 NTP 回调时前后跳，足以让窗口忽长忽短。

        四个窗口参数都在这里把关：ttl 配成 0 或负数等于**静默关掉防重**（不会有任何报错，
        也没有任何日志，只是从此每条消息都进得来），上限配成 0 则是每插一个键就清空一次
        窗口。这类「配错了但看起来正常」必须在构造时就炸掉。
        """

        if tolerance_sec < 0:
            raise ValueError(
                f"tolerance_sec 必须 >= 0（秒），收到 {tolerance_sec!r}："
                "负数会把水位线推到启动之后，历史堆积反而全部放行"
            )
        if message_ttl_sec <= 0:
            raise ValueError(
                f"message_ttl_sec 必须 > 0（秒），收到 {message_ttl_sec!r}："
                "<= 0 等于关掉消息防重，重投会原样跑成一模一样的第二轮对话"
            )
        if action_ttl_sec <= 0:
            raise ValueError(
                f"action_ttl_sec 必须 > 0（秒），收到 {action_ttl_sec!r}："
                "<= 0 等于关掉按钮防重，狂点卡片会变成重复出账"
            )
        if max_entries <= 0:
            raise ValueError(
                f"max_entries 必须 >= 1，收到 {max_entries!r}：<= 0 会让容量淘汰立刻清空窗口"
            )

        self._boot_time = boot_time
        self._tolerance_sec = tolerance_sec
        self._message_ttl_sec = message_ttl_sec
        self._action_ttl_sec = action_ttl_sec
        self._max_entries = max_entries
        self._message_window = _DedupWindow(message_ttl_sec, max_entries)
        self._action_window = _DedupWindow(action_ttl_sec, max_entries)
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def boot_time(self) -> float:
        """启动时刻（epoch 秒），供日志与离线探针核对水位。"""

        return self._boot_time

    @property
    def tolerance_sec(self) -> float:
        """实例默认容差（秒）。"""

        return self._tolerance_sec

    @property
    def message_ttl_sec(self) -> float:
        """消息防重窗口（秒）。"""

        return self._message_ttl_sec

    @property
    def action_ttl_sec(self) -> float:
        """卡片按钮防重窗口（秒）。"""

        return self._action_ttl_sec

    @property
    def max_entries(self) -> int:
        """单个域的容量上限。"""

        return self._max_entries

    def is_historical(self, create_time_ms: int, tolerance_sec: float | None = None) -> bool:
        """事件是否早于「启动水位 − 容差」，即服务起来之前就躺在飞书队列里的离线堆积。

        `tolerance_sec` 传 None 用实例默认值（默认 5 s）；显式传值只影响这一次判定，
        给补跑历史事件之类需要按 0 容差严格判的场景留口子。

        等号算「不历史」：规格说的是「早于才丢」，而两侧代价不对称——误丢一条真消息，
        用户发完话得不到任何回应；误放一条略旧的消息，最多多答一轮，且它紧接着还要过
        一遍消息去重。所以边界留在容差窗口内侧，保消息。

        `tolerance_sec` 与构造函数同一个标准：负数一律 `ValueError`。绕过构造函数走这条
        参数不算「另一个口子」——把容差配成负数会把水位线推到启动之后，重启前堆积的每条
        消息都会被当成刚发生，正是水位线本来要挡掉的那场重放风暴。

        两个非正数的特例，方向相反、都是「保消息」：

        - `create_time_ms <= 0` 当作平台没给时间，**不算历史**（宁可多回一条，也不要
          因为一个缺失字段就把消息静默丢掉）；
        - 但 `0 < create_time_ms < 100_000_000_000` 当作**传成秒了**，抛 `ValueError`。
          10 位数的秒值比水位小一千倍，只比大小的话它会被判成历史，于是每条消息都被静默
          ACK、日志里一个字都没有——那是最难查的一类故障，所以宁可在这里炸响。
        """

        if create_time_ms <= 0:
            return False
        if create_time_ms < _MIN_PLAUSIBLE_EPOCH_MS:
            raise ValueError(
                f"create_time_ms={create_time_ms} 看起来是秒，本 API 要毫秒"
                f"（1969 年至今的 epoch 毫秒都 >= {_MIN_PLAUSIBLE_EPOCH_MS}）"
            )

        tolerance = self._tolerance_sec if tolerance_sec is None else tolerance_sec
        if tolerance < 0:
            raise ValueError(
                f"tolerance_sec 必须 >= 0（秒），收到 {tolerance!r}："
                "负数会把水位线推到启动之后，历史堆积反而全部放行"
            )

        watermark_ms = (self._boot_time - tolerance) * 1000.0
        return create_time_ms < watermark_ms

    # --- 消息 ---------------------------------------------------------------------------

    def message_keys(self, msg: InboundMessage) -> tuple[str, str]:
        """这条消息的两把键：(事件键, 消息键)，形如 `feishu:event:{event_id}` / `feishu:message:{message_id}`。

        为什么要两把：飞书重投时 `event_id` 可能变而 `message_id` 不变，同一事件也可能被
        拆成两条 `message_id` 不同的记录。只认其中一把，总有一类重投漏过去。
        `channel` 进键：第二个渠道接进来时，两边的 id 撞车不能互相吞消息。
        """

        return (f"{msg.channel}:event:{msg.event_id}", f"{msg.channel}:message:{msg.message_id}")

    def is_duplicate_message_obj(self, msg: InboundMessage) -> bool:
        """这条消息是否已经见过——**任一**键命中就算重复。

        首次看见返回 False 并记住两把键，第二次起返回 True，调用方据此静默 ACK、不再跑一轮
        对话。两把键都会记账，所以「event 键先命中」的那次之后，再拿 message 键来判断也
        仍然命中，不会因为走了不同的调用路径就漏掉。
        """

        event_key, message_key = self.message_keys(msg)
        with self._lock:
            now = self._clock()
            seen_event = self._message_window.seen(event_key, now)
            seen_message = self._message_window.seen(message_key, now)
        return seen_event or seen_message

    def is_duplicate_message(self, key: str) -> bool:
        """这条消息是否在 60 s 窗口里见过（键形如 `feishu:message:{message_id}`）。

        首次看见返回 False 并记住，第二次起返回 True——调用方据此静默 ACK，不再跑一轮
        对话。窗口过期或键被容量淘汰后重新算「第一次见」。

        这是按裸键判定的底层接口，键应当来自 `message_keys`；手搓的键会让防重永远不命中。
        """

        return self._remember(self._message_window, key)

    # --- 卡片按钮 -----------------------------------------------------------------------

    def action_key(self, click: CardActionClick) -> str:
        """这次点击的防重键，形如 `feishu:action:{question_id}:{decision}`（规格 §3.2）。

        **`pending_id` 故意不进键。** 同一张卡片重发一次，`pending_id`（和 `action_id`）就会换，
        把它们放进键等于「重发一次卡片就把防重清零」；而狂点三十秒恰恰横跨卡片重发——用户
        根本没意识到中间刷过一次卡，第二次点仍然必须被挡住。键建在「哪张卡片的哪个决定」上，
        它才是用户视角的同一件事。
        """

        return f"{click.channel}:action:{click.question_id}:{click.decision}"

    def is_duplicate_click(self, click: CardActionClick) -> bool:
        """这次按钮点击是否在 30 s 窗口里见过；键由 `action_key` 生成，调用方不要自己拼。

        手机端 500 ms 内连击两次【确认转账】时，第二次必须在这里就被拦住，不能走到授信。
        """

        return self.is_duplicate_action(self.action_key(click))

    def is_duplicate_action(self, key: str) -> bool:
        """这次卡片按钮点击是否在 30 s 窗口里见过（键形如 `feishu:action:{question_id}:{decision}`）。

        按裸键判定的底层接口；正常路径请用 `is_duplicate_click`。
        """

        return self._remember(self._action_window, key)

    # --- 撤销 ---------------------------------------------------------------------------

    def forget(self, key: str) -> None:
        """抹掉这把键（消息域与动作域一起、同一把锁内），之后它重新算「第一次见」。

        调用方在**处理失败**之后调它：本类只能回答「见过」，答不了「处理成功没有」，键留着
        就等于把这条消息判了死刑——飞书重投回来仍被静默吞掉。键不存在是无害的空操作。

        正常路径不要调它，改用 `forget_message` / `forget_click`：那两条自己会拼键，
        而「调用方自己拼键」正是这个类想堵掉的那类错误。
        """

        self._forget_keys(key)

    def forget_message(self, msg: InboundMessage) -> None:
        """抹掉这条消息的两把键（`forget` 的语义化包装），让重投能真正重来。

        两把键都从**两个域**里清（与 `forget` 同一条路径）：正常路径下消息键与动作键不会
        撞，但只要某处误把一把键记进了另一个域，撤销就该撤到底，而不是留一半继续吞重投。
        """

        self._forget_keys(*self.message_keys(msg))

    def forget_click(self, click: CardActionClick) -> None:
        """抹掉这次点击的键（`forget` 的语义化包装），让用户重试能真正再进一次。

        撤销不该成为「让调用方自己拼键」的后门：拿 `action_id` / `pending_id` 拼出来的键
        与 `action_key` 不是同一把，撤了个空的，那次失败的点击会一直挡着用户——用户点一次
        没反应，再点被判成重复，日志里一个字都没有。
        """

        self._forget_keys(self.action_key(click))

    def _forget_keys(self, *keys: str) -> None:
        """锁内把若干把键从两个域里都抹掉；不存在的键是无害的空操作。"""

        with self._lock:
            for key in keys:
                self._message_window.forget(key)
                self._action_window.forget(key)

    def _remember(self, window: _DedupWindow, key: str) -> bool:
        with self._lock:
            return window.seen(key, self._clock())
