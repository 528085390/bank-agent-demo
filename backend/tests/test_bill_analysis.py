"""账单深度分析：空区间报告，以及账户真值（或诚实降级）。

第一条钉住已搬来的 app/services/bill_analysis 的既有输出形状——空区间不抛异常，
而是返回一份 text_report 写死文案、report_meta.total_transactions 为 0 的报告。

其余几条驱动 Task 7：账户信息只有两个真来源（解析器从前导说明行里读出的户名/账号，
以及它落库后的 account_no_masked），任何一层都不许自己编一个「主账户」或「****」。
"""

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select

from app.analysis.cmb_parser import parse_cmb_statement
from app.billing import detect
from app.billing.handlers.registry import HandleContext
from app.billing.handlers.spreadsheet import SpreadsheetHandler
from app.services.bill_analysis import BillAnalyzer
from app.services.bill_analysis.analyzer import cashflow_totals
from app.services.bill_analysis.preprocessor import TransactionPreprocessor
from app.services.bill_import import BillImportService
from app.services.bill_query import BillQueryService
from app.storage.bill_models import BillTask, StatementDocument, Transaction


def test_an_empty_period_reports_no_transactions(rt):
    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 9, 1), date(2026, 9, 30))

    assert report["text_report"] == "该时间段没有交易记录。"
    assert report["report_meta"]["total_transactions"] == 0
    assert report["report_meta"]["cashflow"]["all"] == {
        "income_minor": 0,
        "expense_minor": 0,
        "net_minor": 0,
    }


def _tx(amount: int, direction: str, internal: bool = False):
    return SimpleNamespace(
        amount=amount,
        direction=direction,
        is_income=direction == "credit",
        is_expense=direction == "debit",
        is_internal=internal,
    )


def test_cashflow_keeps_the_bank_total_and_the_external_total_apart():
    # 微众样例的 10 笔：理财申赎和信用卡还款属于内部资金，只从 external 里剔除。
    transactions = [
        _tx(850000, "credit"),
        _tx(50000, "credit"),
        _tx(128045, "debit", internal=True),
        _tx(4680, "debit"),
        _tx(10000, "debit"),
        _tx(3250, "debit"),
        _tx(39900, "debit"),
        _tx(200000, "credit", internal=True),
        _tx(8888, "credit"),
        _tx(300000, "debit", internal=True),
    ]

    flow = cashflow_totals(transactions)

    assert flow["all"] == {
        "income_minor": 1108888,
        "expense_minor": 485875,
        "net_minor": 623013,
    }
    assert flow["external"] == {
        "income_minor": 908888,
        "expense_minor": 57830,
        "net_minor": 851058,
    }


#: 招行导出文本的真实形状：``# sheet:`` 分块、前导说明行，然后才是表头。说明行里同时
#: 有户名与账号，账号已脱敏成 ``6225********1234``——导出文件就是这样（掩码是
#: mask_account 的活），解析器只负责原样读出来。
_STATEMENT_WITH_PREAMBLE = "\n".join(
    [
        "# sheet: 账单明细",
        "户名：张三  账号：6225********1234",
        "交易日期\t摘要\t金额\t余额",
        "2026-01-10\t示例快捷支付\t-123.45\t1876.55",
    ]
)


def test_the_parser_reads_the_account_out_of_the_preamble():
    info = parse_cmb_statement(_STATEMENT_WITH_PREAMBLE).document_info

    assert info.account_no == "6225********1234"
    assert info.account_holder == "张三"


def _transaction(**overrides) -> Transaction:
    """一笔最小可用的原始交易；不入库，只喂给预处理器。"""

    values = {
        "booked_date": date(2026, 1, 10),
        "booked_at": datetime(2026, 1, 10, 9, 30),
        "currency": "CNY",
        "amount": 12345,
        "direction": "debit",
        "transaction_type": "示例快捷支付",
        "counterparty": "某商户",
        "source_page": 1,
        "source_row_index": 1,
    }
    values.update(overrides)
    return Transaction(**values)


def test_the_preprocessor_never_invents_an_account_name():
    processed = TransactionPreprocessor().process([_transaction()])

    assert {tx.account_no for tx in processed} == {""}
    # 「****」以前是硬编码的假末四位，会被下一个人当成真数据。
    assert {tx.card_last4 for tx in processed} == {""}


def _seed_task(rt) -> tuple[UUID, str]:
    """建一个已完成的账单任务，返回 (id, task_no)。

    BillImportService.save_statement 的入参是 **task_no**（_require_task 按它查），
    而外键要的是主键 id —— 两个都得给。
    """

    with rt.database.sessions.begin() as db:
        task = BillTask(
            task_no=f"BT-{uuid4().hex[:12]}",
            user_id="user_0001",
            bank_code="CMB",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            state="COMPLETED",
            source_mode="manual_upload",
            password_attempts_left=3,
        )
        db.add(task)
        db.flush()
        return task.id, task.task_no


def _seed_statement(
    rt,
    *,
    account_no_masked: str,
    task_id: UUID | None = None,
    amount: int = 12345,
    counterparty: str = "某商户",
    raw_record_hash: str | None = None,
    booked_date: date = date(2026, 1, 10),
) -> UUID:
    """给 user_0001 落一份账单文档与一笔交易，返回交易 id。

    同一个任务下可以落多份账单文档（同一时段多张卡的真实形状）。``amount``、
    ``counterparty`` 与 ``raw_record_hash`` 用来把不同交易区分开：预处理器按
    ``raw_record_hash`` 去重，哈希相同才是同一笔交易。
    """

    if task_id is None:
        task_id, _ = _seed_task(rt)
    with rt.database.sessions.begin() as db:
        document = StatementDocument(
            task_id=task_id,
            user_id="user_0001",
            bank_code="CMB",
            source_file_hash=uuid4().hex,
            original_filename="statement.tsv",
            account_holder_masked="张*" if account_no_masked else "",
            account_no_masked=account_no_masked,
            account_type="一卡通",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            parser_name="cmb_parser",
            parser_version="1.0",
        )
        db.add(document)
        db.flush()
        transaction = _transaction(
            statement_document_id=document.id,
            task_id=task_id,
            raw_record_hash=raw_record_hash or uuid4().hex,
            amount=amount,
            counterparty=counterparty,
            booked_date=booked_date,
            booked_at=datetime(booked_date.year, booked_date.month, booked_date.day, 9, 30),
        )
        db.add(transaction)
        db.flush()
        return transaction.id


def _account_usage(report: dict) -> dict:
    return next(m for m in report["modules"] if m["module_id"] == "account_usage")


def test_the_analyzer_passes_the_masked_account_into_the_report(rt):
    _seed_statement(rt, account_no_masked="6225********1234")

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    assert report["report_meta"]["accounts"] == ["6225********1234"]
    assert _account_usage(report)["data"]["accounts"][0]["account_no"] == "6225********1234"


def test_account_usage_says_the_statement_has_no_account_when_it_has_none(rt):
    _seed_statement(rt, account_no_masked="")

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    assert report["report_meta"]["accounts"] == []
    assert any(
        "账单未提供账户信息" in insight["text"] for insight in _account_usage(report)["insights"]
    )


#: 账单原文里是完整卡号时，入库必须脱敏——这是既有的 mask_account 行为（Task 7 不碰它），
#: 但在 Task 7 之前解析器根本读不出账号，这条链上没有真值可供脱敏。
_STATEMENT_WITH_RAW_ACCOUNT = "\n".join(
    [
        "# sheet: 账单明细",
        "户名：张三  账号：6225123456781234",
        "交易日期\t摘要\t金额\t余额",
        "2026-01-10\t示例快捷支付\t-123.45\t1876.55",
    ]
)


def test_the_statement_account_reaches_the_report_through_the_import(rt):
    """端到端：解析原文 → 脱敏入库 → 分析报告，账户号一路是同一张卡。"""

    _, task_no = _seed_task(rt)
    BillImportService(rt.database).save_statement(
        task_no, parse_cmb_statement(_STATEMENT_WITH_RAW_ACCOUNT)
    )

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    # mask_account 把 16 位卡号脱敏成「6225********1234」；报告里拿到的就是它。
    assert report["report_meta"]["accounts"] == ["6225********1234"]


def _accounts_by_no(report: dict) -> dict[str, int]:
    """报告里每个账户各摊到几笔交易 —— 归错账户会立刻体现在计数上。"""

    return {
        account["account_no"]: account["count"]
        for account in _account_usage(report)["data"]["accounts"]
    }


def test_a_document_without_an_account_does_not_inherit_another_documents_account(rt):
    """同一时段两份账单、只有一份读得到账号：另一份的交易不许被盖上它的号。"""

    task_id, _ = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    _seed_statement(rt, account_no_masked="", task_id=task_id, amount=99999)

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    # 两笔各归各的：读得到账号的那笔归它，读不到的那笔归「未知」。若分析器把唯一读到
    # 的账号盖给整个时段，这里会变成 {"6225********1234": 2} —— 那就是在编归属。
    assert _accounts_by_no(report) == {"6225********1234": 1, "": 1}
    assert report["report_meta"]["accounts"] == ["6225********1234"]

    by_no = {acc["account_no"]: acc for acc in _account_usage(report)["data"]["accounts"]}
    assert by_no["6225********1234"]["expense_minor"] == 12345
    assert by_no[""]["expense_minor"] == 99999


def test_each_transaction_keeps_its_own_documents_account(rt):
    """两份都读得到账号、但是不同的卡：两个账号都要留在报告里。"""

    task_id, _ = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    _seed_statement(rt, account_no_masked="6225********9999", task_id=task_id, amount=99999)

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    assert _accounts_by_no(report) == {"6225********1234": 1, "6225********9999": 1}
    assert report["report_meta"]["accounts"] == ["6225********1234", "6225********9999"]

    insights = "".join(insight["text"] for insight in _account_usage(report)["insights"])
    assert "账单未提供账户信息" not in insights


def test_identical_rows_on_two_accounts_both_survive(rt):
    """两份账单、账户不同、**交易行逐字段相同**：两份的行都要留，两个账户都要在。

    去重键里没有账户时，第二份整份都是「重复」—— 它被静默清空，连同它的账户一起从
    ``report_meta["accounts"]`` 消失（那份账户列表是从幸存行推出来的）。
    """

    task_id, _ = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    _seed_statement(rt, account_no_masked="6225********9999", task_id=task_id, amount=12345)

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    assert _accounts_by_no(report) == {"6225********1234": 1, "6225********9999": 1}
    assert report["report_meta"]["accounts"] == ["6225********1234", "6225********9999"]
    assert report["report_meta"]["total_transactions"] == 2


def test_the_same_transaction_stored_twice_is_still_merged(rt):
    """同一账户、同一笔交易（``raw_record_hash`` 相同）落了两行：分析只算一次。

    这是入库层判重的同一枚指纹；正常写入不会留下这种行，分析侧只是兜住历史数据与
    并发插入。除此之外，长得再像的两行也不许合并。
    """

    task_id, _ = _seed_task(rt)
    duplicate = "same-record-hash"
    _seed_statement(
        rt,
        account_no_masked="6225********1234",
        task_id=task_id,
        raw_record_hash=duplicate,
    )
    _seed_statement(
        rt,
        account_no_masked="6225********1234",
        task_id=task_id,
        raw_record_hash=duplicate,
    )

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    assert _accounts_by_no(report) == {"6225********1234": 1}
    assert report["report_meta"]["total_transactions"] == 1


def test_two_same_day_purchases_are_not_merged(rt):
    """同日、同额、同商户的两笔真实交易（哈希不同）都要算 —— 它们只是长得像。

    以前的去重键不含交易时刻，这两行会被合并成一笔，而「查询流水 / 统计」数的是原始
    行，于是两边单数对不上。
    """

    task_id, _ = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id)

    report = BillAnalyzer(rt.database).analyze("user_0001", date(2026, 1, 1), date(2026, 1, 31))

    assert report["report_meta"]["total_transactions"] == 2


def test_statistics_and_analysis_count_the_same_rows(rt):
    """同一区间的「共 N 笔」与分析的「交易总数」必须是同一个数。"""

    task_id, _ = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=999)

    period_start, period_end = date(2026, 1, 1), date(2026, 1, 31)
    page = BillQueryService(rt.database).get_transactions(
        "user_0001", period_start, period_end
    )
    report = BillAnalyzer(rt.database).analyze("user_0001", period_start, period_end)

    assert page["total"] == 3
    assert report["report_meta"]["total_transactions"] == page["total"]


#: 同一份对账单走 CSV 处理器（spreadsheet handler）的形状：说明行 + 真表头 + 数据行。
_STATEMENT_AS_CSV = "\n".join(
    [
        "户名：张三  账号：6225********1234",
        "招商银行交易流水",
        "交易日期,摘要,金额,余额",
        "2026-01-10,示例快捷支付,-123.45,1876.55",
    ]
)


def test_the_spreadsheet_handler_keeps_the_preamble_the_parser_needs():
    """链路①：表格处理器渲染出的文本，必须让解析器还能读到户名与账号。

    「表格处理器不用改」这个结论此前只有读代码背书，没有测试钉住：一旦
    ``_read_sheet`` / ``_render_sheets_as_text`` 把说明行丢掉，账户维度会静默变空。
    """

    ctx = HandleContext(
        filename="statement.csv",
        declared_type=detect.CSV,
        detected_type=detect.CSV,
        scratch=Path("."),
        depth=0,
        max_depth=1,
        recurse=lambda *_: None,
    )
    result = SpreadsheetHandler().handle(_STATEMENT_AS_CSV.encode("utf-8"), ctx)

    info = parse_cmb_statement(result.text).document_info

    assert info.account_no == "6225********1234"
    assert info.account_holder == "张三"


def test_analysis_can_be_scoped_to_one_document(rt):
    """按文档分析：同一时段的另一份账单不能混进来。

    「分析刚导入的这份流水」靠的就是这个范围：用户传的是哪个文件，报告里就只有
    那个文件解析出来的交易 —— 同月另一张卡的账单不该被算进去。
    """

    task_id, _ = _seed_task(rt)
    first = _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    _seed_statement(rt, account_no_masked="6225********9999", task_id=task_id, amount=99999)

    with rt.database.sessions() as db:
        document_id = db.scalar(
            select(Transaction.statement_document_id).where(Transaction.id == first)
        )

    report = BillAnalyzer(rt.database).analyze(
        "user_0001", date(2026, 1, 1), date(2026, 1, 31), None, document_id=document_id
    )

    assert report["report_meta"]["total_transactions"] == 1
    assert report["report_meta"]["accounts"] == ["6225********1234"]
    assert report["report_meta"]["cashflow"]["all"]["expense_minor"] == 12345


def test_the_analysis_tool_resolves_a_task_number_to_that_statement(rt):
    """工具收到任务号时不追问日期，直接分析这份任务对应的账单文档。"""

    from app.modules.bill.analysis_handlers import create_analysis_handlers
    from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext

    task_id, task_no = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    # 同一用户的另一份账单（另一个任务、同一时段）：按任务号分析时不能把它算进来。
    _seed_statement(rt, account_no_masked="6225********9999", amount=99999)

    handler = create_analysis_handlers(rt.database)["analyze_bill_comprehensive"]
    invocation = ToolInvocation(
        "bill",
        ToolCall(tool_name="bill_analyze_comprehensive", arguments={"task_id": task_no}),
        TrustedContext(user_id="user_0001", session_id="sess_bill"),
    )

    data = handler(invocation, None)

    # 两份文档同一天；任务号定位到本任务建的那一份（第一笔），不是整个用户。
    assert data.facts["report_meta"]["total_transactions"] == 1
    assert data.facts["report_meta"]["accounts"] == ["6225********1234"]


def test_sending_by_task_number_mails_only_that_statement(rt, monkeypatch):
    """按任务号发信：冻结时记下任务号，执行时按同一份文档重算。

    否则邮件会按日期区间重算，把同期间的另一份账单也算进去 —— 屏幕上 1 笔、邮件里 2 笔。
    """

    from app.modules import email_handlers as email_handlers_module
    from app.modules.email_handlers import create_email_handlers
    from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext

    task_id, task_no = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    _seed_statement(rt, account_no_masked="6225********9999", amount=99999)

    class Notifier:
        database = rt.database

        def get_user_email(self, user_id):
            return "user@example.com"

        def send_email(self, **kwargs):
            self.sent = kwargs
            return {"status": "SENT", "notification_id": "n1"}

    notifier = Notifier()
    captured = {}

    def fake_pdf(report):
        captured["report"] = report
        return b"%PDF-1.4"

    monkeypatch.setattr(email_handlers_module, "build_analysis_report_pdf", fake_pdf)

    def invocation(tool, arguments):
        return ToolInvocation(
            "bill",
            ToolCall(tool_name=tool, arguments=arguments),
            TrustedContext(user_id="user_0001", session_id="sess_bill"),
        )

    handlers = create_email_handlers(notifier)
    frozen = handlers["bill_send_report"](invocation("bill_send_report", {"task_id": task_no}), None)

    # 冻结时任务号进了快照，期间取的是文档自己的起止（不需要用户填日期）。
    assert frozen.facts["task_id"] == task_no
    assert frozen.facts["period_start"] == "2026-01-01"

    handlers["bill_send_report_execute"](
        invocation(
            "bill_send_report_execute",
            {
                "task_id": frozen.facts["task_id"],
                "period_start": frozen.facts["period_start"],
                "period_end": frozen.facts["period_end"],
            },
        ),
        None,
    )

    # 同一天的另一份账单没有被算进来。
    assert captured["report"]["report_meta"]["total_transactions"] == 1
    assert captured["report"]["report_meta"]["accounts"] == ["6225********1234"]


def test_the_query_tool_reads_only_the_statement_the_task_number_names(rt):
    """「查询刚导入的流水」：按任务号只取这一份账单，并把任务号随结果带回去。"""

    from app.modules.bill.handlers import create_bill_tool_handlers
    from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext

    task_id, task_no = _seed_task(rt)
    _seed_statement(rt, account_no_masked="6225********1234", task_id=task_id, amount=12345)
    # 同一用户的另一份账单（另一个任务、同一时段）：按任务号查询时不能出现。
    _seed_statement(rt, account_no_masked="6225********9999", amount=99999)

    handler = create_bill_tool_handlers(rt.database)["query_transactions"]
    invocation = ToolInvocation(
        "bill",
        ToolCall(tool_name="bill_query_transactions", arguments={"task_id": task_no}),
        TrustedContext(user_id="user_0001", session_id="sess_bill"),
    )

    data = handler(invocation, None)

    assert data.facts["total"] == 1
    assert data.facts["task_id"] == task_no
    assert data.display["task_id"] == task_no
    assert [item["amount_minor"] for item in data.facts["items"]] == [12345]


def test_a_full_calendar_year_is_reported_as_an_annual_report(rt):
    """整年区间 → 年报：报告类型换掉，并多一段年度总览。"""

    task_id, _ = _seed_task(rt)
    for month, amount in ((1, 10000), (2, 20000), (3, 30000)):
        _seed_statement(
            rt,
            account_no_masked="6225********1234",
            task_id=task_id,
            amount=amount,
            booked_date=date(2025, month, 5),
        )

    report = BillAnalyzer(rt.database).analyze(
        "user_0001", date(2025, 1, 1), date(2025, 12, 31)
    )

    assert report["report_meta"]["report_type"] == "annual"
    annual = next(m for m in report["modules"] if m["module_id"] == "annual_summary")
    data = annual["data"]
    assert data["year"] == 2025
    assert data["expense_minor"] == 60000
    assert data["active_months"] == ["2025-01", "2025-02", "2025-03"]
    # 只导入了三个月：年报必须如实说明覆盖范围，而不是把三个月当全年。
    assert "2025-04" in data["missing_months"]
    assert any("数据覆盖 3 / 12" in insight["text"] for insight in annual["insights"])


def test_a_partial_year_stays_a_standard_report(rt):
    """「2025 年 1 到 6 月」不是年报：口径由区间决定，不由用户叫法决定。"""

    task_id, _ = _seed_task(rt)
    _seed_statement(
        rt, account_no_masked="6225********1234", task_id=task_id, booked_date=date(2025, 3, 5)
    )

    report = BillAnalyzer(rt.database).analyze(
        "user_0001", date(2025, 1, 1), date(2025, 6, 30)
    )

    assert report["report_meta"]["report_type"] == "standard"
    assert all(m["module_id"] != "annual_summary" for m in report["modules"])


# --- 范围清洗：只认本轮用户原话 ------------------------------------------------


def test_scope_the_supervisor_invented_is_stripped():
    """用户只说「我要分析流水」：总调度补进去的任务号和期间都不算数。"""

    from app.modules.bill.workflow import sanitize_goal

    goal = (
        "用户要求分析流水。请对用户最近导入的流水"
        "（最近任务为 task_3e8508c419f04c45a136c0d66114562a，2026-05-01 至 2026-05-02，"
        "共 10 笔交易）进行分析。"
    )

    cleaned = sanitize_goal(goal, "我要分析流水", "SUCCEEDED")

    assert "task_3e8508c419f04c45a136c0d66114562a" not in cleaned
    assert "2026-05-01" not in cleaned
    assert "2026-05-02" not in cleaned
    assert "分析流水" in cleaned


def test_dates_the_user_gave_are_kept_even_if_the_supervisor_rewrote_them():
    """用户说了「五月」、「2026.01.01」这类写法：总调度转成的日期要保留。"""

    from app.modules.bill.workflow import sanitize_goal

    goal = "对 2026-05-01 至 2026-05-31 的流水做分析。"
    for user_text in ("分析五月的流水", "看看 2026.05.01 到 2026.05.31", "5月1日到5月31日"):
        assert "2026-05-01" in sanitize_goal(goal, user_text, "SUCCEEDED")


def test_a_retry_after_a_failed_analysis_may_reuse_the_previous_scope():
    """上一轮失败 + 「重新分析」：保留原范围，不必再问一遍。"""

    from app.modules.bill.workflow import sanitize_goal

    goal = "分析任务 task_3e8508c419f04c45a136c0d66114562a（2026-05-01 至 2026-05-02）。"

    assert sanitize_goal(goal, "重新分析", "FAILED") == goal


def test_a_retry_after_a_successful_analysis_does_not_reuse_it():
    """上一轮成功 + 「重新分析」：仍算新请求，范围清掉、重新问日期。"""

    from app.modules.bill.workflow import sanitize_goal

    goal = "分析任务 task_3e8508c419f04c45a136c0d66114562a（2026-05-01 至 2026-05-02）。"

    cleaned = sanitize_goal(goal, "重新分析", "SUCCEEDED")

    assert "task_3e8508c419f04c45a136c0d66114562a" not in cleaned
    assert "2026-05-01" not in cleaned


def test_last_analysis_status_comes_from_the_run_events(rt):
    """失败判定读结构化状态，不读助手回复的措辞。"""

    from datetime import timedelta

    from app.core.types import utcnow
    from app.modules.bill.workflow import last_analysis_status
    from app.storage.models import AgentRunEvent

    conversation = "conv_status_probe"

    def add_event(status, created):
        with rt.database.sessions.begin() as db:
            db.add(
                AgentRunEvent(
                    event_id=f"evt_{uuid4().hex}",
                    conversation_id=conversation,
                    run_id="run_1",
                    event_type="TOOL_RESULT",
                    agent_id="bill",
                    tool_name="bill_analyze_comprehensive",
                    user_id="user_0001",
                    status=status,
                    payload_json={},
                    created_at=created,
                )
            )

    assert last_analysis_status(rt.database, conversation) is None

    now = utcnow()
    add_event("FAILED", now)
    assert last_analysis_status(rt.database, conversation) == "FAILED"

    # 之后又成功过一次：最近一次的结果才是判据。
    add_event("SUCCEEDED", now + timedelta(minutes=1))
    assert last_analysis_status(rt.database, conversation) == "SUCCEEDED"


def test_the_bill_module_binds_its_database_into_the_graph_builder(rt):
    """范围清洗要查运行事件，所以装配时把数据库绑进图构建器（与 base.py 的 writes 同法）。"""

    from app.modules.bill import build_bill_module

    module = build_bill_module(database=rt.database)

    builder = module.declaration.graph_builder
    assert builder.keywords.get("database") is rt.database
