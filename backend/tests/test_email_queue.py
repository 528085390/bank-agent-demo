"""邮件通知模块单元测试。

覆盖：配置校验、摘要归一化、模板渲染、附件生成、队列配置与（Redis 可达时的）入队消费。
需要外部依赖的用例（reportlab/openpyxl）在缺失时自动 skip；队列用 fakeredis 离线验证。

运行：
    uv run pytest tests/test_email_queue.py -v
"""

import pytest

from app.notification.config import EmailConfig
from app.notification.queue_config import QueueConfig
from app.notification.summary import normalize_summary

# --------------------------------------------------------------------------- 配置与数据


def test_email_config_requires_credentials():
    cfg = EmailConfig(smtp_host="smtp.qq.com", smtp_port=465, smtp_user="", smtp_password="")
    assert cfg.validate_config() is False
    cfg2 = EmailConfig(
        smtp_host="smtp.qq.com", smtp_port=465, smtp_user="a@b.com", smtp_password="x"
    )
    assert cfg2.validate_config() is True


def test_queue_config_retry_delay():
    cfg = QueueConfig()
    assert cfg.retry_delay(0) == 60
    assert cfg.retry_delay(1) == 300
    assert cfg.retry_delay(2) == 900
    assert cfg.retry_delay(3) is None


def test_normalize_summary_computes_net_and_categories():
    summary = {
        "period": "2026-09",
        "record_count": 3,
        "credit_total": "0.00",
        "debit_total": "686.50",
        "matched_total": "186.50",
        "categories": [{"category": "咖啡", "amount": "186.50"}],
    }
    ctx = normalize_summary(summary)
    assert ctx["period"] == "2026-09"
    assert ctx["credit_total"] == "0.00"
    assert ctx["debit_total"] == "686.50"
    assert ctx["net_total"] == "-686.50"
    assert ctx["record_count"] == 3
    assert ctx["categories"][0]["amount"] == "186.50"


def test_normalize_summary_tolerates_missing_values():
    ctx = normalize_summary({})
    assert ctx["net_total"] == "0.00"
    assert ctx["categories"] == []


def test_email_service_returns_false_when_unreachable():
    """SMTP 服务器不可达时，send_email 应返回 False 而非抛异常（供审计落 FAILED）。"""
    from app.notification.config import EmailConfig
    from app.notification.email_service import EmailService

    cfg = EmailConfig(
        smtp_host="127.0.0.1",
        smtp_port=1,
        smtp_user="a@b.com",
        smtp_password="x",
        smtp_use_ssl=False,
    )
    service = EmailService(cfg)
    ok = service.send_email("t@example.com", "subject", "<p>hi</p>", retry_times=1)
    assert ok is False


def test_email_queue_raises_when_redis_unavailable():
    """Redis 不可达时 EmailQueue 构造抛异常，供 NotificationService 降级为同步发送。"""
    import pytest as _pytest

    from app.notification.queue import EmailQueue
    from app.notification.queue_config import QueueConfig

    with _pytest.raises(Exception):
        EmailQueue(QueueConfig(redis_url="redis://127.0.0.1:6390/0"))


# --------------------------------------------------------------------------- 模板渲染


def test_render_bill_summary():
    from app.notification.templates import render_bill_summary

    html = render_bill_summary(
        {
            "period": "2026-09",
            "credit_total": "0.00",
            "debit_total": "686.50",
            "net_total": "-686.50",
            "record_count": 3,
            "categories": [{"category": "咖啡", "amount": "186.50", "count": 1}],
            "quality_issues": [],
        }
    )
    assert "总收入" in html
    assert "总支出" in html
    assert "咖啡" in html


def test_render_bill_summary_accepts_period_range():
    from app.notification.templates import render_bill_summary

    html = render_bill_summary(
        {
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "credit_total": "5000.00",
            "debit_total": "3210.55",
            "net_total": "1789.45",
            "record_count": 42,
            "categories": [],
            "quality_issues": [],
        }
    )
    assert "2026-01-01" in html
    assert "2026-01-31" in html


# --------------------------------------------------------------------------- 附件生成


def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has("reportlab"), reason="reportlab 未安装")
def test_build_pdf_report():
    from app.notification.attachments import build_pdf_report

    data = build_pdf_report(
        {
            "period": "2026-09",
            "credit_total": "0.00",
            "debit_total": "686.50",
            "record_count": 3,
            "categories": [{"category": "咖啡", "amount": "186.50", "count": 1}],
        }
    )
    assert data[:4] == b"%PDF"


@pytest.mark.skipif(not _has("openpyxl"), reason="openpyxl 未安装")
def test_build_excel_report():
    from app.notification.attachments import build_excel_report

    data = build_excel_report(
        {
            "period": "2026-09",
            "credit_total": "0.00",
            "debit_total": "686.50",
            "record_count": 3,
            "categories": [{"category": "咖啡", "amount": "186.50", "count": 1}],
        }
    )
    assert data[:2] == b"PK"  # xlsx 是 zip 容器


# --------------------------------------------------------------------------- 队列（fakeredis 离线验证）


def _patch_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    monkeypatch.setattr(
        "app.notification.queue._redis_client",
        lambda cfg: fakeredis.FakeRedis(decode_responses=True),
    )
    return fakeredis


def test_enqueue_and_priority(monkeypatch):
    _patch_redis(monkeypatch)
    from app.notification.queue import EmailQueue

    queue = EmailQueue(QueueConfig())
    job_id = queue.enqueue(
        "bill_summary", "task_1", "t@example.com", {"user_id": "u1", "summary": {}}
    )
    assert job_id.startswith("bill_summary:task_1:")
    assert queue.queue_length() == 1
    assert queue.peek()[0]["recipient"] == "t@example.com"
    # 高优先级插队首
    queue.enqueue(
        "bill_summary", "task_2", "t2@example.com", {"user_id": "u1", "summary": {}}, priority=1
    )
    assert queue.peek()[0]["task_id"] == "task_2"


def test_worker_failure_retry_and_dlq(monkeypatch):
    _patch_redis(monkeypatch)
    from app.notification.queue import EmailWorker

    worker = EmailWorker(
        lambda job: {"status": "FAILED", "error": "boom"}, QueueConfig(retry_delays=[1, 2])
    )
    base = {
        "job_id": "bill_summary:task_x:1",
        "task_type": "bill_summary",
        "task_id": "task_x",
        "recipient": "t@example.com",
        "payload": {},
        "retry_count": 0,
    }

    # 第一次失败 → 进入延迟重试队列
    worker._handle_failure(dict(base), "boom")
    assert worker._redis.zcard(worker.config.retry_queue) == 1

    # 重试到期 → 回到主队列
    for item in worker._redis.zrangebyscore(worker.config.retry_queue, 0, float("inf")):
        worker._redis.zadd(worker.config.retry_queue, {item: 0})
    worker._drain_retry_queue()
    assert worker._redis.llen(worker.config.queue_name) == 1
    assert worker._redis.zcard(worker.config.retry_queue) == 0

    # 重试次数超限 → 进入死信队列
    worker._handle_failure(dict(base, retry_count=2), "boom")
    assert worker._redis.llen(worker.config.dead_letter_queue) == 1
