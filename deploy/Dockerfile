# syntax=docker/dockerfile:1
#
# 两个构建目标，构建上下文是仓库根：
#   app —— Python 运行镜像，backend 与 bank_backend 各带一个 .venv
#   web —— nginx 镜像，托管前端构建产物并把 /api 反代给 backend
#
# 构建发生在 os（Oracle 东京，aarch64）上，由 deploy/deploy.sh 触发。

# ---------------------------------------------------------------- 前端构建
FROM node:22-slim AS web-build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- Python 运行时
FROM python:3.13-slim AS app

# 3.13 与本机 .venv 一致（本机 Python 3.13.5），uv.lock 就是按它解析出来的。
# uv 版本也与本机一致（uv 0.11.7），保证 --frozen 能原样复现。
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只装第三方依赖：改业务代码时这一层仍然命中缓存。
COPY backend/pyproject.toml backend/uv.lock backend/alembic.ini ./backend/
COPY bank_backend/pyproject.toml bank_backend/uv.lock bank_backend/alembic.ini ./bank_backend/
RUN cd backend && uv sync --frozen --no-dev --no-install-project \
 && cd ../bank_backend && uv sync --frozen --no-dev --no-install-project

# 再装项目本身（uv 默认 editable 装进各自 .venv）。
COPY backend/ ./backend/
COPY bank_backend/ ./bank_backend/
# alembic 的 0001_billing_baseline 会读 docs/contracts/sql/001_init.sql（从 migrations/versions
# 往上 5 层 = 仓库根），所以这一小块文档必须进镜像，否则迁移直接 FileNotFoundError。
COPY docs/contracts/ ./docs/contracts/
RUN cd backend && uv sync --frozen --no-dev \
 && cd ../bank_backend && uv sync --frozen --no-dev

COPY deploy/entrypoint-backend.sh deploy/entrypoint-bank.sh /app/
# CRLF 会让 bash 把 "\r" 当成命令的一部分；从 Windows 提交的文件在这里统一矫正。
RUN sed -i 's/\r$//' /app/entrypoint-backend.sh /app/entrypoint-bank.sh \
 && chmod +x /app/entrypoint-backend.sh /app/entrypoint-bank.sh \
 && mkdir -p /app/.tmp

EXPOSE 8000 8001
CMD ["bash", "/app/entrypoint-backend.sh"]

# ---------------------------------------------------------------- 静态站 + 反代
FROM nginx:stable-alpine AS web
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web-build /src/dist /usr/share/nginx/html

# 把这次的提交号烧进站点：浏览器打开 /version.txt 就能核对线上是哪一版，
# 不用登服务器看 state/current_tag。TAG 由 deploy.sh 经 compose 的 build args 传进来。
ARG GIT_SHA=unknown
RUN printf '%s\n' "$GIT_SHA" > /usr/share/nginx/html/version.txt

EXPOSE 8088
