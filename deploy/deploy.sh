#!/usr/bin/env bash
# 真机测试环境部署脚本（在 os 上跑，不在本机跑）。
#
# CI 的用法（.github/workflows/deploy-test.yml）：
#   STACK_DIR=/opt/stack/bank-agent TAG=<sha> bash /opt/stack/bank-agent/releases/<sha>/deploy/deploy.sh
#
# 人工的用法一样，只是 TAG 由脚本自己从 release 目录名推出来：
#   bash /opt/stack/bank-agent/releases/<sha>/deploy/deploy.sh
#
# 做五件事：准备目录与密钥文件 → 构建 arm64 镜像 → 起 compose → 等健康检查
# → 通过则记下当前版本，不通过就切回上一个版本。
#
# 动手之前先抢 /tmp/bank-agent-deploy.lock：两个人几乎同时推 test 时排队，不并发
# （并发的 build/up/回滚会互相踩容器和镜像 tag）。等锁超过 LOCK_WAIT 秒才放弃。
set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/stack/bank-agent}"
RELEASE_DIR="${RELEASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TAG="${TAG:-$(basename "$RELEASE_DIR")}"
PORT="${PORT:-8088}"
WAIT_SECONDS="${WAIT_SECONDS:-420}"
KEEP_RELEASES="${KEEP_RELEASES:-3}"

STATE_DIR="$STACK_DIR/state"
SECRETS_DIR="$STACK_DIR/secrets"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
BANK_PY=/app/bank_backend/.venv/bin/python
COMPOSE=(docker compose -f "${RELEASE_DIR}/deploy/docker-compose.yml")

export STACK_DIR TAG PORT

# 自己的输出同时落一份到 /tmp/deploy-<tag>.log：CI 失败时 workflow 会把这份日志
# 回捞到 Actions 页面，事后复盘也靠它（不用再翻 ssh 会话）。
LOG_FILE="${LOG_FILE:-/tmp/deploy-${TAG}.log}"
exec > >(tee "$LOG_FILE") 2>&1

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m[OK]\033[0m %s\n' "$*"; }
note() { printf '  \033[0;90m[..]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; }

# ---- 部署锁 ---------------------------------------------------------------
# 同一时刻只允许一个部署在跑。GitHub Actions 那边已经把并发推送变成排队
# （cancel-in-progress: false），这里再兜一层：workflow_dispatch 重跑、手工重跑、
# 本地调试都绕不过去。后到的进程等在锁上，不会和前一个抢容器和镜像 tag。
LOCK_FILE="${LOCK_FILE:-/tmp/bank-agent-deploy.lock}"
LOCK_WAIT="${LOCK_WAIT:-900}"
exec 9>"$LOCK_FILE"
LOCK_START=$SECONDS
if ! flock -w "$LOCK_WAIT" 9; then
  fail "等了 ${LOCK_WAIT}s 还没拿到部署锁（$LOCK_FILE），另一个部署仍在进行"
  note "看它跑到哪一步：ls -lt /tmp/deploy-*.log | head -3，再 tail 最新的那份"
  exit 1
fi
WAITED=$((SECONDS - LOCK_START))
if [ "$WAITED" -ge 5 ]; then
  ok "已获得部署锁（等了 ${WAITED}s，前一个部署刚跑完）"
else
  ok "已获得部署锁（$LOCK_FILE）"
fi

# "上一个版本"必须在拿到锁之后再读：排队期间前一个部署可能刚改过 current_tag。
PREV_TAG="$(cat "$STATE_DIR/current_tag" 2>/dev/null || true)"

deploy_failed() {
  fail "$1"
  echo
  note "当前容器状态"
  "${COMPOSE[@]}" ps || true
  echo
  note "最近日志"
  "${COMPOSE[@]}" logs --tail=120 backend bank web 2>&1 || true

  if [ -z "$PREV_TAG" ]; then
    warn "这是首次部署，没有可回滚的历史版本"
    exit 1
  fi
  if ! docker image inspect "bank-agent-app:${PREV_TAG}" >/dev/null 2>&1; then
    warn "上一个版本 bank-agent-app:${PREV_TAG} 的镜像已经不在了，无法回滚"
    exit 1
  fi
  echo
  warn "回滚到上一个版本 ${PREV_TAG}"
  if TAG="$PREV_TAG" "${COMPOSE[@]}" up -d --remove-orphans --no-build; then
    ok "已回滚到 ${PREV_TAG}（${HEALTH_URL} 需要重新人工确认）"
  else
    fail "回滚也失败了，需要人工介入"
  fi
  exit 1
}

# ---------------------------------------------------------------- 1 目录
step "准备目录 $STACK_DIR"
mkdir -p "$SECRETS_DIR" "$STATE_DIR" "$STACK_DIR/data/pgdata" "$STACK_DIR/data/tmp" "$STACK_DIR/releases"
ok "releases / secrets / state / data 就位"

# ---------------------------------------------------------------- 2 密钥文件
step "准备密钥文件"
DB_CRED_FILE="$SECRETS_DIR/database.docker.txt"
PG_PASS_FILE="$SECRETS_DIR/pg_password.txt"

if [ ! -f "$DB_CRED_FILE" ]; then
  # 密码只生成一次：四行凭据文件和 Postgres 的 POSTGRES_PASSWORD_FILE 必须一致，
  # 否则 pgdata 里已初始化的密码和凭据文件对不上，连接会一直被拒。
  PG_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  # 四行必须用全角冒号「：」，read_credentials 只按全角冒号切分。
  printf '账户：postgres\n密码：%s\n外网地址：db\n端口：5432\n' "$PG_PASSWORD" > "$DB_CRED_FILE"
  printf '%s' "$PG_PASSWORD" > "$PG_PASS_FILE"
  chmod 600 "$DB_CRED_FILE" "$PG_PASS_FILE"
  ok "已生成 $DB_CRED_FILE（账号 postgres，密码随机 48 位）"
else
  ok "沿用已有的 $DB_CRED_FILE"
  if [ ! -f "$PG_PASS_FILE" ]; then
    fail "$PG_PASS_FILE 丢了：数据库密码只存在这两个文件里，缺一个就连不上 pgdata。"
    fail "要么从 $DB_CRED_FILE 的第二行把密码抄进 $PG_PASS_FILE，要么删掉 data/pgdata 重新初始化。"
    exit 1
  fi
fi

LLM_ENV="$SECRETS_DIR/llm.env"
if [ ! -f "$LLM_ENV" ]; then
  # 全部注释掉：只有真的填了值才注入。空赋值同样会被 pydantic 当成"已设置的空字符串"，
  # 反而会把镜像里 backend/.env 的值顶掉，所以模板里不写空赋值。
  cat > "$LLM_ENV" <<'LLM'
# 模型端点。backend/.env 不再入库，镜像里没有任何模型配置，
# 所以服务器上的模型 key 只能从这里注入。环境变量优先级高于 .env。
#
# 取值照抄本机 backend/.env.local 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL；
# 备用网关见仓库根 key.txt（三行：base_url / api_key / model）。
#
# 填完重跑一次部署，或者 docker compose up -d 让 backend 重建。
# 不填的话：服务照常起、页面能开，但 /api/health 的 model_configured=false，对话全废。
#
# LLM_BASE_URL=https://example.com/v1
# LLM_API_KEY=sk-xxxx
# LLM_MODEL=deepseek-v4-flash
LLM
  chmod 600 "$LLM_ENV"
  warn "已生成 $LLM_ENV，但里面一行都没启用：填好 LLM_* 再跑一次部署，否则页面能开、对话全废"
fi

# 发信账号。backend/.env 不再入库之后，镜像里没有 SMTP 配置，服务器上的授权码只能从
# 这里注入。不填只是发不出报告，不影响登录与对话。
MAIL_ENV="$SECRETS_DIR/mail.env"
if [ ! -f "$MAIL_ENV" ]; then
  cat > "$MAIL_ENV" <<'MAIL'
# 邮件。取值照抄本机 backend/.env 的 SMTP_* 五行。
# 授权码不是邮箱登录密码；163 邮箱要在网页端开启 SMTP 后单独生成。
#
# SMTP_HOST=smtp.163.com
# SMTP_PORT=465
# SMTP_USER=xxx@163.com
# SMTP_PASSWORD=xxxx
# SMTP_FROM_NAME=银行AI智能体
MAIL
  chmod 600 "$MAIL_ENV"
  warn "已生成 $MAIL_ENV，但里面一行都没启用：不填则报告发不出去，其余功能正常"
fi

# 银行访问控制的两把钥匙。只在第一次部署时生成，之后原地复用：轮换会让「已经跑着的
# 容器」和「刚写好的文件」短暂不一致，而它们都不需要轮换。
#
# 分成两个文件而不是一个，是为了让 backend **拿不到** OPS_TOKEN：它能读 bank.env 的话，
# 「清库只能由运维做」这条就只剩口头约定了。
BANK_ENV_FILE="$SECRETS_DIR/bank_backend.env"
APP_BANK_ENV_FILE="$SECRETS_DIR/app_bank.env"
if [ ! -f "$BANK_ENV_FILE" ] || [ ! -f "$APP_BANK_ENV_FILE" ]; then
  SERVICE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  OPS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  printf 'SERVICE_TOKEN=%s\nOPS_TOKEN=%s\n' "$SERVICE_TOKEN" "$OPS_TOKEN" > "$BANK_ENV_FILE"
  printf 'BANK_SERVICE_TOKEN=%s\n' "$SERVICE_TOKEN" > "$APP_BANK_ENV_FILE"
  chmod 600 "$BANK_ENV_FILE" "$APP_BANK_ENV_FILE"
  ok "已生成服务凭证与运维口令（$BANK_ENV_FILE / $APP_BANK_ENV_FILE）"
else
  SERVICE_TOKEN="$(sed -n 's/^SERVICE_TOKEN=//p' "$BANK_ENV_FILE")"
  OPS_TOKEN="$(sed -n 's/^OPS_TOKEN=//p' "$BANK_ENV_FILE")"
  if [ -z "$SERVICE_TOKEN" ] || [ -z "$OPS_TOKEN" ]; then
    fail "$BANK_ENV_FILE 缺少 SERVICE_TOKEN 或 OPS_TOKEN：bank 会拒绝所有调用，页面一片失败"
    exit 1
  fi
  ok "沿用已有的服务凭证与运维口令"
fi

# Basic Auth 全站口令。明文一行「账号:密码」放在 secrets/basic_auth（第一次自动生成），
# 每次部署由它派生 nginx 认的 secrets/htpasswd（apr1 哈希）。改口令＝改 basic_auth 再部署。
BASIC_AUTH_FILE="$SECRETS_DIR/basic_auth"
HTPASSWD_FILE="$SECRETS_DIR/htpasswd"
if [ ! -f "$BASIC_AUTH_FILE" ]; then
  BASIC_AUTH_INIT_PASS="$(openssl rand -hex 12)"
  printf 'bank:%s\n' "$BASIC_AUTH_INIT_PASS" > "$BASIC_AUTH_FILE"
  chmod 600 "$BASIC_AUTH_FILE"
  ok "已生成 $BASIC_AUTH_FILE（账号 bank，随机初始口令见文件）"
fi
BASIC_AUTH_USER="$(cut -d: -f1 "$BASIC_AUTH_FILE")"
BASIC_AUTH_PASS="$(cut -d: -f2- "$BASIC_AUTH_FILE")"
if [ -z "$BASIC_AUTH_USER" ] || [ -z "$BASIC_AUTH_PASS" ]; then
  fail "$BASIC_AUTH_FILE 格式不对：要写成一行「账号:密码」，现在读到的账号是「$BASIC_AUTH_USER」"
  exit 1
fi
# apr1 是 nginx auth_basic 认的格式；本机（Ubuntu）自带 openssl。
printf '%s:%s\n' "$BASIC_AUTH_USER" "$(openssl passwd -apr1 "$BASIC_AUTH_PASS")" > "$HTPASSWD_FILE"
# 644 不是疏忽：nginx worker 以 nginx 用户读这个文件，600 会让每个请求都 500。
# 文件里只有加盐哈希，不含明文。
chmod 644 "$HTPASSWD_FILE"
ok "已生成 $HTPASSWD_FILE（账号 $BASIC_AUTH_USER）"

# ---------------------------------------------------------------- 3 构建
step "构建镜像 bank-agent-app:${TAG} / bank-agent-web:${TAG}"
note "本机就是 aarch64，走原生构建，不碰 buildx 和 QEMU"
"${COMPOSE[@]}" build backend web || deploy_failed "镜像构建失败"
ok "镜像构建完成"

# ---------------------------------------------------------------- 4 起服务
step "启动容器"
"${COMPOSE[@]}" up -d --remove-orphans --no-build || deploy_failed "容器启动失败"
"${COMPOSE[@]}" ps

# ---------------------------------------------------------------- 5 健康检查
step "等待 ${HEALTH_URL} 返回 200（最多 ${WAIT_SECONDS} 秒）"
note "首次启动要建库 + 迁移 + 种子 + checkpoint 表，慢一点是正常的"
deadline=$((SECONDS + WAIT_SECONDS))
healthy=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl -fsS --max-time 5 "$HEALTH_URL" -o /tmp/bank-agent-health.json 2>/dev/null; then
    healthy=1
    break
  fi
  sleep 5
done
[ "$healthy" = "1" ] || deploy_failed "健康检查超时：${HEALTH_URL} 在 ${WAIT_SECONDS} 秒内没有返回 200"

ok "backend 健康"
cat /tmp/bank-agent-health.json 2>/dev/null || true
echo
if grep -q '"model_configured"[[:space:]]*:[[:space:]]*true' /tmp/bank-agent-health.json 2>/dev/null; then
  ok "模型端点已配置"
else
  warn "model_configured=false：$LLM_ENV 里 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 还没填"
fi

if curl -fsS --max-time 10 -u "$BASIC_AUTH_USER:$BASIC_AUTH_PASS" "http://127.0.0.1:${PORT}/" -o /dev/null; then
  ok "前端页面可访问（带 Basic Auth）"
else
  deploy_failed "前端页面打不开：http://127.0.0.1:${PORT}/"
fi

# 反向确认口令真的生效了：不带凭据必须 401。nginx.conf 里 auth_basic 被删掉时
# 上面那条照样能过，这里才会红。
page_code="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
if [ "$page_code" = "401" ]; then
  ok "未带凭据访问被拦下（401）"
else
  deploy_failed "Basic Auth 没生效：不带凭据访问首页返回 $page_code，应该是 401"
fi

# ---------------------------------------------------------------- 6 银行演示数据（仅首次）
if [ ! -f "$STATE_DIR/bank_seeded" ]; then
  step "首次部署：初始化银行演示数据"
  note "bank_backend 规定 /api/seed/reset 会清空整库，所以只在首次部署跑一次"
  note "之后想重置，手工执行 deploy/README.md 里的「重置银行演示数据」"

  # 探活不能用 /api/health：bank 的 health 要求模拟状态已初始化（种子跑完才有），
  # 而种子正是下一步要做的事，用它探活会永远等不到。这里只确认 uvicorn 已在 8001 监听。
  bank_deadline=$((SECONDS + 300))
  bank_up=0
  while [ "$SECONDS" -lt "$bank_deadline" ]; do
    if "${COMPOSE[@]}" exec -T bank "$BANK_PY" -c "import socket;socket.create_connection(('127.0.0.1',8001),3).close()" >/dev/null 2>&1; then
      bank_up=1
      break
    fi
    sleep 5
  done
  [ "$bank_up" = "1" ] || deploy_failed "bank 服务 300 秒内没有就绪"

  # 清库端点要两把钥匙：服务凭证 + 运维口令。容器里有这两个变量（env_file 注入），
  # 所以直接取 os.environ 而不是把密钥拼进命令行——命令行会出现在进程列表里。
  "${COMPOSE[@]}" exec -T bank "$BANK_PY" -c "
import os, urllib.request
req = urllib.request.Request(
    'http://127.0.0.1:8001/api/seed/reset',
    method='POST',
    headers={
        'X-Service-Token': os.environ['SERVICE_TOKEN'],
        'X-Ops-Token': os.environ['OPS_TOKEN'],
    },
)
urllib.request.urlopen(req, timeout=300)
" || deploy_failed "银行基础种子（POST /api/seed/reset）失败"
  ok "基础种子 + 模拟时钟"

  # exec 起的新进程 cwd 是镜像 WORKDIR(/app)，不是 entrypoint 里 cd 过去的 bank_backend，
  # 不显式 cd 就找不到 scripts 包（ModuleNotFoundError: No module named 'scripts'）。
  "${COMPOSE[@]}" exec -T bank sh -c "cd /app/bank_backend && $BANK_PY -m scripts.seed_wealth_demo" || deploy_failed "seed_wealth_demo 失败"
  ok "理财演示数据"

  "${COMPOSE[@]}" exec -T bank sh -c "cd /app/bank_backend && $BANK_PY -m scripts.seed_subscription_demo" || deploy_failed "seed_subscription_demo 失败"
  ok "订阅演示数据（u_wealth）"

  # 注册要领取一个独立客户，池子必须先在银行侧存在。这一步幂等，可以重复跑：
  # 银行库被 reset 之后重跑它就能把所有已领取的客户补回来。
  "${COMPOSE[@]}" exec -T bank sh -c "cd /app/bank_backend && $BANK_PY -m scripts.seed_customer_pool --count 20" || deploy_failed "seed_customer_pool 失败"
  ok "注册客户池（u_pool_01..20）"

  # 种子跑完才轮到 bank 的 /api/health：它除了查库，还校验 available_minor 与余额一致。
  "${COMPOSE[@]}" exec -T bank "$BANK_PY" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8001/api/health',timeout=15)" >/dev/null 2>&1 \
    || deploy_failed "种子跑完了，但 bank /api/health 仍不是 200"
  ok "bank /api/health 通过（种子版本与账户余额一致）"

  date -Is > "$STATE_DIR/bank_seeded"
else
  note "银行演示数据已初始化过（$(cat "$STATE_DIR/bank_seeded" 2>/dev/null)），跳过"
fi

# ---------------------------------------------------------------- 7 记账与清理
step "记录当前版本"
printf '%s' "$TAG" > "$STATE_DIR/current_tag"
ok "state/current_tag = $TAG"

step "清理旧版本（保留最近 ${KEEP_RELEASES} 个 release 目录 + 本次/上次镜像）"
ls -1dt "$STACK_DIR"/releases/*/ 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))" | while read -r old; do
  case "$old" in
    "$STACK_DIR"/releases/*) rm -rf "$old"; note "删除 ${old%/}" ;;
    *) warn "跳过可疑路径 $old" ;;
  esac
done
docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -E '^bank-agent-(app|web):' \
  | grep -v -E ":(${TAG}|${PREV_TAG})\$" \
  | xargs -r docker rmi >/dev/null 2>&1 || true

echo
ok "部署完成：本机 http://127.0.0.1:${PORT}/，对外入口见《部署说明》"
