# 真机测试环境（os / demo.example.com）

推送到 `test` 分支 → GitHub Actions 打包源码 → SSH 传到服务器 → **在服务器上原生构建 arm64 镜像**
→ 起 compose → 健康检查 → 通过则记下版本，不通过自动回滚到上一个版本。

- 入口：<http://demo.example.com:8088/> —— **浏览器会弹 Basic Auth，账号 `bank` / 密码 `<basic-auth-password>`**
- 版本目录：`/opt/stack/bank-agent/releases/<sha>/`
- 线上版本：<http://demo.example.com:8088/version.txt>（构建时把提交号烧进镜像，浏览器直接看；**不需要口令**）
- 后端状态：<http://demo.example.com:8088/api/health>（**不需要口令**，部署脚本和监控靠它）
- 当前版本：`/opt/stack/bank-agent/state/current_tag`
- 构建日志：GitHub Actions 页面；服务器上的部署日志在 `/tmp/deploy-<sha>.log`（`deploy.sh` 自己 tee 的，失败时 workflow 会自动回捞）

`dev` 分支不触发部署，`test` 分支才是部署开关。

本文只留命令；**完整手册（边界、排障对照表、回滚、改造流程）在 [`docs/deploy-cicd.md`](../docs/deploy-cicd.md)**。

## 为什么不是服务器上 git clone

仓库是私有的，os 上没有任何 GitHub 凭据。所以 workflow 用 `git archive` 打包**已跟踪文件**
（不含 `.git`、`.venv`、`node_modules`），通过 SSH 管道直接解到 `releases/<sha>/`。
好处是服务器不需要凭据、不需要 git、也不会把 Windows 的虚拟环境带进 aarch64 构建上下文。

## 目录与文件

| 文件 | 作用 |
|---|---|
| `deploy/Dockerfile` | 两个 target：`app`（Python 运行时，backend 与 bank 共用）、`web`（nginx + 前端产物） |
| `deploy/Dockerfile.dockerignore` | 只作用于这个 Dockerfile，排除 `.venv` / `node_modules` 等 |
| `deploy/docker-compose.yml` | `db` + `backend` + `bank` + `web` 四个服务，全部用 `${STACK_DIR}` 绝对路径 |
| `deploy/entrypoint-backend.sh` | 等库 → 建 test 库 → alembic → 种子 → checkpoint 表 → uvicorn :8000 |
| `deploy/entrypoint-bank.sh` | 等库 → 建 bank_db → alembic → uvicorn :8001（**不碰业务数据**） |
| `deploy/deploy.sh` | 服务器端总入口：目录/密钥 → 构建 → 起容器 → 健康检查 → 回滚 → 清理 |
| `deploy/nginx.conf` | 8088 静态站 + `/api` 反代到 `backend:8000`（SSE 关缓冲）；**全站 Basic Auth**，只放行 `/api/health` 与 `/version.txt` |
| `.github/workflows/deploy-test.yml` | push `test` 触发的部署流水线 |

服务器上的运行态（**不在仓库里**）：

```
/opt/stack/bank-agent/
├── releases/<sha>/        每次部署解开的源码，保留最近 3 个
├── secrets/
│   ├── database.docker.txt  四行凭据（账户：postgres / 密码 / 外网地址：db / 端口：5432）
│   ├── pg_password.txt      同一个密码，Postgres 容器初始化用
│   ├── llm.env              LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
│   ├── basic_auth           全站口令，一行「账号:密码」（改完重新部署即生效）
│   └── htpasswd             由 basic_auth 派生的 apr1 哈希，nginx 挂载它
├── data/
│   ├── pgdata/            Postgres 数据目录（删掉等于清库）
│   └── tmp/               账单附件、沙箱
└── state/
    ├── current_tag        当前部署的 sha，回滚目标
    └── bank_seeded        银行演示数据初始化过的时间戳（只初始化一次）
```

## 一次性准备

1. **仓库 secret**：Settings → Secrets and variables → Actions → New repository secret

   | 名称 | 值 |
   |---|---|
   | `OS_SSH_KEY` | `~/.ssh/arm_key` 的**全文**（含 `-----BEGIN/END-----`） |

   host 从仓库变量 `OS_HOST` 读取；`user` 与 `STACK_DIR` 固定在 workflow 的 `env` 里（`ubuntu` / `/opt/stack/bank-agent`），不用配。

2. **服务器端口放行**（两处都要，缺一处外面就连不上）：

   ```bash
   # iptables：rules.v4 末尾是 REJECT，必须插到第 1 条再持久化
   sudo iptables -I INPUT 1 -p tcp --dport 8088 -j ACCEPT
   sudo netfilter-persistent save
   ```

   OCI 安全组加一条 ingress：TCP 8088，源 `0.0.0.0/0`（security list
   `ocid1.securitylist.oc1.ap-tokyo-1.aaaaaaaalcqzdd4r2dqilktr2vqfphm7bejn67mrjsrxj2e6o65vldvj3jsq`）。

3. **模型端点**：`secrets/llm.env` 首次部署时自动生成，但模板里每一行都是注释掉的。

   `backend/.env` 现在**不入库**（里面有 SMTP 授权码和服务间令牌），所以镜像里既没有模型配置
   也没有发信配置，两样都只能从 `secrets/` 注入。不注入模型就是 `model_configured=false`：
   服务照常起、页面能开、对话全废；不注入邮件则只是报告发不出去。

   取值照抄本机 `backend/.env.local` 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`；
   备用网关在仓库根 `key.txt`（三行：base_url / api_key / model）。填完重跑一次部署。

4. **发信账号**：`secrets/mail.env`，同样是首次部署自动生成、模板全注释。

   填本机 `backend/.env` 的 `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` /
   `SMTP_FROM_NAME`。授权码不是邮箱密码，163 要在网页端开启 SMTP 后单独生成。

   > 两个模板都刻意**不写空赋值**（`LLM_BASE_URL=`）：空字符串同样会被 pydantic 当成"已设置"，
   > 反而把镜像里 `.env` 的值顶掉。只有真的填了值才注入。

## 日常操作

```bash
cd /opt/stack/bank-agent
CUR=$(cat state/current_tag)
DC="docker compose -f releases/$CUR/deploy/docker-compose.yml"

sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC ps          # 状态
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC logs -f web # 跟日志
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC restart backend
```

> `STACK_DIR` 与 `TAG` 每次都得带上：compose 文件里全是 `${STACK_DIR}/${TAG}` 的绝对路径，
> 不依赖 `--project-directory`，所以换个目录调用也不会串到别的项目上。

**改全站口令**（账号密码只存服务器 `secrets/basic_auth`，不写入仓库）：

```bash
sudo nano /opt/stack/bank-agent/secrets/basic_auth   # 一行「账号:密码」
# 然后随便重跑一次部署（推 test 或手工跑 deploy.sh），htpasswd 会重新派生
```

> 口令只是为了挡住陌生人和脚本，**不是身份认证**：后端所有会话仍然是同一个演示用户
> `user_0001`，拿到口令的人能看到彼此创建的对话。`/api/health` 和 `/version.txt`
> 刻意不设口令（部署脚本与监控要用）。

**重置银行演示数据**（`/api/seed/reset` 会清空整库，只在你真的要重来时跑）。
清库端点要两把钥匙，容器里都有，用 Python 从环境取，别把密钥写进命令行：

```bash
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC exec bank \
  /app/bank_backend/.venv/bin/python -c "
import os, urllib.request
req = urllib.request.Request('http://127.0.0.1:8001/api/seed/reset', method='POST',
    headers={'X-Service-Token': os.environ['SERVICE_TOKEN'],
             'X-Ops-Token': os.environ['OPS_TOKEN']})
urllib.request.urlopen(req, timeout=300)"
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC exec bank \
  /app/bank_backend/.venv/bin/python -m scripts.seed_wealth_demo
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC exec bank \
  /app/bank_backend/.venv/bin/python -m scripts.seed_subscription_demo
```

> 凭证在 `secrets/bank_backend.env`（`SERVICE_TOKEN` / `OPS_TOKEN`，首次部署时生成）。
> `secrets/app_bank.env` 只给 backend 一个 `BANK_SERVICE_TOKEN`——**运维口令刻意不放进
> backend**，这样 app 进程即使被利用也清不了库。想看银行侧日志里为什么全是 401，
> 先核对这两个文件的 `SERVICE_TOKEN` 是否相同。

**回滚到上一个版本**（部署脚本健康检查失败时会自动做这件事，这里是手工版）：

```bash
PREV=<上一个 sha>   # 见 releases/ 目录
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$PREV \
  docker compose -f releases/$PREV/deploy/docker-compose.yml up -d --no-build
echo -n $PREV | sudo tee state/current_tag
```

**彻底清库重建**（Postgres 数据坏了、或者密码和 pgdata 对不上时）：

```bash
sudo -E env STACK_DIR=/opt/stack/bank-agent TAG=$CUR $DC down
sudo rm -rf /opt/stack/bank-agent/data/pgdata /opt/stack/bank-agent/state/bank_seeded
# 下一次部署会重新 initdb + 建库 + 迁移 + 种子
```

## 已知取舍

- **数据库在容器里**，数据在 `data/pgdata`，没有外部备份。删目录 = 清库，这是测试环境，够用。
- **首次部署**要跑建库、迁移、种子、checkpoint 表，健康检查给了 420 秒（`WAIT_SECONDS` 可调）。
- **构建在服务器上做**：4C/24G 的 arm64 原生构建，不用 QEMU，首次冷构建实测约 12 分钟；
  之后只改业务代码时依赖层命中缓存，push 到生效约 25 秒（实测数据见 `docs/deploy-cicd.md` 第 3 节）。
- **演示数据只初始化一次**：`state/bank_seeded` 存在就跳过，重启容器不会重置银行卡余额。
- `backend/.env` 不入库（授权码与令牌都在里面），镜像里没有 `.env`；模型 key 走
  `secrets/llm.env`、发信账号走 `secrets/mail.env`，环境变量优先级高于 `.env`。
- 这台机器还跑着 sing-box（443）和 Immich（2283），本方案只占用 8088，互不影响。
- **Basic Auth 是唯一的一道门**：后端没有登录，口令泄露等于把演示环境和模型额度一起交出去。
  改口令只改 `secrets/basic_auth`，不要动 `nginx.conf` 里的 `auth_basic` 开关。
