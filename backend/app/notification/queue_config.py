"""邮件队列配置。"""

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.core.config import Settings


class QueueConfig(BaseModel):
    """Redis 邮件队列配置。"""

    redis_url: str = "redis://127.0.0.1:6379/1"
    queue_name: str = "email_queue"
    dead_letter_queue: str = "email_dlq"
    retry_queue: str = "email_retry_queue"
    max_retries: int = 3
    retry_delays: list[int] = [60, 300, 900]
    poll_timeout: int = 5

    @classmethod
    def from_settings(cls, settings: "Settings") -> "QueueConfig":
        return cls(
            redis_url=settings.email_queue_redis_url,
            queue_name=settings.email_queue_name,
            dead_letter_queue=settings.email_queue_dlq,
            retry_queue=settings.email_queue_retry_queue,
            max_retries=settings.email_queue_max_retries,
            retry_delays=list(settings.email_queue_retry_delays),
            poll_timeout=settings.email_queue_poll_timeout,
        )

    def retry_delay(self, retry_count: int) -> int | None:
        """返回第 retry_count 次重试（从 0 起）的延迟秒数；超出配置则返回 None。"""
        if retry_count < len(self.retry_delays):
            return self.retry_delays[retry_count]
        return None
