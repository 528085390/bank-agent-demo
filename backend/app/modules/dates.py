"""工具参数里的日期解析，只此一处。

模型会把用户的原话直接塞进 `period_start` / `period_end`（「9月13日到今天」），所以工具
得读懂这些写法。三个 handler 曾经各写一份解析：`analyze_bill_comprehensive` 认得「今天」，
`send_analysis_report` 与 `query_transactions` 只认 ISO —— 同一句话换个工具就被判
VALIDATION，模型只好自己猜日期，而它没有可靠的「今天」（猜成了对话里出现过的 10-10）。
解析因此收到这里，一处定义、三处共用：工具认得的写法，全项目一致。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from app.core.clock import SystemClock
from app.modules.base import ToolFailure
from app.tools.protocol import ErrorCode

#: 口语里的相对日期，只收最近这几天（含预约常用的明天/后天）。
#:
#: 再远的说法（上周、上个月、去年）不在这里硬编码：那需要一套日历规则，而模型按
#: `DOMAIN_PROMPT` 里给它的当前日期换算更准。这里的三个词是最高频、也最容易算错的
#: （「昨天」跨月会算错），所以由代码兜住。
RELATIVE_DAYS = {
    '今天': 0,
    '今日': 0,
    '明天': 1,
    '明日': 1,
    '昨天': -1,
    '昨日': -1,
    '前天': -2,
    '前日': -2,
    '后天': 2,
    '后日': 2,
}

#: 相对年份：整年范围（年报）。值是相对当前年份的偏移。
RELATIVE_YEARS = {
    '今年': 0,
    '去年': -1,
    '前年': -2,
}

#: 只写年份的写法：「2025年」——整年范围，年报用它。
_YEAR_ONLY = re.compile(r'^(\d{4})年$')

#: 报错时列出来，方便模型改口重试。
SUPPORTED_FORMATS = (
    'YYYY-MM-DD、YYYY年M月D日、YYYY/M/D、M月D日、今年M月D日、'
    '今年M月、一月一日、今年一月、今天、明天、昨天、前天、后天，'
    '以及整年的写法：2025年、今年、去年、前年'
)

_FULL_CN = re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$')
#: 月/日既接受阿拉伯数字，也接受中文数字：五月一日、十二月三十一日。
_NUMBER = r'(?:\d{1,2}|[一二三四五六七八九十]+)'
_MONTH_DAY = re.compile(rf'^({_NUMBER})月(?:({_NUMBER})[日号])?$')
#: 斜杠写法。浏览器原生日期框显示的占位符就是 `yyyy/mm/dd`，用户照着打很正常 ——
#: 认不得它，界面提示什么、用户就照着打出什么，然后被判不合法，是最没道理的一种失败。
_SLASH = re.compile(r'^(\d{4})/(\d{1,2})/(\d{1,2})$')

_CN_DIGITS = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9,
}


def _chinese_number(value: str) -> int | None:
    """把「一」…「三十一」读成整数；只支持 1–31 这种日期范围。"""

    if not value or any(char not in '一二三四五六七八九十' for char in value):
        return None
    if value == '十':
        return 10
    if value.startswith('十'):
        rest = value[1:]
        return 10 + _CN_DIGITS.get(rest, 0) if rest else 10
    if '十' in value:
        tens, ones = value.split('十', 1)
        tens_value = _CN_DIGITS.get(tens)
        if tens_value is None:
            return None
        ones_value = _CN_DIGITS.get(ones, 0) if ones else 0
        if ones and ones_value is None:
            return None
        return tens_value * 10 + ones_value
    return _CN_DIGITS.get(value)


def _number(value: str) -> int | None:
    return int(value) if value.isdigit() else _chinese_number(value)


def parse_tool_date(value: object, field: str, *, today: date | None = None) -> date:
    """把工具参数读成 `date`，读不出来就报 VALIDATION。

    Args:
        value: 模型传来的原值（可能是 None，可能带空白）
        field: 参数名，用于错误提示
        today: 「今天」是哪天。默认系统当天；测试里固定住它，免得断言跟着日历漂。

    Returns:
        date 对象

    Raises:
        ToolFailure: 缺参数，或这个写法不认识
    """

    if value is None or not str(value).strip():
        raise ToolFailure(ErrorCode.VALIDATION, f"缺少必需参数：{field}")

    text = str(value).strip()
    base = today or SystemClock.today()
    year_override: int | None = None

    # 整年的相对说法（年报）：「今年」「去年」「前年」本身就是完整区间，不是缺了月日。
    if text in RELATIVE_YEARS:
        return _year_date(base.year + RELATIVE_YEARS[text], field, text)

    if text.startswith('今年'):
        # 「今年5月」这类：由代码按系统日期补年份，模型不需要知道今天是哪一年。
        year_override = base.year
        text = text[2:].strip()

    if text in RELATIVE_DAYS:
        return base + timedelta(days=RELATIVE_DAYS[text])

    # ISO 优先：这是工具描述里推荐的写法，也是模型最常给的。
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    match = _YEAR_ONLY.match(text)
    if match:
        return _year_date(int(match.group(1)), field, text)

    match = _FULL_CN.match(text) or _SLASH.match(text)
    if match:
        return _checked_date(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), text, field
        )

    match = _MONTH_DAY.match(text)
    if match:
        month = _number(match.group(1))
        if month is None:
            raise ToolFailure(
                ErrorCode.VALIDATION,
                f"参数 {field} 不是可识别的月份：{text}",
            )
        year = year_override or base.year
        day_text = match.group(2)
        if day_text is None:
            # 只给月份时：开始日期取 1 号，结束日期取当月最后一天。
            return _month_date(year, month, field, text)
        day = _number(day_text)
        if day is None:
            raise ToolFailure(
                ErrorCode.VALIDATION,
                f"参数 {field} 不是可识别的日期：{text}",
            )
        return _checked_date(year, month, day, text, field)

    raise ToolFailure(
        ErrorCode.VALIDATION,
        f"参数 {field} 不是可识别的日期：{value}。支持 {SUPPORTED_FORMATS}",
    )


def _year_date(year: int, field: str, text: str) -> date:
    """整年写法（「2025年」「去年」）：开始取 1 月 1 日，结束取 12 月 31 日。

    年报就是这一种：用户说「出 2025 年年报」时，模型把「2025年」原样传下来即可，
    不需要自己拼出两个日期。
    """

    if field.endswith('start'):
        return date(year, 1, 1)
    if field.endswith('end'):
        return date(year, 12, 31)
    raise ToolFailure(
        ErrorCode.VALIDATION, f"参数 {field} 只给了年份，请给出具体日期：{text}"
    )


def _month_date(year: int, month: int, field: str, text: str) -> date:
    """月份-only 的写法：开始取 1 号，结束取月末，其他字段拒绝猜测。"""

    if not 1 <= month <= 12:
        raise ToolFailure(ErrorCode.VALIDATION, f"参数 {field} 不是有效月份：{text}")
    if field.endswith('start'):
        day = 1
    elif field.endswith('end'):
        day = calendar.monthrange(year, month)[1]
    else:
        raise ToolFailure(
            ErrorCode.VALIDATION, f"参数 {field} 只给了月份，请给出具体日期：{text}"
        )
    return _checked_date(year, month, day, text, field)


def _checked_date(year: int, month: int, day: int, text: str, field: str) -> date:
    """中文写法拼出来的年月日仍然可能不是真日期（2月30日）。"""

    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ToolFailure(ErrorCode.VALIDATION, f"参数 {field} 不是有效日期：{text}") from exc
