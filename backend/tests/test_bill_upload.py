"""账单上传：大小上限、坏文件的失败分类，以及文件哈希的归属范围。

前三条用例钉住的是**已搬来的**代码的既有行为，不是驱动新代码：

* 超限文件在路由层就被挡下（app/api/bill.py 的 upload-file），错误码 FILE_TOO_LARGE、
  HTTP 413，响应走基底信封，所以错误码在 error.code。
* 处理失败时任务视图的 error 是 {"code", "message"}（见 bill_task.py 的 _task_view），
  码来自两条来源：app/billing/errors.py 的 BillingError.code，或
  app/core/bill_errors.py 的 PARSE_FAILED / FILE_NOT_FOUND 等。

后三条（跨用户同文件、同用户重传、插入竞态）驱动的是 Task 6 的新行为：
`statement_document` 的文件哈希唯一性**按用户划分**（UNIQUE(user_id, source_file_hash)），
所以第二个用户上传字节相同的文件必须**成功**，而不是撞全局唯一约束后落 FAILED/INTERNAL；
真正撞上约束的那一次要变成 DUPLICATE_IMPORT（409），绝不能是 INTERNAL。

处理一律用同步的 process_task 推进，不等 start_processing 起的 daemon 线程——
线程会让断言撞上未完成的处理。
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.bill_config import get_bill_settings
from app.main import create_app
from app.services.bill_import import (
    BillImportService,
    DocumentInfo,
    ParseResult,
    TxRecord,
)
from app.services.bill_processing import process_task
from app.storage.bill_models import BillTask, StatementDocument, Transaction
from app.storage.models import AppUser, LoginSession


@pytest.fixture(autouse=True)
def _clear_bill_settings_cache():
    """get_bill_settings 是进程级 @lru_cache，而 monkeypatch 只还原环境变量。

    不在每个用例前后各清一次，缓存里的 Settings 就会活到同一个 xdist worker 的下一个
    用例——后续任何上传 >1MB 账单的测试都会被误判成 413。
    """

    get_bill_settings.cache_clear()
    yield
    get_bill_settings.cache_clear()


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def _create_uploadable_task(client):
    """建任务 → 确认申请，拿到可以 upload-file 的 WAITING_UPLOAD 任务。"""

    created = client.post(
        "/api/bill/tasks",
        json={
            "bank_code": "CMB",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
        },
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["data"]["task_id"]

    confirmed = client.post(f"/api/bill/tasks/{task_id}/application-confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["state"] == "WAITING_UPLOAD"

    return task_id


def test_a_task_may_cover_a_single_day(rt, session):
    client = client_for(rt, session)

    response = client.post(
        "/api/bill/tasks",
        json={
            "bank_code": "CMB",
            "period_start": "2026-09-01",
            "period_end": "2026-09-01",
        },
    )

    assert response.status_code == 200, response.text
    task = response.json()["data"]
    assert task["period_start"] == "2026-09-01"
    assert task["period_end"] == "2026-09-01"


def test_a_task_cannot_end_before_it_starts(rt, session):
    client = client_for(rt, session)

    response = client.post(
        "/api/bill/tasks",
        json={
            "bank_code": "CMB",
            "period_start": "2026-09-02",
            "period_end": "2026-09-01",
        },
    )

    # 这个项目把 VALIDATION 映射为 422（见 app/core/bill_errors.py 的 STATUS）。
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION"


def test_upload_over_the_size_limit_reports_file_too_large(rt, session, monkeypatch):
    monkeypatch.setenv("BILL_MAX_FILE_SIZE_MB", "1")
    # 缓存由 autouse 的 _clear_bill_settings_cache 在用例前后各清一次（见文件开头）。

    client = client_for(rt, session)
    task_id = _create_uploadable_task(client)

    response = client.post(
        f"/api/bill/tasks/{task_id}/upload-file",
        files={"file": ("big.csv", b"a" * (2 * 1024 * 1024), "text/csv")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_unrecognized_bytes_fail_with_parse_failed(rt, session):
    client = client_for(rt, session)
    task_id = _create_uploadable_task(client)

    # 既不是已知魔数、也不是可解码的文本（含 NUL 字节，decode_text 直接放弃）→
    # detect_type 归为 unknown → 没有处理器 → result.text 为 None →
    # bill_processing 走 _fail(PARSE_FAILED, "未能提取文本内容")。
    payload = b"\x00\x01\x02\x03" * 256
    uploaded = client.post(
        f"/api/bill/tasks/{task_id}/upload-file",
        files={"file": ("statement.bin", payload, "application/octet-stream")},
    )
    assert uploaded.status_code == 200, uploaded.text

    process_task(task_id, None, rt.database)

    task = client.get(f"/api/bill/tasks/{task_id}").json()["data"]
    assert task["state"] == "FAILED"
    assert task["error"]["code"] == "PARSE_FAILED"


def test_a_corrupt_zip_fails_with_attachment_unsupported(rt, session):
    client = client_for(rt, session)
    task_id = _create_uploadable_task(client)

    # 魔数像 ZIP（PK\x03\x04）但读不出中央目录：detect._detect_zip_family 的注释自己写了
    # 这条路径"仍归为 zip，由 archive 处理器报 ATTACHMENT_UNSUPPORTED"，
    # archive._read_zip 确实抛 unsupported(...) → bill_processing 走 _fail(exc.code)。
    # 点名它，是为了钉住"用户的坏包不得落 INTERNAL"。
    payload = b"PK\x03\x04" + b"\x00" * 512
    uploaded = client.post(
        f"/api/bill/tasks/{task_id}/upload-file",
        files={"file": ("statement.zip", payload, "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text

    process_task(task_id, None, rt.database)

    task = client.get(f"/api/bill/tasks/{task_id}").json()["data"]
    assert task["state"] == "FAILED"
    assert task["error"]["code"] == "ATTACHMENT_UNSUPPORTED"


# --- Task 6：文件哈希按归属人唯一 ---------------------------------------------

#: 固定内容：两个数据行，行哈希由 (时刻, 金额, 对手方) 决定。
STATEMENT_CSV = (
    "交易日期,摘要,金额,余额\n"
    "2026-09-05,工资,12000.00,15000.00\n"
    "2026-09-08,超市,-320.50,14679.50\n"
).encode("utf-8")

#: 两个数据行都解析出来时的交易条数。
STATEMENT_ROWS = 2


def second_session(rt) -> dict:
    """user_0002 的真实登录会话：跨用户行为要走 HTTP 才算数。

    P1 之后浏览器凭证是 token，不是 `session_id`：行里必须带 `token_hash` 与
    `absolute_expires_at`，否则 `authenticate()` 直接拒绝。这里手工签发而不是走
    `/api/auth/register`，是为了让这条用例只回答「同一个文件两个用户都能导」这一个问题 ——
    注册还会顺带领一个银行客户，那是另一件事。
    """

    from datetime import datetime, timedelta, timezone

    from app.identity.tokens import digest, new_session_token

    now = datetime.now(timezone.utc)
    token = new_session_token()
    with rt.database.sessions.begin() as db:
        if not db.scalar(select(AppUser).where(AppUser.user_id == "user_0002")):
            db.add(AppUser(user_id="user_0002"))
            db.flush()
        row = LoginSession(
            session_id=f"sess_other_{uuid4().hex}",
            user_id="user_0002",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=1),
            token_hash=digest(token),
            last_seen_at=now,
        )
        db.add(row)
    return {"session_id": row.session_id, "token": token, "user_id": "user_0002"}


def _import_statement(client, rt, task_id: str, payload: bytes) -> None:
    """上传固定内容，再用同步的 process_task 把它推到终态。"""

    uploaded = client.post(
        f"/api/bill/tasks/{task_id}/upload-file",
        files={"file": ("statement.csv", payload, "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    process_task(task_id, None, rt.database)


def _task_view(client, task_id: str) -> dict:
    response = client.get(f"/api/bill/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _document_task_nos(rt) -> set[str]:
    """有 statement_document 行的任务号（不读 user_id：去重用例与列的存在与否无关）。"""

    with rt.database.sessions.begin() as db:
        task_nos = {task.id: task.task_no for task in db.scalars(select(BillTask)).all()}
        return {task_nos[doc.task_id] for doc in db.scalars(select(StatementDocument)).all()}


def _document_owners(rt) -> dict:
    """statement_document 的归属快照：task_no → (user_id, source_file_hash)。"""

    with rt.database.sessions.begin() as db:
        task_nos = {task.id: task.task_no for task in db.scalars(select(BillTask)).all()}
        return {
            task_nos[doc.task_id]: (doc.user_id, doc.source_file_hash)
            for doc in db.scalars(select(StatementDocument)).all()
        }


def _transaction_counts(rt) -> dict:
    """bill_transaction 按任务归组计数：task_no → 行数。"""

    with rt.database.sessions.begin() as db:
        task_nos = {task.id: task.task_no for task in db.scalars(select(BillTask)).all()}
        counts: dict[str, int] = {}
        for row in db.scalars(select(Transaction)).all():
            task_no = task_nos[row.task_id]
            counts[task_no] = counts.get(task_no, 0) + 1
        return counts


def test_an_empty_placeholder_is_repaired_by_a_later_parse(rt):
    """第一次解析 0 条留下的空文档，不能挡住解析器修好后的同一文件。"""

    file_hash = uuid4().hex
    with rt.database.sessions.begin() as db:
        old_task = BillTask(
            task_no=f"BT-{uuid4().hex[:12]}",
            user_id="user_0001",
            bank_code="CMB",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            state="COMPLETED",
            source_mode="manual_upload",
            uploaded_file_hash=file_hash,
            password_attempts_left=3,
        )
        new_task = BillTask(
            task_no=f"BT-{uuid4().hex[:12]}",
            user_id="user_0001",
            bank_code="CMB",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            state="WAITING_PASSWORD",
            source_mode="manual_upload",
            uploaded_file_hash=file_hash,
            password_attempts_left=3,
        )
        db.add_all([old_task, new_task])
        db.flush()
        placeholder = StatementDocument(
            task_id=old_task.id,
            user_id="user_0001",
            bank_code="CMB",
            source_file_hash=file_hash,
            original_filename="old.pdf",
            account_holder_masked="",
            account_no_masked="",
            account_type="一卡通",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            parser_name="cmb_parser",
            parser_version="1.0",
        )
        db.add(placeholder)
        db.flush()
        placeholder_id = placeholder.id
        new_task_no = new_task.task_no
        new_task_id = new_task.id

    info = DocumentInfo(
        bank_code="CMB",
        file_hash=file_hash,
        filename="new.pdf",
        account_holder="李某某",
        account_no="",
        account_type="一卡通",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        parser_name="cmb_parser",
        parser_version="1.1",
    )
    tx = TxRecord(
        booked_date=date(2026, 5, 5),
        amount=Decimal("-10.00"),
        currency="CNY",
        balance=None,
        transaction_type="消费-餐饮",
        counterparty="美团点评-肠粉王店",
        source_page=1,
        source_row_index=1,
        booked_at=datetime(2026, 5, 5, 9, 15),
    )

    result = BillImportService(rt.database).save_statement(
        new_task_no, ParseResult(document_info=info, transactions=[tx])
    )

    assert result.duplicate is False
    assert result.success == 1
    assert result.document_id == str(placeholder_id)
    with rt.database.sessions() as db:
        document = db.get(StatementDocument, placeholder_id)
        rows = db.scalars(
            select(Transaction).where(Transaction.statement_document_id == placeholder_id)
        ).all()
        assert document.task_id == new_task_id
        assert len(rows) == 1
        assert rows[0].amount == 1000


def test_a_second_user_can_import_the_same_file(rt, session):
    """第二个用户传字节相同的文件必须成功：哈希的唯一性是「每人一份」，不是全局一份。

    修复前：文件级去重 SELECT 带 BillTask.user_id 过滤 → 看不到别人的文档 → 照样
    INSERT → 撞全局 UNIQUE(source_file_hash) → IntegrityError → 裸 except 把任务记成
    FAILED/INTERNAL，而「这文件我导过」根本不成立。
    """

    first = client_for(rt, session)
    task_a = _create_uploadable_task(first)
    _import_statement(first, rt, task_a, STATEMENT_CSV)
    assert _task_view(first, task_a)["state"] == "COMPLETED"

    second = client_for(rt, second_session(rt))
    task_b = _create_uploadable_task(second)
    _import_statement(second, rt, task_b, STATEMENT_CSV)

    view_b = _task_view(second, task_b)
    assert view_b["state"] == "COMPLETED", view_b
    assert view_b["error"] is None, view_b

    owners = _document_owners(rt)
    assert set(owners) == {task_a, task_b}
    assert owners[task_a][0] == session["user_id"]
    assert owners[task_b][0] == "user_0002"
    # 两份文档确实是同一份文件（同一内容哈希）——唯一性只按归属人划分。
    assert owners[task_a][1] == owners[task_b][1]

    # B 的交易挂在自己的任务上，A 的那两条一行没多、一行没少。
    assert _transaction_counts(rt) == {task_a: STATEMENT_ROWS, task_b: STATEMENT_ROWS}
    assert _task_view(first, task_a)["state"] == "COMPLETED"


def test_the_same_user_reimporting_the_same_file_is_a_duplicate(rt, session):
    """同一用户重传同一份文件：不新增文档、不新增交易，任务照样完成。"""

    client = client_for(rt, session)
    first_task = _create_uploadable_task(client)
    _import_statement(client, rt, first_task, STATEMENT_CSV)
    assert _task_view(client, first_task)["state"] == "COMPLETED"

    second_task = _create_uploadable_task(client)
    _import_statement(client, rt, second_task, STATEMENT_CSV)

    view = _task_view(client, second_task)
    assert view["state"] == "COMPLETED", view
    assert view["error"] is None, view

    assert _document_task_nos(rt) == {first_task}
    assert _transaction_counts(rt) == {first_task: STATEMENT_ROWS}

    # 去重不该让第二个任务读到一份空账：摘要仍取自那份已有文档。
    summary = client.get(f"/api/bill/tasks/{second_task}/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["data"]["record_count"] == STATEMENT_ROWS


def test_a_lost_insert_race_is_reported_as_duplicate_import(rt, session, monkeypatch):
    """去重查询落空、INSERT 真撞约束时，必须是 DUPLICATE_IMPORT(409)，不是 INTERNAL。

    竞态：两个请求都在对方提交之前查完——文件级与记录级两个查询都没看到对方那笔写入，
    于是都走到 INSERT，先提交的那个赢。这里把两个查询都 monkeypatch 成「什么都没查到」，
    确定性地复现那一刻；随后第二个 INSERT 撞上 uq_statement_document_user_file。
    """

    from app.core.bill_errors import status_of
    from app.services import bill_import

    client = client_for(rt, session)
    first_task = _create_uploadable_task(client)
    _import_statement(client, rt, first_task, STATEMENT_CSV)
    assert _task_view(client, first_task)["state"] == "COMPLETED"

    monkeypatch.setattr(BillImportService, "_existing_document", lambda self, db, task: None)
    monkeypatch.setattr(bill_import, "_hashes_user_already_has", lambda db, user_id, hashes: set())

    second_task = _create_uploadable_task(client)
    _import_statement(client, rt, second_task, STATEMENT_CSV)

    view = _task_view(client, second_task)
    assert view["state"] == "FAILED", view
    assert view["error"]["code"] == "DUPLICATE_IMPORT", view
    assert status_of(view["error"]["code"]) == 409

    # 输掉竞态的那次不留半条数据：没有新文档、没有新交易。
    assert _document_task_nos(rt) == {first_task}
    assert _transaction_counts(rt) == {first_task: STATEMENT_ROWS}



# --- 上传不再要求日期：任务周期在解析完成后回填 --------------------------------------


def test_a_task_can_be_created_without_a_period(rt, session):
    """上传卡片不再问日期：周期先空着，解析完成后再由文档回填。"""

    client = client_for(rt, session)
    created = client.post("/api/bill/tasks", json={"bank_code": "CMB"})

    assert created.status_code == 200, created.text
    task = created.json()["data"]
    assert task["period_start"] is None
    assert task["period_end"] is None


def test_the_task_period_is_filled_in_from_the_parsed_statement(rt, session):
    """解析成功后，任务周期取这份文件里交易的真实起止，而不是用户填的东西。"""

    client = client_for(rt, session)
    task_id = client.post("/api/bill/tasks", json={"bank_code": "CMB"}).json()["data"][
        "task_id"
    ]
    client.post(f"/api/bill/tasks/{task_id}/application-confirm")

    _import_statement(client, rt, task_id, STATEMENT_CSV)

    view = _task_view(client, task_id)
    assert view["state"] == "COMPLETED"
    # STATEMENT_CSV 的两行是 2026-09-05 与 2026-09-08。
    assert view["period_start"] == "2026-09-05"
    assert view["period_end"] == "2026-09-08"


def test_a_reupload_with_a_different_file_hash_still_resolves_to_the_statement(rt, session):
    """内容相同、文件指纹不同（同一份流水换个容器）时，任务仍能找到已入库的流水。

    压缩包与解压后的 PDF 就是这种关系：行级去重会发现每一笔都已存在，于是不建新文档；
    但任务不能因此变成"查不到流水"—— 用解析结果里的期间做回退，把已有文档找回来。
    """

    client = client_for(rt, session)
    first = _create_uploadable_task(client)
    _import_statement(client, rt, first, STATEMENT_CSV)
    assert _task_view(client, first)["state"] == "COMPLETED"

    second = _create_uploadable_task(client)
    # 加一个 UTF-8 BOM：文件哈希变了，解析出来的交易一模一样。
    _import_statement(client, rt, second, b"\xef\xbb\xbf" + STATEMENT_CSV)

    view = _task_view(client, second)
    assert view["state"] == "COMPLETED"
    # 没有新文档，但周期按解析结果补上了（2026-09-05 至 2026-09-08）。
    assert view["period_start"] == "2026-09-05"
    assert view["period_end"] == "2026-09-08"

    rows = client.get(f"/api/bill/tasks/{second}/transactions").json()["data"]
    assert rows["total"] == STATEMENT_ROWS
