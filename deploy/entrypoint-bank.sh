#!/usr/bin/env bash
# bank_backend 容器入口：等库 → 建 bank_db 库 → 迁移 → 起 uvicorn。
#
# 刻意**不做**业务数据种子：bank_backend/README.md 规定 /api/seed/reset 会清空整库、
# seed_wealth_demo / seed_subscription_demo 必须由开发者显式执行。演示数据的一次性
# 初始化放在 deploy/deploy.sh 里，只在首次部署时跑一次。
set -euo pipefail

PY=/app/bank_backend/.venv/bin/python
cd /app/bank_backend

echo "[bank] 等待 PostgreSQL 就绪"
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
  echo "[bank] PostgreSQL 120 秒内仍不可达，放弃启动" >&2
  exit 1
fi
echo "[bank] PostgreSQL 就绪"

echo "[bank] 确保 bank_db 库存在"
"$PY" scripts/init_database.py

echo "[bank] alembic upgrade head"
"$PY" -m alembic upgrade head

echo "[bank] 启动 uvicorn :8001"
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --loop asyncio
