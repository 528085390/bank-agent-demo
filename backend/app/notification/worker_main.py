"""邮件队列 Worker 独立启动脚本。

用法：
    python -m app.notification.worker_main

前提：EMAIL_QUEUE_ENABLED=true 且 Redis 可达。
"""

import logging
import signal
import sys

from app.core.config import get_settings
from app.notification.queue import EmailWorker
from app.notification.queue_config import QueueConfig
from app.notification.service import NotificationService
from app.storage.database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    settings = get_settings()
    if not settings.email_queue_enabled:
        logger.error("邮件队列未启用（EMAIL_QUEUE_ENABLED=false），Worker 无任务可处理")
        return 1

    database = Database(settings.database_url)
    notification = NotificationService(database, settings)
    if not notification.queue_enabled:
        logger.error("Redis 不可达，Worker 无法启动")
        database.dispose()
        return 1

    worker = EmailWorker(notification.send_job, QueueConfig.from_settings(settings))

    def shutdown(signum, frame):
        del signum, frame
        logger.info("收到停止信号，正在关闭 Worker")
        worker.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("邮件 Worker 启动")
    try:
        worker.start()
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
