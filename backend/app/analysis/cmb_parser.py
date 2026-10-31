"""招商银行流水解析器（C 交付物）。

解析 B 附件处理器产出的文本（TSV 风格，含 ``# sheet:`` 标记），产出 A 的
``ParseResult``。``TxRecord.amount`` 用 Decimal 元（带符号），A 入库时 ×100 转分。

文本格式约定（与 B 的 ``_render_sheets_as_text`` 对齐）：:

    # sheet: 账单明细
    交易日期	摘要	金额	余额
    2026-01-10	示例快捷支付	-123.45	1876.55

按表头名定位列（而非硬编码列序），因此真实招行流水的列顺序变化也能解析；
解析不到的列 / 行记入 ``quality_issues``，不抛异常——宁可少解析，不让整单失败。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.core.clock import SystemClock
from app.services.bill_import import DocumentInfo, ParseResult, TxRecord

PARSER_NAME = "cmb_parser"
PARSER_VERSION = "1.0"

# 表头关键词 → 字段（列名含任一关键词即定位该字段）。列名按关键词认、不按银行写死，
# 所以不同银行/渠道只要表头用词落在这些词里就能解析（例如微信账单的「交易时间」）。
_DATE_HEADERS = ("交易时间", "日期", "记账日期", "交易日期", "date")
#: 明确的“只有日期”列。出现它时，另一列「交易时间」才是独立的时刻列；只有
#: 「交易时间」一列时（微信这类），它本身就是完整的 datetime，走 _DATE_HEADERS。
_EXPLICIT_DATE_HEADERS = ("日期", "date")
#: 独立的时间列，只在已经找到日期列时启用。
_TIME_HEADERS = ("时间", "time")
_TYPE_HEADERS = ("摘要", "交易摘要", "类型", "交易类型", "description", "type")
_AMOUNT_HEADERS = ("金额", "交易金额", "amount")
_BALANCE_HEADERS = ("余额", "联机余额", "balance")
_COUNTERPARTY_HEADERS = ("对手", "对方", "商户", "counterparty", "merchant")
#: 商品列。微信账单带这一列，信息量比对手方还大 ——「清远北区自营食堂」「单车」
#: 「12306消费」「书籍购买」都在里面。分类拿它当文本的一部分，才认得出食堂、单车这些场景。
#: 注意「商品」和「商户」是两个字，不会互相误伤。
_PRODUCT_HEADERS = ("商品", "商品说明", "商品名称", "product", "goods")
#: 方向列（「收/支」这类）。带它的账单金额列通常是全正数，方向只写在这一列里。
_DIRECTION_HEADERS = ("收/支", "收支", "方向", "direction")


def parse_cmb_statement(text: str) -> ParseResult:
    """解析招行流水文本，返回 A 的 ``ParseResult``。"""

    transactions: list[TxRecord] = []
    issues: list[str] = []

    account_no = ""
    account_holder = ""

    for sheet_name, header, rows, preamble in _split_sheets(text):
        if preamble:
            # 说明行被跳过是正常的，但要留痕：出问题时能一眼看到系统把哪行当成了表头。
            issues.append(f"工作表「{sheet_name}」前 {len(preamble)} 行是说明，已跳过")
        # 户名与账号就写在这些说明行里，这是这份导出里唯一记录账户身份的地方。
        found_no, found_holder = _extract_account_identity(preamble)
        account_no = account_no or found_no
        account_holder = account_holder or found_holder
        columns = _map_columns(header)
        if "date" not in columns or "amount" not in columns:
            issues.append(f"工作表「{sheet_name}」缺少日期或金额列，已跳过")
            continue
        # 收支方向只有两个来源：方向列（微信这类）或金额自带的符号（招行这类）。
        # 两者都没有时金额一律是正数，就按收入记——方向信息本就不在这份导出里，
        # 那是导出方的事，解析器不为此报错。
        has_direction = "direction" in columns
        rows = _merge_continuation_rows(rows, columns)
        for row_index, row in enumerate(rows, start=1):
            tx = _parse_row(row, columns, row_index, has_direction=has_direction)
            if tx is None:
                issues.append(f"工作表「{sheet_name}」第 {row_index} 行无法解析，已跳过")
                continue
            transactions.append(tx)

    if not transactions:
        issues.append("未解析到任何交易记录")

    dates = [tx.booked_date for tx in transactions]
    document_info = DocumentInfo(
        bank_code="CMB",
        # file_hash / filename 由 A 在 bill_processing 里回填（B 的 AttachmentResult 才有）。
        file_hash="",
        filename="",
        # 账号/户名从被跳过的前导说明行里读出来（见 _extract_account_identity）。
        # 读不到就留空 —— A 会脱敏成空串，绝不在这里编一个账户名。
        account_holder=account_holder,
        account_no=account_no,
        account_type="一卡通",
        period_start=min(dates) if dates else SystemClock.today(),
        period_end=max(dates) if dates else SystemClock.today(),
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
    return ParseResult(document_info=document_info, transactions=transactions, quality_issues=issues)


#: 找回表头时最多向下看多少行。账单的说明区通常只有几行到十几行。
_HEADER_SEARCH_LIMIT = 30


#: 前导说明行里的账号：真实卡号 16–19 位，以 6225 开头（招行一卡通）。
#: 导出文件里往往已经被脱敏成「6225********1234」，所以星号也要认 —— 只认数字的话，
#: 恰恰是真实文件反而提不到账号。
_ACCOUNT_NO = re.compile(r"6225[\d*]{12,15}")

#: 户名/姓名后面跟的值，到空白或标点为止。
_ACCOUNT_HOLDER = re.compile(r"(?:户名|姓名)\s*[：:]\s*([^\s\t，,；;、]+)")


def _extract_account_identity(preamble: list[list[str]]) -> tuple[str, str]:
    """从被跳过的说明行里提取 (账号, 户名)；读不到就给空串。

    这份导出没有别的字段说明「这是谁的哪张卡」，账户身份只在说明行里。读不出来就诚实
    留空 —— 编一个账户名只会让下游把假数据当真值。
    """

    account_no = ""
    account_holder = ""
    for line in preamble:
        text = "\t".join(line)
        if not account_no:
            found = _ACCOUNT_NO.search(text)
            if found:
                account_no = found.group(0)
        if not account_holder:
            found = _ACCOUNT_HOLDER.search(text)
            if found:
                account_holder = found.group(1)
    return account_no, account_holder


def _split_sheets(text: str) -> list[tuple[str, list[str], list[list[str]], list[list[str]]]]:
    """按 ``# sheet:`` 分块，返回 [(表名, 表头行, [数据行...], [跳过的说明行...])]。"""

    sheets: list[tuple[str, list[str], list[list[str]], list[list[str]]]] = []
    current_name = ""
    current_lines: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("# sheet:"):
            if current_lines:
                sheets.append(_split_header(current_name, current_lines))
            current_name = line[len("# sheet:"):].strip()
            current_lines = []
        elif line.strip():
            current_lines.append(line.split("\t"))
    if current_lines:
        sheets.append(_split_header(current_name, current_lines))
    return sheets


def _split_header(
    name: str, lines: list[list[str]]
) -> tuple[str, list[str], list[list[str]], list[list[str]]]:
    """找真正的表头行，返回 (表名, 表头行, 数据行, 被跳过的说明行)。

    账单导出常在表格前放若干说明行（"微信支付账单明细"、"共155笔记录"、"收入15笔
    2723.43元"…），所以**第一行往往不是列名**。按两条线索找：

    1. **能不能认出必需列**（日期 + 金额）——最准，因为表头就是那个"有日期列、有金额列"
       的行；
    2. 认不出就**看形状**：说明行总是只占第一列，所以取第一个"两列以上非空"的行。

    两条都不成立时才退回第一行，由调用方照常报"缺少日期或金额列"。

    被跳过的行**原样带回去**（以前只带一个计数）：户名与账号就写在里面，调用方要用它
    提取账户身份（见 ``_extract_account_identity``）。
    """

    if not lines:
        return name, [], [], []

    window = lines[:_HEADER_SEARCH_LIMIT]
    for index, line in enumerate(window):
        columns = _map_columns(line)
        if "date" in columns and "amount" in columns:
            return name, line, lines[index + 1:], lines[:index]
    for index, line in enumerate(window):
        if sum(1 for cell in line if cell.strip()) >= 2:
            return name, line, lines[index + 1:], lines[:index]
    return name, lines[0], lines[1:], []


def _map_columns(header: list[str]) -> dict[str, int]:
    """把表头行映射为 {字段: 列索引}，按关键词匹配。

    日期/时间分两种表头：

    - 「交易日期」+「交易时间」：前者是日期、后者是时刻，两列合并成完整时间；
    - 只有「交易时间」：它本身就是完整的 datetime（微信这类）。
    """

    columns: dict[str, int] = {}
    names = [name.strip().lower() for name in header]

    # 先找明确的日期列，再决定「时间」是不是独立时刻列。
    date_index = next(
        (index for index, name in enumerate(names) if any(kw in name for kw in _EXPLICIT_DATE_HEADERS)),
        None,
    )
    time_index = None
    if date_index is not None:
        time_index = next(
            (
                index
                for index, name in enumerate(names)
                if index != date_index and any(kw in name for kw in _TIME_HEADERS)
            ),
            None,
        )
    else:
        # 没有「日期」列时，把「交易时间」当作完整的 datetime 列。
        date_index = next(
            (index for index, name in enumerate(names) if any(kw in name for kw in _DATE_HEADERS)),
            None,
        )
    if date_index is not None:
        columns["date"] = date_index
    if time_index is not None:
        columns["time"] = time_index

    for index, lowered in enumerate(names):
        if index in {date_index, time_index}:
            continue
        if "type" not in columns and any(kw in lowered for kw in _TYPE_HEADERS):
            columns["type"] = index
        elif "amount" not in columns and any(kw in lowered for kw in _AMOUNT_HEADERS):
            columns["amount"] = index
        elif "balance" not in columns and any(kw in lowered for kw in _BALANCE_HEADERS):
            columns["balance"] = index
        elif "counterparty" not in columns and any(kw in lowered for kw in _COUNTERPARTY_HEADERS):
            columns["counterparty"] = index
        elif "product" not in columns and any(kw in lowered for kw in _PRODUCT_HEADERS):
            columns["product"] = index
        elif "direction" not in columns and any(kw in lowered for kw in _DIRECTION_HEADERS):
            columns["direction"] = index
    return columns


def _direction_sign(value: str) -> int | None:
    """方向列的字面值 → 金额符号（支出 -1 / 收入 +1）；认不出返回 None。"""

    text = value.strip().lower()
    if not text:
        return None
    # 两字词优先，免得「收支」这类中间态被单字拆错。
    if "收入" in text or text in {"收", "+", "in", "credit", "income"}:
        return 1
    if "支出" in text or text in {"支", "-", "out", "debit", "expense"}:
        return -1
    return None


#: 一个「只有时刻」的格子。PDF 把一行交易拆成两行时，续行的日期列里装的就是它。
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def _merge_continuation_rows(rows: list[list[str]], columns: dict[str, int]) -> list[list[str]]:
    """把被 PDF 提取拆成两行的交易拼回一行。

    这类 PDF 的表格是竖排的，一行交易提出来会断成两行，断点正好落在日期列上：

        第 1 行  …单号前半 | 2026-10-11 | 转入零钱通- | 其他 | 零钱 | 290.44 | …商户单号前半
        第 2 行  单号后半  | 14:27:27   | 来自零钱     |      |      |        | …商户单号后半

    判据只有一个：**某行的日期列长得像个时刻**（`HH:MM:SS`）。那样的行不是新交易，是
    上一行被截断的续行 —— 逐列把它补回上一行：该格空着就填进去，已有内容就接在后面
    （单号、摘要这类文本正是被从中间截断的）。日期列两段之间补一个空格，好让
    `2026-10-11` 和 `14:27:27` 拼回 `datetime.fromisoformat` 认得的写法。

    认不出续行的账单不受影响：它们的日期列每一行都是真日期，一行都合并不了。
    """

    date_at = columns["date"]
    merged: list[list[str]] = []

    for row in rows:
        cell = row[date_at].strip() if date_at < len(row) else ""
        looks_like_time = bool(_TIME_ONLY.match(cell))

        if not (looks_like_time and merged):
            merged.append(list(row))
            continue

        target = merged[-1]
        for index, value in enumerate(row):
            piece = value.strip()
            if not piece:
                continue
            while index >= len(target):
                target.append("")
            if not target[index].strip():
                target[index] = piece
            elif index == date_at:
                # 日期 + 时刻：中间要有空格，否则拼成 "2026-10-1114:27:27"
                target[index] = f"{target[index].strip()} {piece}"
            else:
                # 被从中间截断的文本（单号、商户名），直接接上
                target[index] = target[index].rstrip() + piece

    return merged


def _parse_row(
    row: list[str], columns: dict[str, int], row_index: int, *, has_direction: bool
) -> TxRecord | None:
    """解析一行数据；缺日期或金额返回 None（记入 quality_issues）。"""

    def cell(field: str) -> str:
        index = columns.get(field)
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    date_text = cell("date")
    time_text = cell("time")
    booked_at = _parse_datetime(
        f"{date_text} {time_text}".strip() if time_text else date_text
    )
    amount = _parse_decimal(cell("amount"))
    if booked_at is None or amount is None:
        return None

    # 有方向列就以它为准（这类账单的金额列是全正数，符号不带信息）；认不出才沿用金额
    # 自带的符号，那正是「无方向列、靠正负号」那一派的表达方式。
    if has_direction:
        sign = _direction_sign(cell("direction"))
        if sign is not None:
            amount = abs(amount) * sign

    balance = _parse_decimal(cell("balance"))
    return TxRecord(
        booked_date=booked_at.date(),
        booked_at=booked_at,
        amount=amount,
        currency="CNY",
        balance=balance,
        transaction_type=cell("type"),
        counterparty=cell("counterparty"),
        product=cell("product"),
        source_page=1,
        source_row_index=row_index,
    )


def _parse_datetime(value: str) -> datetime | None:
    """日期/时间列。

    微信等导出带时分秒（"2026-10-10 14:09:11"）。**时分秒必须留下来** —— 后面的
    「交易活跃时段」「夜间交易」全靠它。这个函数以前叫 `_parse_date`，结尾带一个
    `.date()`，把时刻切掉了，于是所有交易都被当成 12 点。

    只有日期的写法（"2026-10-10"）同样接受，时间部分补 00:00。
    """

    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        # fromisoformat 同时接受纯日期与带时间的写法。
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    """只要日期部分的调用方用这个。"""

    parsed = _parse_datetime(value)
    return parsed.date() if parsed else None


def _parse_decimal(value: str) -> Decimal | None:
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None
