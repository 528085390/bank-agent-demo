"""账单邮件通知服务：渲染、发送与审计落库。

发送链路支持两种模式：
- 队列模式（email_queue_enabled 且 Redis 可达）：发送请求入队，由 Worker 异步消费；
- 同步模式（默认 / 降级）：直接 SMTP 发送，失败进程内重试。

无论哪种模式，每次发送结果都会写入 email_notification 审计表。
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import AppError
from app.core.types import new_id
from app.notification.attachments import Attachment
from app.notification.config import EmailConfig
from app.notification.email_service import EmailService
from app.notification.queue import EmailQueue
from app.notification.queue_config import QueueConfig
from app.notification.summary import normalize_summary
from app.notification.templates import (
    render_anomaly_alert,
    render_bill_report,
    render_bill_summary,
)
from app.storage.database import Database
from app.storage.models import AppUser, EmailNotification

logger = logging.getLogger(__name__)


class NotificationService:
    """对账单任务发送通知邮件，并把每一次发送结果写入 email_notification 审计表。"""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self._email_service: EmailService | None = None
        self._queue: EmailQueue | None = self._build_queue()

    def _build_queue(self) -> EmailQueue | None:
        if not self.settings.email_queue_enabled:
            return None
        try:
            queue = EmailQueue(QueueConfig.from_settings(self.settings))
            logger.info("邮件队列已启用 redis_url=%s", self.settings.email_queue_redis_url)
            return queue
        except Exception as exc:
            logger.warning("邮件队列不可用，降级为同步发送: %s", exc)
            return None

    @property
    def email_service(self) -> EmailService:
        if self._email_service is None:
            self._email_service = EmailService(EmailConfig.from_settings(self.settings))
        return self._email_service

    @property
    def queue_enabled(self) -> bool:
        return self._queue is not None

    def email_configured(self) -> bool:
        """SMTP 凭据是否完整；未配置时一切发送安全跳过（不抛错）。"""
        return EmailConfig.from_settings(self.settings).validate_config()

    def get_user_email(self, user_id: str) -> str | None:
        with self.database.sessions() as db:
            user = db.scalar(select(AppUser).where(AppUser.user_id == user_id))
            return user.email if user else None

    def set_user_email(self, user_id: str, email: str) -> str:
        with self.database.sessions.begin() as db:
            user = db.scalar(select(AppUser).where(AppUser.user_id == user_id))
            if not user:
                raise AppError("USER_NOT_FOUND", "用户不存在", 404)
            user.email = email
            return email

    def clear_user_email(self, user_id: str) -> None:
        """解除邮箱绑定，回到「未绑定」状态。

        绑定是报告唯一的收件地址，所以必须能撤销 —— 否则地址填错之后只能改绑到另一个，
        回不到「未绑定」。未找到用户时按 404 报错，与 set_user_email 一致。
        """

        with self.database.sessions.begin() as db:
            user = db.scalar(select(AppUser).where(AppUser.user_id == user_id))
            if not user:
                raise AppError("USER_NOT_FOUND", "用户不存在", 404)
            user.email = None

    def send_bill_summary(
        self, user_id: str, task_id: str, summary: dict, *, recipient_email: str | None = None
    ) -> dict | None:
        """账单分析完成后发送摘要邮件；SMTP 或收件人未配置时安全跳过。"""
        if not self.email_configured():
            logger.info("跳过邮件发送：SMTP 未配置 task_id=%s", task_id)
            return None
        recipient = recipient_email or self.get_user_email(user_id)
        if not recipient:
            logger.info("跳过邮件发送：用户未配置邮箱 user_id=%s task_id=%s", user_id, task_id)
            return None
        if self._queue is not None:
            job_id = self._queue.enqueue(
                "bill_summary", task_id, recipient, {"user_id": user_id, "summary": summary}
            )
            return {"queued": True, "job_id": job_id}
        return self._send_bill_summary_sync(user_id, task_id, summary, recipient)

    def send_bill_report(
        self,
        task_id: str,
        recipient_email: str,
        summary: dict,
        *,
        attachments: list[str | Attachment] | None = None,
    ) -> dict | None:
        """发送带附件的账单明细报告（手动触发）。"""
        if not self.email_configured():
            logger.info("跳过邮件发送：SMTP 未配置 task_id=%s", task_id)
            return None
        if self._queue is not None:
            job_id = self._queue.enqueue(
                "bill_report",
                task_id,
                recipient_email,
                {"summary": summary, "attachments": _serialize_attachments(attachments)},
            )
            return {"queued": True, "job_id": job_id}
        return self._send_bill_report_sync(
            task_id, recipient_email, summary, attachments=attachments
        )

    def send_anomaly_alert(
        self, task_id: str, recipient_email: str, alerts: list[dict]
    ) -> dict | None:
        """发送异常交易提醒邮件。"""
        if not self.email_configured():
            return None
        if self._queue is not None:
            job_id = self._queue.enqueue(
                "anomaly_alert", task_id, recipient_email, {"alerts": alerts}
            )
            return {"queued": True, "job_id": job_id}
        return self._send_anomaly_alert_sync(task_id, recipient_email, alerts)

    def send_email(
        self,
        *,
        recipient: str,
        subject: str,
        html: str,
        attachments: list[str | Attachment] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """发送一封通用邮件，复用同一套审计与队列设施。

        SMTP 未配置时返回 FAILED 而不是抛错（与既有发送入口的安全跳过一致）；队列可用时
        入队，否则同步发送。返回 {"status": "SENT" | "QUEUED" | "FAILED", ...}。
        """

        if not self.email_configured():
            return {"status": "FAILED", "error": "SMTP 未配置"}
        task_id = task_id or new_id("email_task")
        if self._queue is not None:
            job_id = self._queue.enqueue(
                "generic",
                task_id,
                recipient,
                {
                    "subject": subject,
                    "html": html,
                    "attachments": _serialize_attachments(attachments),
                },
            )
            return {"status": "QUEUED", "queued": True, "job_id": job_id}
        return self._send_and_record(
            task_id, recipient, "REPORT", subject, html, attachments=attachments
        )

    def _send_bill_summary_sync(
        self, user_id: str, task_id: str, summary: dict, recipient: str
    ) -> dict[str, Any]:
        context = normalize_summary(summary)
        subject = f"账单分析完成 - {context['period']}"
        html = render_bill_summary(context)
        return self._send_and_record(task_id, recipient, "SUMMARY", subject, html)

    def _send_bill_report_sync(
        self,
        task_id: str,
        recipient: str,
        summary: dict,
        *,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        context = normalize_summary(summary)
        subject = f"账单明细报告 - {context['period']}"
        html = render_bill_report(context)
        return self._send_and_record(
            task_id, recipient, "REPORT", subject, html, attachments=attachments
        )

    def _send_anomaly_alert_sync(
        self, task_id: str, recipient: str, alerts: list[dict]
    ) -> dict[str, Any]:
        subject = "账单异常交易提醒"
        html = render_anomaly_alert({"alerts": alerts})
        return self._send_and_record(task_id, recipient, "ALERT", subject, html)

    def send_job(self, job: dict) -> dict[str, Any] | None:
        """Worker 消费入口：按 task_type 调用同步发送（避免再次入队）。"""
        task_type = job["task_type"]
        task_id = job["task_id"]
        recipient = job["recipient"]
        payload = job["payload"]
        if task_type == "bill_summary":
            return self._send_bill_summary_sync(
                payload["user_id"], task_id, payload["summary"], recipient
            )
        if task_type == "bill_report":
            return self._send_bill_report_sync(
                task_id,
                recipient,
                payload["summary"],
                attachments=_deserialize_attachments(payload.get("attachments")),
            )
        if task_type == "anomaly_alert":
            return self._send_anomaly_alert_sync(task_id, recipient, payload["alerts"])
        if task_type == "generic":
            return self._send_and_record(
                task_id,
                recipient,
                "REPORT",
                payload["subject"],
                payload["html"],
                attachments=_deserialize_attachments(payload.get("attachments")),
            )
        raise ValueError(f"未知任务类型: {task_type}")

    def queue_stats(self) -> dict[str, Any]:
        if self._queue is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "queue_length": self._queue.queue_length(),
            "dlq_length": self._queue.dlq_length(),
            "retry_length": self._queue.retry_length(),
            "pending": self._queue.peek(5),
        }

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self.database.sessions() as db:
            rows = db.scalars(
                select(EmailNotification)
                .where(EmailNotification.task_id == task_id)
                .order_by(EmailNotification.created_at)
            ).all()
            return [
                {
                    "notification_id": r.notification_id,
                    "recipient_email": r.recipient_email,
                    "subject": r.subject,
                    "email_type": r.email_type,
                    "status": r.status,
                    "retry_count": r.retry_count,
                    "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    def _send_and_record(
        self,
        task_id: str,
        recipient: str,
        email_type: str,
        subject: str,
        html: str,
        *,
        attachments: list[str | Attachment] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        notification_id = self._record(
            task_id, recipient, email_type, subject, status="PENDING", now=now
        )
        error = None
        ok = False
        try:
            ok = self.email_service.send_email(recipient, subject, html, attachments)
        except Exception as exc:
            error = str(exc)
            logger.exception("邮件发送异常 task_id=%s", task_id)
        if not ok and error is None:
            error = "SMTP 发送失败（已重试）"
        self._finalize(notification_id, ok, error)
        return {
            "notification_id": notification_id,
            "status": "SENT" if ok else "FAILED",
            "error": error,
            "recipient": recipient,
        }

    def _record(self, task_id, recipient, email_type, subject, *, status, now) -> str:
        with self.database.sessions.begin() as db:
            row = EmailNotification(
                notification_id=new_id("email"),
                task_id=task_id,
                recipient_email=recipient,
                subject=subject,
                email_type=email_type,
                status=status,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.flush()
            return row.notification_id

    def _finalize(self, notification_id: str, ok: bool, error: str | None) -> None:
        with self.database.sessions.begin() as db:
            row = db.scalar(
                select(EmailNotification).where(
                    EmailNotification.notification_id == notification_id
                )
            )
            if not row:
                return
            now = datetime.now(timezone.utc)
            row.status = "SENT" if ok else "FAILED"
            row.error_message = error
            row.sent_at = now if ok else None
            if not ok:
                row.retry_count += 1
            row.updated_at = now


def _serialize_attachments(
    attachments: list[str | Attachment] | None,
) -> list[dict[str, str]]:
    """把附件编成可 JSON 化的载荷：字节附件 base64，路径附件保留路径。

    队列载荷要过 json.dumps，裸 bytes 会直接抛 TypeError，所以字节附件必须在这里
    转成文本；消费端由 _deserialize_attachments 还原。
    """

    import base64

    serialized: list[dict[str, str]] = []
    for attachment in attachments or []:
        if isinstance(attachment, Attachment):
            content = base64.b64encode(attachment.content).decode("ascii")
            serialized.append({"filename": attachment.filename, "content": content})
        else:
            serialized.append({"path": attachment})
    return serialized


def _deserialize_attachments(items) -> list[str | Attachment]:
    """还原 _serialize_attachments 的载荷，两种附件形态都认。"""

    import base64

    result: list[str | Attachment] = []
    for item in items or []:
        if "filename" in item:
            result.append(Attachment(item["filename"], base64.b64decode(item["content"])))
        elif "path" in item:
            result.append(item["path"])
    return result
