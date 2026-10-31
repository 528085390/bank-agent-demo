#!/usr/bin/env bash
# backend 容器入口：等库 → 建 test 库 → 迁移 → 种子 → checkpoint 表 → 起 uvicorn。
#
# 步骤与仓库根的 start.ps1「准备数据库（迁移 + 种子）」完全一致，顺序也一致，
# 所以真机测试环境看到的表结构和演示数据跟本机是同一套。
# 每一步都幂等：容器重启、镜像重发都不会把库搞坏。
set -euo pipefail

PY=/app/backend/.venv/bin/python
cd /app/backend

echo "[backend] 等待 PostgreSQL 就绪"
ready=0
for attempt in $(seq 1 60); do
  if "$PY" - <<'PYWAIT'
import psycopg

from app.core.database_file import sqlalchemy_url

url = sqlalchemy_url("postgres").replace("postgresql+psycopg://", "postgresql://")
psycopg.connect(url, connect_timeout=3).close()
PYWAIT
  then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" != "1" ]; then
  echo "[backend] PostgreSQL 120 秒内仍不可达，放弃启动" >&2
  exit 1
fi
echo "[backend] PostgreSQL 就绪"

echo "[backend] 确保 test 库存在"
"$PY" scripts/init_test_database.py

echo "[backend] alembic upgrade head"
"$PY" -m alembic upgrade head

echo "[backend] 校对演示种子"
"$PY" -m app.storage.seed

echo "[backend] 初始化 checkpoint 表"
"$PY" scripts/init_checkpointer.py

echo "[backend] 启动 uvicorn :8000"
# serve.py 把 host 写死成 127.0.0.1，容器里必须监听 0.0.0.0，所以这里直接调 uvicorn。
# --loop asyncio 是刻意指定的：镜像里装了 uvicorn[standard]，默认会挑 uvloop，
# 而 psycopg 的异步连接在 selector 事件循环上才是本机验证过的组合。
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --loop asyncio
