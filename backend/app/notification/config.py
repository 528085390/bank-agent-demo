"""邮件发送配置。"""

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.core.config import Settings


class EmailConfig(BaseModel):
    """SMTP 邮件服务配置。密码只从环境变量注入，绝不进入日志或审计。"""

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_use_ssl: bool = True
    from_name: str = "银行AI智能体"

    @classmethod
    def from_env(cls) -> "EmailConfig":
        """从环境变量加载配置（供独立测试脚本等无应用上下文场景使用）。"""
        return cls(
            smtp_host=os.getenv("SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(os.getenv("SMTP_PORT", "465")),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_use_ssl=os.getenv("SMTP_USE_SSL", "true").lower() == "true",
            from_name=os.getenv("SMTP_FROM_NAME", "银行AI智能体"),
        )

    @classmethod
    def from_settings(cls, settings: "Settings") -> "EmailConfig":
        """从应用 Settings 构建，复用统一 .env 加载。"""
        return cls(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_user=settings.smtp_user,
            smtp_password=settings.smtp_password,
            smtp_use_ssl=settings.smtp_use_ssl,
            from_name=settings.smtp_from_name,
        )

    def validate_config(self) -> bool:
        """验证配置是否完整可发送。"""
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)
