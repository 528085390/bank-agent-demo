"""基于 Redis 的邮件队列：生产者入队、Worker 消费、延迟重试与死信队列。

Redis 不可达或未安装 redis 包时，EmailQueue 构造会抛异常，由 NotificationService
捕获并降级为同步发送。
"""

import json
import logging
import time
from typing import Any

from app.notification.queue_config import QueueConfig

logger = logging.getLogger(__name__)


def _redis_client(config: QueueConfig):
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("缺少 redis 依赖，请先 uv add redis") from exc
    return redis.from_url(
        config.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


class EmailQueue:
    """邮件队列生产者。构造时立即验证连接，失败即抛异常交由调用方降级。"""

    def __init__(self, config: QueueConfig | None = None):
        self.config = config or QueueConfig()
        self._redis = _redis_client(self.config)
        self._redis.ping()

    def enqueue(
        self,
        task_type: str,
        task_id: str,
        recipient: str,
        payload: dict[str, Any],
        *,
        priority: int = 0,
    ) -> str:
        """把邮件任务入队，返回 job_id。priority>0 时插入队首（高优先级）。"""
        job_id = f"{task_type}:{task_id}:{int(time.time() * 1000)}"
        job = {
            "job_id": job_id,
            "task_type": task_type,
            "task_id": task_id,
            "recipient": recipient,
            "payload": payload,
            "enqueued_at": time.time(),
            "retry_count": 0,
            "priority": priority,
        }
        body = json.dumps(job, ensure_ascii=False)
        if priority > 0:
            self._redis.lpush(self.config.queue_name, body)
        else:
            self._redis.rpush(self.config.queue_name, body)
        logger.info("邮件任务已入队 job_id=%s type=%s recipient=%s", job_id, task_type, recipient)
        return job_id

    def queue_length(self) -> int:
        return int(self._redis.llen(self.config.queue_name))

    def dlq_length(self) -> int:
        return int(self._redis.llen(self.config.dead_letter_queue))

    def retry_length(self) -> int:
        return int(self._redis.zcard(self.config.retry_queue))

    def peek(self, count: int = 10) -> list[dict]:
        items = self._redis.lrange(self.config.queue_name, 0, count - 1)
        return [json.loads(item) for item in items]


class EmailWorker:
    """邮件队列消费者：BRPOP 消费、失败延迟重试、超限进入死信队列。"""

    def __init__(self, send_job, config: QueueConfig | None = None):
        self.send_job = send_job  # callable(job) -> dict | None
        self.config = config or QueueConfig()
        self._redis = _redis_client(self.config)
        self._running = False

    def start(self) -> None:
        self._running = True
        logger.info("邮件 Worker 已启动 queue=%s", self.config.queue_name)
        while self._running:
            try:
                self._drain_retry_queue()
                self._process_one()
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("Worker 循环异常")
                time.sleep(1)

    def stop(self) -> None:
        self._running = False

    def _process_one(self) -> None:
        result = self._redis.brpop(self.config.queue_name, timeout=self.config.poll_timeout)
        if result is None:
            return  # 超时，继续等待
        _, job_json = result
        job = json.loads(job_json)
        try:
            outcome = self.send_job(job)
            if outcome and outcome.get("status") == "FAILED":
                raise RuntimeError(outcome.get("error") or "邮件发送失败")
            logger.info("邮件任务完成 job_id=%s", job["job_id"])
        except Exception as exc:
            logger.error("邮件任务失败 job_id=%s error=%s", job["job_id"], exc)
            self._handle_failure(job, str(exc))

    def _drain_retry_queue(self) -> None:
        now = time.time()
        for item_json in self._redis.zrangebyscore(self.config.retry_queue, 0, now):
            job = json.loads(item_json)
            self._redis.lpush(self.config.queue_name, item_json)
            self._redis.zrem(self.config.retry_queue, item_json)
            logger.info("重试任务重新入队 job_id=%s", job["job_id"])

    def _handle_failure(self, job: dict, error: str) -> None:
        retry_count = job.get("retry_count", 0)
        delay = self.config.retry_delay(retry_count)
        if delay is not None:
            job["retry_count"] = retry_count + 1
            job["last_error"] = error
            job["retry_at"] = time.time() + delay
            self._redis.zadd(
                self.config.retry_queue,
                {json.dumps(job, ensure_ascii=False): time.time() + delay},
            )
            logger.info(
                "任务将在 %s 秒后重试 job_id=%s retry=%s/%s",
                delay,
                job["job_id"],
                retry_count + 1,
                self.config.max_retries,
            )
        else:
            job["failed_at"] = time.time()
            job["final_error"] = error
            self._redis.lpush(self.config.dead_letter_queue, json.dumps(job, ensure_ascii=False))
            logger.error("任务失败超限进入死信队列 job_id=%s", job["job_id"])
