"""邮件发送核心服务。"""

import logging
import smtplib
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional

from app.notification.attachments import Attachment
from app.notification.config import EmailConfig

logger = logging.getLogger(__name__)

# 连接超时，避免 SMTP 服务器不可达时拖垮调用方（需求：单封邮件不超过 3 秒）。
_CONNECT_TIMEOUT = 5


class EmailService:
    """通过 SMTP 发送邮件，支持 SSL/TLS、HTML 正文与附件，失败指数退避重试。"""

    def __init__(self, config: Optional[EmailConfig] = None):
        self.config = config or EmailConfig.from_env()
        if not self.config.validate_config():
            raise ValueError("邮件配置不完整，请检查 SMTP_* 环境变量")

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        attachments: Optional[list[str | Attachment]] = None,
        retry_times: int = 3,
    ) -> bool:
        """发送邮件。

        Returns:
            bool: 发送成功返回 True，否则 False。
        """
        last_error: Exception | None = None
        for attempt in range(retry_times):
            try:
                self._send_once(to_email, subject, html_content, attachments)
                logger.info("邮件发送成功: to=%s subject=%s", to_email, subject)
                return True
            except Exception as exc:  # SMTP 异常种类繁多，统一捕获并按次数重试。
                last_error = exc
                logger.error("邮件发送失败 (尝试 %s/%s): %s", attempt + 1, retry_times, exc)
                if attempt < retry_times - 1:
                    time.sleep(2**attempt)  # 指数退避：1s、2s、4s
        logger.error("邮件最终发送失败: to=%s subject=%s error=%s", to_email, subject, last_error)
        return False

    def _send_once(self, to_email: str, subject: str, html_content: str, attachments) -> None:
        msg = MIMEMultipart()
        msg["From"] = formataddr((self.config.from_name, self.config.smtp_user))
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_content, "html", "utf-8"))
        if attachments:
            for attachment in attachments:
                if isinstance(attachment, Attachment):
                    self._attach_bytes(msg, attachment)
                else:
                    self._attach_file(msg, attachment)

        if self.config.smtp_use_ssl:
            server = smtplib.SMTP_SSL(
                self.config.smtp_host, self.config.smtp_port, timeout=_CONNECT_TIMEOUT
            )
        else:
            server = smtplib.SMTP(
                self.config.smtp_host, self.config.smtp_port, timeout=_CONNECT_TIMEOUT
            )
            server.starttls()
        try:
            server.login(self.config.smtp_user, self.config.smtp_password)
            server.sendmail(self.config.smtp_user, [to_email], msg.as_string())
        finally:
            server.quit()

    def _attach_file(self, msg: MIMEMultipart, file_path: str) -> None:
        path = Path(file_path)
        if not path.exists():
            logger.warning("附件不存在: %s", file_path)
            return
        with open(file_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        msg.attach(part)

    @staticmethod
    def _attach_bytes(msg: MIMEMultipart, attachment: Attachment) -> None:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment.content)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=attachment.filename)
        msg.attach(part)
