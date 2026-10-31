"""节流器与飞书卡片渲染器的契约：什么时候推一次 PATCH，以及推出去的那张卡片长什么样。

两条断言值得单独说明，因为它们的失败方式都是「看不见的」：

1. 按钮回传值必须是**同一份** `{"q": ..., "p": ..., "d": ...}`。卡片会被重渲染（节流刷新、
   中断后再刷、确认之后 PATCH 成回执），如果这里的值掺进时间戳、随机数或渲染序号，同一笔待确认
   操作在两次渲染里就会得到两个不同的回传值——防重缓存按值建键，两个键都不重复，于是手机端连击
   两次【确认】就成了两次真实出账。所以这里钉死：键只有 q/p/d，值只来自入参，两次渲染逐字节相同。

2. 卡片 JSON 里不许出现验证码。渲染器本来就拿不到验证码，但 `pending_dict` 是别处传进来的
   dict：一旦上游把一条带 code 的探针数据塞进来，唯一能挡住它的地方就是这里——要素按白名单投影，
   未知键一律不渲染。

节流器的用例全部拨表（注入时钟），不真等 1.5 秒：这一层被钉住的是算术，不是等待。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.channels.render.base import BaseRenderer, CardStatus, format_minor
from app.channels.render.feishu import (
    MAX_TEXT_CHARS,
    TRUNCATED_NOTICE,
    FeishuCardRenderer,
    confirmation_action_value,
)
from app.channels.throttler import (
    DEFAULT_INTERVAL_MS,
    DEFAULT_MIN_DELTA_CHARS,
    StreamThrottler,
)

# --- 节流器 ---------------------------------------------------------------------------


class FakeClock:
    """可手拨的单调时钟：1.5 秒的间隔要在两条断言之间「跳过」，而不是真等。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_the_first_frame_always_goes_out():
    """第一次没有「上次」，无从比较：直接推，否则用户看着一张空卡片等首字。"""

    throttler = StreamThrottler(clock=FakeClock())

    assert throttler.should_update("Hello", is_final=False) is True


def test_a_small_delta_inside_the_interval_is_held_back():
    """任务书用例：刚推过 "Hello" 就多出 6 个字符，1.5 秒内不该再发一次 PATCH。"""

    clock = FakeClock()
    throttler = StreamThrottler(interval_ms=1500, min_delta_chars=30, clock=clock)

    assert throttler.should_update("Hello", is_final=False) is True
    throttler.record_update("Hello")

    assert throttler.should_update("Hello World", is_final=False) is False


def test_final_is_always_forced_even_inside_the_interval():
    """final 不受任何闸限制：流结束时卡片必须落到最终态，哪怕上一帧刚推过。"""

    clock = FakeClock()
    throttler = StreamThrottler(interval_ms=1500, min_delta_chars=30, clock=clock)

    throttler.record_update("Hello")

    assert throttler.should_update("Hello World", is_final=True) is True


def test_final_is_still_forced_when_the_text_did_not_change():
    """文字没变也要落一次：最终还要把「生成中」固化掉（改状态、补流水号）。"""

    throttler = StreamThrottler(clock=FakeClock())
    throttler.record_update("已经说完的话")

    assert throttler.should_update("已经说完的话", is_final=True) is True


def test_an_unchanged_text_is_never_pushed_twice():
    """同样的文字推第二次只会换来一次无意义的 PATCH（和一次 2200 冲突的风险）。"""

    clock = FakeClock()
    throttler = StreamThrottler(clock=clock)
    throttler.record_update("同一段话")

    clock.advance(60.0)
    assert throttler.should_update("同一段话", is_final=False) is False


def test_the_interval_alone_is_not_enough_without_the_minimum_delta():
    """时间到了但只多了几个字：攒着，等够 30 个字符再推——否则长时间低速流会把编辑接口打满。"""

    clock = FakeClock()
    throttler = StreamThrottler(interval_ms=1500, min_delta_chars=30, clock=clock)
    throttler.record_update("字" * 100)

    clock.advance(1.5)
    assert throttler.should_update("字" * 105, is_final=False) is False

    clock.advance(1.5)
    assert throttler.should_update("字" * 130, is_final=False) is True


def test_the_minimum_delta_alone_is_not_enough_inside_the_interval():
    """一次涌来 200 个字符也不能立刻推：间隔是飞书编辑频率的硬闸，不是建议值。

    这一条挡的是真实故障：模型突发吐出几百字时，若「增量够大就推」，几百毫秒内会连发几十次
    PATCH，撞上飞书的频率限制（2200 / 频控），卡片从此停在半句话上不再更新。
    """

    clock = FakeClock()
    throttler = StreamThrottler(interval_ms=1500, min_delta_chars=30, clock=clock)
    throttler.record_update("开头")

    clock.advance(0.4)
    assert throttler.should_update("开头" + "后文" * 100, is_final=False) is False


def test_a_rewritten_text_counts_as_a_full_change():
    """文本变短（重新生成 / 归一化）时不能按「增量 -80」判成没变化。

    负增量说明这段文字被整体换掉了，长度本身就是新的信息量：按全文长度算，间隔到了就推。
    """

    clock = FakeClock()
    throttler = StreamThrottler(interval_ms=1500, min_delta_chars=30, clock=clock)
    throttler.record_update("旧的" * 50)

    clock.advance(1.5)
    assert throttler.should_update("换掉的一整段" * 10, is_final=False) is True


def test_should_update_is_a_pure_query():
    """判定不改状态：真正推进窗口的是 record_update。

    否则一次被网络异常吞掉的 PATCH 也会让节流器以为「已经发出去了」，卡片与用户所见从此错位。
    """

    clock = FakeClock()
    throttler = StreamThrottler(interval_ms=1500, min_delta_chars=30, clock=clock)
    throttler.record_update("已发出的内容")
    # 增量 40 个字符（远超 30）：唯一拦得住它的是间隔闸。
    pending = "已发出的内容" + "加" * 40

    for _ in range(5):
        assert throttler.should_update(pending, is_final=False) is False

    # 判定没有推进窗口：时间一到，同样是这段文字立刻放行。
    clock.advance(1.5)
    assert throttler.should_update(pending, is_final=False) is True


def test_the_defaults_are_the_ones_in_the_spec():
    """规格 §3.4.2 的两个数：1500 毫秒刷新间隔、30 字符最小增量。"""

    throttler = StreamThrottler()

    assert DEFAULT_INTERVAL_MS == 1500
    assert DEFAULT_MIN_DELTA_CHARS == 30
    assert throttler.interval_ms == 1500
    assert throttler.min_delta_chars == 30


def test_negative_arguments_are_refused():
    """负间隔等于「每次都推」，负增量等于「任何变化都推」——两者都是把闸拆了，别让它悄悄成立。"""

    with pytest.raises(ValueError):
        StreamThrottler(interval_ms=-1)
    with pytest.raises(ValueError):
        StreamThrottler(min_delta_chars=-1)


def test_the_throttler_keeps_no_history():
    """渠道是常驻进程：节流器只许记住「当前这段文字 + 上次推送时刻」，不许攒流水。"""

    clock = FakeClock()
    throttler = StreamThrottler(clock=clock)

    for step in range(500):
        throttler.record_update("字" * (step + 1))
        clock.advance(0.001)

    for name in StreamThrottler.__slots__:
        value = getattr(throttler, name)
        if isinstance(value, (list, tuple, dict, set, bytearray)):
            assert len(value) <= 1

    assert throttler.last_text == "字" * 500


# --- 卡片渲染：取件工具 -----------------------------------------------------------------


def buttons_of(card: dict[str, Any]) -> list[dict[str, Any]]:
    """卡片里所有 button 元素（含嵌在 column_set 等容器里的），深度遍历。"""

    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return found


def callback_values(card: dict[str, Any]) -> list[dict[str, Any]]:
    """卡片上所有回调按钮回传的 value（飞书 2.0 把回调值放在 behaviors 里）。"""

    values: list[dict[str, Any]] = []
    for button in buttons_of(card):
        for behavior in button.get("behaviors") or []:
            if behavior.get("type") == "callback":
                values.append(behavior["value"])
    return values


def url_targets(card: dict[str, Any]) -> list[str]:
    """卡片上所有「点开一个链接」按钮的目标地址。"""

    targets: list[str] = []
    for button in buttons_of(card):
        for behavior in button.get("behaviors") or []:
            if behavior.get("type") == "open_url":
                targets.append(behavior["default_url"])
    return targets


def markdown_of(card: dict[str, Any]) -> str:
    """卡片里所有 markdown 元素的文字，拼起来便于断言「卡片上写了什么」。"""

    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "markdown":
                chunks.append(str(node.get("content") or ""))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return "\n".join(chunks)


def column_cells(card: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """双列要素面板：每列各自的 elements。"""

    for element in card["body"]["elements"]:
        if element.get("tag") == "column_set":
            return [column["elements"] for column in element["columns"]]
    return []


def confirmation_card(mfa_url: str | None = None, **overrides: Any) -> dict[str, Any]:
    """一张最普通的小额转账确认卡；`overrides` 落进载荷，`mfa_url` 走渲染器的参数。"""

    pending: dict[str, Any] = {
        "operation": "transfer",
        "amount": "500.00",
        "payee_name": "李四",
        "payee_account": "6222 0000 0000 6001",
        "fee": "0.00",
    }
    pending.update(overrides)
    return FeishuCardRenderer().render_confirmation_card(
        pending_dict=pending, question_id="q123", pending_id="p456", mfa_url=mfa_url
    )


# --- 卡片渲染：确认卡 -------------------------------------------------------------------


def test_confirmation_card_carries_schema_2_0_and_the_operation_title():
    """任务书用例：Schema 2.0、标题「操作确认：转账汇款」、两个动作都在。"""

    card = confirmation_card()

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "操作确认：转账汇款"
    assert "confirm" in str(card)
    assert "cancel" in str(card)


def test_a_live_card_can_be_patched_in_place():
    """流式与 3 秒响应都靠「同一个消息原地更新」：卡片必须声明可多次更新。"""

    card = confirmation_card()

    assert card["config"]["update_multi"] is True
    assert isinstance(card["body"]["elements"], list) and card["body"]["elements"]


def test_the_two_actions_are_the_stable_click_contract():
    """回传值只有 q / p / d 三个键，值只来自入参——这是防重与点击绑定共用的那把键。"""

    values = callback_values(confirmation_card())

    assert values == [
        {"q": "q123", "p": "p456", "d": "confirm"},
        {"q": "q123", "p": "p456", "d": "cancel"},
    ]
    for value in values:
        assert set(value) == {"q", "p", "d"}


def test_two_renders_of_the_same_pending_action_are_byte_identical():
    """重渲染不许改变回传值：卡片上的值一旦漂移，同一笔操作的两次点击就成了两次出账。"""

    first = confirmation_card()
    second = confirmation_card()

    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_an_unknown_key_in_the_pending_payload_never_reaches_the_card():
    """要素按白名单投影：上游误塞进来的验证码 / 令牌 / 快照原文，一个都不许出现在卡片上。"""

    card = confirmation_card(
        verification_code="123456",
        code="123456",
        verification_token="tok_do_not_render",
        cvc="987",
    )
    dumped = json.dumps(card, ensure_ascii=False)

    for leak in ("123456", "tok_do_not_render", "987"):
        assert leak not in dumped
    assert "收款人" in markdown_of(card)


def test_confirmation_fields_are_laid_out_in_two_columns():
    """要素双列展示：收款人 / 账号 / 金额 / 手续费都要出现，金额按分换算成人看得懂的写法。"""

    card = confirmation_card()
    columns = column_cells(card)

    assert len(columns) == 2
    texts = [element["content"] for column in columns for element in column]
    assert any("收款人" in text and "李四" in text for text in texts)
    assert any("金额" in text and "¥500.00" in text for text in texts)
    assert any("手续费" in text for text in texts)
    assert any("收款账号" in text for text in texts)


def test_an_amount_in_minor_units_is_formatted_as_money():
    """真实载荷给的是 `amount_minor=500000`（整数分），卡片上要写成 ¥5,000.00。"""

    card = FeishuCardRenderer().render_confirmation_card(
        pending_dict={"operation": "purchase_product", "amount_minor": 500000},
        question_id="q1",
        pending_id="p1",
    )

    assert "¥5,000.00" in markdown_of(card)
    assert card["header"]["title"]["content"] == "操作确认：购买理财"


def test_the_wrapped_interrupt_payload_renders_the_same_card():
    """引擎手里那份 `hint.pending` 是带 kind / confirmation 的信封，渲染器要认它。"""

    inner = {"operation": "transfer", "amount": "500.00", "payee_name": "李四"}
    renderer = FeishuCardRenderer()

    wrapped = renderer.render_confirmation_card(
        pending_dict={"kind": "confirmation", "question_id": "q123", "confirmation": inner},
        question_id="q123",
        pending_id="p456",
    )
    bare = renderer.render_confirmation_card(
        pending_dict=inner, question_id="q123", pending_id="p456"
    )

    assert wrapped == bare


def test_the_operation_wording_replaces_a_generic_module_label():
    """模块只给了默认的「确认」时，按钮写业务化的「确认转账」——用户要知道自己按下去的是什么。"""

    card = confirmation_card(confirm_label="确认", cancel_label="取消")
    labels = [button["text"]["content"] for button in buttons_of(card)]

    assert labels == ["确认转账", "取消操作"]


def test_a_card_that_needs_a_code_offers_the_signed_link_instead_of_a_dead_button():
    """要验证码的卡片：确认按钮必须是跳转签名短链，而不是一个点了必然被拒的回调按钮。"""

    mfa_url = "http://127.0.0.1:8000/api/channels/mfa?t=signed-token"
    card = confirmation_card(requires_code=True, mfa_url=mfa_url)

    assert url_targets(card) == [mfa_url]
    assert [value["d"] for value in callback_values(card)] == ["cancel"]
    assert "验证码" in markdown_of(card)


def test_a_card_that_needs_a_code_without_a_link_offers_no_confirm_button():
    """没有短链就一个确认入口都不给（不能让人点一个注定 422 的按钮），只留取消。"""

    card = confirmation_card(requires_code=True)

    assert url_targets(card) == []
    assert [value["d"] for value in callback_values(card)] == ["cancel"]
    assert "验证码" in markdown_of(card)


def test_a_non_http_mfa_url_is_refused():
    """短链地址来自配置：非 http(s) 的地址等于把一段可以放任意协议的内容交给客户端。"""

    with pytest.raises(ValueError):
        confirmation_card(requires_code=True, mfa_url="javascript:alert(1)")


def test_the_action_value_builder_refuses_an_unknown_decision():
    """回传值是唯一的构造点，越界的决定在这里就被挡住，不靠调用方自觉。"""

    assert confirmation_action_value("q1", "p1", "confirm") == {
        "q": "q1",
        "p": "p1",
        "d": "confirm",
    }
    with pytest.raises(ValueError):
        confirmation_action_value("q1", "p1", "maybe")


# --- 卡片渲染：文本卡与中间态 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "template"),
    [
        ("streaming", "blue"),
        ("pending", "orange"),
        ("processing", "blue"),
        ("success", "green"),
        ("cancelled", "grey"),
        ("failed", "red"),
    ],
)
def test_the_status_drives_the_header_colour(status, template):
    """状态到色彩的映射是给人看的：待确认橙色、处理中蓝色、成功绿色、取消灰色、失败红色。"""

    card = FeishuCardRenderer().render_text_card("查询活期余额", "余额 ¥8,320.00", status=status)

    assert card["header"]["template"] == template
    assert card["header"]["title"]["content"] == "查询活期余额"


def test_an_unknown_status_is_refused():
    """状态写错要当场炸，而不是渲染成一张颜色随便的卡片（颜色是用户判断结果的唯一线索）。"""

    with pytest.raises(ValueError):
        FeishuCardRenderer().render_text_card("标题", "正文", status="done")


def test_the_text_card_is_json_and_truncates_an_oversized_answer():
    """回答可以被截断，但不能让 PATCH 直接失败：超长文本按上限截断并留一句提示。

    提示串自己也算长度：最终正文必须**不超过** MAX_TEXT_CHARS。先截满再追加提示那种写法每次
    都稳稳超限一点点，而节流器是按"前 MAX_TEXT_CHARS 个字"判增量的——两边口径一错开，对同一
    张卡的刷新就会被判成"又长了一大段"，于是白撞频控与 2200。
    """

    text = "啊" * (MAX_TEXT_CHARS + 500)
    card = FeishuCardRenderer().render_text_card("余额", text)
    rendered = markdown_of(card)
    # 正文是第一个 markdown 要素（第二个是灰色的状态页脚）。
    body = card["body"]["elements"][0]["content"]

    assert json.loads(json.dumps(card, ensure_ascii=False))["schema"] == "2.0"
    assert len(rendered) < len(text)
    assert "已截断" in rendered
    assert body.endswith(TRUNCATED_NOTICE)
    # 与 throttler._visible 同一个上限：可见正文一个字都不许超，而且不浪费位置。
    assert len(body) == MAX_TEXT_CHARS


def test_the_intermediate_card_cannot_be_clicked_again():
    """3 秒同步响应返回的中间态：蓝色、说清在干什么、没有任何可点的回调（否则连击就重复出账）。"""

    card = FeishuCardRenderer().render_intermediate_card(
        "正在处理转账...", "已收到您的确认，正在核验。"
    )

    assert card["header"]["template"] == "blue"
    assert callback_values(card) == []
    assert url_targets(card) == []
    assert all(button.get("disabled") is True for button in buttons_of(card))
    assert buttons_of(card)


def test_the_feishu_renderer_satisfies_the_shared_contract():
    """三个渲染方法就是渠道间的共同接口：换渠道时引擎这一侧不该改。"""

    renderer = FeishuCardRenderer()

    assert isinstance(renderer, BaseRenderer)
    assert set(CardStatus) == {
        CardStatus.STREAMING,
        CardStatus.PENDING,
        CardStatus.PROCESSING,
        CardStatus.SUCCESS,
        CardStatus.CANCELLED,
        CardStatus.FAILED,
    }


def test_the_amount_is_shown_once_even_when_both_forms_arrive():
    """`amount` 与 `amount_minor` 同名"金额"：同时给时只显示一个，绝不并排两条。

    并排两条金额，用户会以为这笔操作有两笔钱——而两份值一旦不一致（一份分、一份元），
    卡片上也没有任何线索说明该信哪一份。白名单的顺序就是优先级：先出现的那个胜出。
    """

    card = confirmation_card(amount="500.00", amount_minor=50000)

    cells = [cell for column in column_cells(card) for cell in column]
    amounts = [cell["content"] for cell in cells if cell["content"].startswith("**金额**")]
    assert len(amounts) == 1
    assert amounts[0] == "**金额**\n¥500.00"


def test_the_amount_formatter_keeps_two_decimals_on_a_float_payload():
    """上游漏乘 100 直接给 500.0 元时不能印成 `¥500.0`——那会被读成五毛钱。"""

    card = confirmation_card(amount=500.0, fee=0.5)

    rendered = markdown_of(card)
    assert "**金额**\n¥500.00" in rendered
    assert "**手续费**\n¥0.50" in rendered
    assert "¥500.0\n" not in rendered


def test_the_text_card_states_what_it_is_doing():
    """页脚那句话是用户判断"还要不要等"的唯一线索，正文只有一半时更不能少。"""

    renderer = FeishuCardRenderer()

    assert "正在生成…" in markdown_of(
        renderer.render_text_card("余额", "正在算", status="streaming")
    )
    assert "已完成" in markdown_of(renderer.render_text_card("余额", "算好了", status="success"))
    assert "未完成" in markdown_of(renderer.render_text_card("余额", "失败了", status="failed"))


def test_a_button_label_from_the_payload_cannot_smuggle_a_code_onto_the_card():
    """载荷给的按钮文字是最容易被塞进验证码的位置：含数字的标签一律退回操作措辞。"""

    # 标签长度合规（8 位以内）、只是含数字——唯一能挡住它的是那条数字规则。
    card = confirmation_card(confirm_label="9确认9", cancel_label="取消")

    rendered = json.dumps(card, ensure_ascii=False)
    assert "9确认9" not in rendered
    assert "确认转账" in rendered
    assert "取消操作" in rendered


def test_an_envelope_that_contradicts_the_click_ids_is_refused():
    """信封说这笔是 q123/p456，调用方却要按 q999 写按钮：那张卡点下去必 409，当场炸掉更好。"""

    renderer = FeishuCardRenderer()
    pending = {
        "kind": "confirmation",
        "question_id": "q999",
        "pending_id": "p456",
        "confirmation": {"operation": "transfer", "amount": "500.00"},
    }

    with pytest.raises(ValueError):
        renderer.render_confirmation_card(pending, question_id="q123", pending_id="p456")

    # 一致的 id 仍然正常渲染：这道闸只挡矛盾，不挡正常路径。
    same = dict(pending, question_id="q123")
    assert callback_values(renderer.render_confirmation_card(same, "q123", "p456"))


def test_two_answers_that_render_identically_are_not_pushed_twice():
    """两边口径一致：渲染器按 MAX_TEXT_CHARS 截断，节流器也必须按截断后的长度算增量。

    否则两张被截成同一张卡片的超长回答会被判成"长了一大段"，于是每 1.5 秒白推一次 PATCH，
    直到流结束——用户看到的卡片一个字都没变。
    """

    clock = FakeClock()
    throttler = StreamThrottler(
        interval_ms=1500, min_delta_chars=30, max_text_chars=MAX_TEXT_CHARS, clock=clock
    )
    first = "开" * MAX_TEXT_CHARS
    throttler.record_update(first)

    clock.advance(5.0)
    # 前 MAX_TEXT_CHARS 个字相同、只在被截掉的部分不同：渲染出来的卡片逐字节相同。
    second = "开" * MAX_TEXT_CHARS + "尾巴" * 40

    assert throttler.should_update(second, is_final=False) is False
    assert throttler.should_update(second, is_final=True) is True


def test_a_text_limit_of_zero_is_refused():
    """0 等于把每段正文都截成空串：所有流式回答都会被判成"没变化"，卡片停在第一帧。"""

    with pytest.raises(ValueError):
        StreamThrottler(max_text_chars=0)


def test_format_minor_is_the_single_money_formatter():
    """金额只在一个地方换算：分开写两份，卡片和回执迟早对不上。"""

    assert format_minor(500000) == "¥5,000.00"
    assert format_minor(0) == "¥0.00"
    assert format_minor(-150) == "-¥1.50"


def test_a_boolean_element_reads_as_yes_or_no():
    """`fee_waived=True` 不该在中文卡片上显示成 `True`：卡片是给人看的。"""

    card = confirmation_card(fee_waived=True)

    assert "**手续费减免**\n是" in markdown_of(card)
