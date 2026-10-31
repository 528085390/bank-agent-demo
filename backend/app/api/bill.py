"""账单任务 HTTP API。

统一响应信封沿用基底的 ``{request_id, data, error}``；鉴权复用 ``app.api.routes`` 的
``identity``。密码提交后由后台线程推进处理（``start_processing``）。
"""

from datetime import date

from fastapi import APIRouter, File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.bill_schemas import CreateTaskRequest, SubmitPasswordRequest
from app.api.routes import envelope, identity, runtime
from app.core.bill_config import get_bill_settings
from app.core.bill_errors import FILE_TOO_LARGE
from app.core.errors import AppError
from app.services.bill_analysis import BillAnalyzer
from app.services.bill_import import document_for_task
from app.services.bill_processing import start_processing
from app.services.bill_query import BillQueryService
from app.services.bill_task import BillTaskService

router = APIRouter(prefix="/api/bill", tags=["bill"])


def _service(request: Request) -> BillTaskService:
    return BillTaskService(runtime(request).database)


@router.post("/tasks")
async def create_task(body: CreateTaskRequest, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(
        _service(request).create_task,
        who["user_id"],
        body.bank_code,
        body.period_start,
        body.period_end,
    )
    return envelope(request, view)


@router.post("/upload")
async def upload_once(
    request: Request,
    file: UploadFile = File(...),
    # 手动上传不要求周期：解析结果才是真正的起止，解析完由 backfill_period 回填。
    period_start: date | None = Form(None),
    period_end: date | None = Form(None),
    bank_code: str = Form("CMB"),
    password: str = Form(""),
):
    """一次性上传：创建任务 → 确认申请 → 上传文件 → 开始处理。

    前端的「上传流水」单页表单用这一个接口即可；需要用户先去手机银行申请、
    或分步补交密码的流程，走 /tasks/* 的分步接口。两者共用同一套服务与数据。
    密码可留空：文件未加密时直接处理；实际需要密码时任务回到 WAITING_PASSWORD。
    """

    who = await identity(request)
    user_id = who["user_id"]
    database = runtime(request).database
    service = _service(request)

    settings = get_bill_settings()
    limit = settings.bill_max_file_size_mb * 1024 * 1024
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise AppError(FILE_TOO_LARGE, f"文件过大，最大 {settings.bill_max_file_size_mb}MB", 413)

    created = await run_in_threadpool(
        service.create_task, user_id, bank_code, period_start, period_end
    )
    task_id = created["task_id"]
    await run_in_threadpool(service.confirm_application, task_id, user_id)
    await run_in_threadpool(
        service.save_upload, task_id, user_id, bytes(data), file.filename or "upload"
    )
    if password.strip():
        view = await run_in_threadpool(service.submit_password, task_id, user_id, password)
    else:
        view = await run_in_threadpool(service.begin_processing, task_id, user_id)

    start_processing(task_id, password or None, database)
    return envelope(request, view)


@router.post("/tasks/{task_id}/application-confirm")
async def confirm_application(task_id: str, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(_service(request).confirm_application, task_id, who["user_id"])
    return envelope(request, view)


@router.post("/tasks/{task_id}/upload-file")
async def upload_file(task_id: str, request: Request, file: UploadFile = File(...)):
    who = await identity(request)
    settings = get_bill_settings()
    limit = settings.bill_max_file_size_mb * 1024 * 1024
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise AppError(FILE_TOO_LARGE, f"文件过大，最大 {settings.bill_max_file_size_mb}MB", 413)
    view = await run_in_threadpool(
        _service(request).save_upload, task_id, who["user_id"], bytes(data), file.filename or "upload"
    )
    return envelope(request, view)


@router.post("/tasks/{task_id}/begin-processing")
async def begin_processing(task_id: str, request: Request):
    """不带密码直接开始处理。

    文件未必加密——Excel / CSV 根本不需要密码，硬要用户先填一个纯属拦路。所以上传完
    就可以走这条起处理；**确实**需要密码的（加密 PDF / 压缩包）会在后台失败并退回
    WAITING_PASSWORD，前端那时再提示输入即可。
    """

    who = await identity(request)
    database = runtime(request).database
    view = await run_in_threadpool(_service(request).begin_processing, task_id, who["user_id"])
    start_processing(task_id, None, database)
    return envelope(request, view)


@router.post("/tasks/{task_id}/password")
async def submit_password(task_id: str, body: SubmitPasswordRequest, request: Request):
    who = await identity(request)
    database = runtime(request).database
    view = await run_in_threadpool(
        _service(request).submit_password, task_id, who["user_id"], body.password
    )
    start_processing(task_id, body.password, database)
    return envelope(request, view)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(_service(request).get_task, task_id, who["user_id"])
    return envelope(request, view)


@router.get("/tasks/{task_id}/summary")
async def get_summary(task_id: str, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(_service(request).get_summary, task_id, who["user_id"])
    return envelope(request, view)


def _task_analysis(request: Request, task_id: str, user_id: str):
    """按「这份任务对应的账单文档」分析，而不是按任务周期。

    任务周期在上传时可以为空（迁移 0018），解析完成后才回填；即便回填了，它也只是
    展示用字段。真正要分析的是这份文件里的流水，所以这里按 ``document_id`` 取数 ——
    与聊天里「分析刚导入的流水」走同一口径。
    """

    database = runtime(request).database
    with database.sessions() as db:
        doc = document_for_task(db, task_id, user_id)
        if doc is None:
            raise AppError("ANALYSIS_NOT_READY", "这份账单还没有解析出流水", 409)
        # 在会话内取好值：离开 with 之后只剩下一个 detached 实例。
        period_start, period_end, document_id = doc.period_start, doc.period_end, doc.id
    return BillAnalyzer(database).analyze(
        user_id, period_start, period_end, None, document_id=document_id
    )


@router.get("/tasks/{task_id}/analysis")
async def get_analysis(task_id: str, request: Request):
    """返回当前账单任务的完整深度分析结果。"""

    who = await identity(request)
    task = await run_in_threadpool(_service(request).get_task, task_id, who["user_id"])
    if task["state"] != "COMPLETED":
        raise AppError("ANALYSIS_NOT_READY", "账单尚未处理完成", 409)
    result = await run_in_threadpool(_task_analysis, request, task_id, who["user_id"])
    return envelope(request, result)


@router.get("/tasks/{task_id}/transactions")
async def get_transactions(
    task_id: str,
    request: Request,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    direction: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """分页查询明细。筛选参数都是可选的，省略即不筛选。"""

    who = await identity(request)
    view = await run_in_threadpool(
        _service(request).get_transactions,
        task_id, who["user_id"], page, page_size,
        category=category, direction=direction, date_from=date_from, date_to=date_to,
    )
    return envelope(request, view)


@router.get("/transactions")
async def query_transactions(
    request: Request,
    period_start: date,
    period_end: date,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    direction: str | None = None,
):
    """按期间分页查询当前用户的流水明细，供聊天里的查询表格翻页。

    和 `bill_query_transactions` 工具共用同一个查询服务；区别只是入口在 HTTP，
    因此不需要再过一次模型。只读，不写任何状态。
    """

    who = await identity(request)
    service = BillQueryService(runtime(request).database)
    view = await run_in_threadpool(
        service.get_transactions,
        who["user_id"],
        period_start,
        period_end,
        category=category,
        direction=direction,
        page=page,
        page_size=page_size,
    )
    return envelope(request, view)


@router.get("/tasks/{task_id}/audit")
async def get_audit(task_id: str, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(_service(request).get_audit, task_id, who["user_id"])
    return envelope(request, view)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    who = await identity(request)
    view = await run_in_threadpool(_service(request).cancel_task, task_id, who["user_id"])
    return envelope(request, view)
