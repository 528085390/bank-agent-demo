# 真机测试环境 CI/CD 使用手册

> **文档标识**：`docs/deploy-cicd.md`
> **适用读者**：要把代码发到真机测试环境（os / demo.example.com）的开发者
> **权威实现**：`deploy/` 下的全部脚本 + `.github/workflows/deploy-test.yml`
> **速查版**：`deploy/README.md`（只列命令，不解释原理）；本文讲"为什么"和"出问题怎么办"
> **最后更新**：2026-10-26

---

## 1. 三十秒上手

| 我要做的事 | 命令 |
|---|---|
| 发一版到真机 | `git branch -f test dev && git push --force-with-lease origin test` |
| 打开线上页面 | <http://demo.example.com:8088/>（Basic Auth：`bank` / `<basic-auth-password>`） |
| 看线上是哪一版 | `curl http://demo.example.com:8088/version.txt`（不需要口令） |
| 看这次部署干了什么 | GitHub → Actions → `deploy-test` → 最近一次运行 |
| 失败了 | 先看 Actions 页面上"失败时回捞部署日志"那一步（服务器日志会自动贴回来） |

没有别的前置步骤。推完等半分钟到一分钟（首次冷构建要十几分钟），`/version.txt` 变成你推的 sha 就是生效了。

---

## 2. 边界：什么会触发部署，什么不会

| 动作 | 会不会部署 | 说明 |
|---|---|---|
| push 到 `test` | **会** | 唯一的自动部署入口 |
| push 到 `dev` | 不会 | `dev` 是正式分支，改了不部署 |
| push 到其他分支 | 不会 | workflow 只监听 `test` |
| 开 PR / 合并 PR | 不会 | 没有配 `pull_request` 触发器 |
| Actions 页面手动 Run workflow（选 `test` 分支） | **会** | `workflow_dispatch`，重跑用 |
| 连续推两次 `test` | 都跑，但排队 | 前一次不打断、跑完为止；更早的排队项被丢弃，只留最新一次 |

几个必须知道的设定：

- **`test` 分支是部署开关，不是开发分支。** 它的内容永远等于"线上那一版"，所以从 `dev` 同步时用 `git branch -f` 强推，不要在上面直接写代码（会被下次强推覆盖）。
- **这套环境是测试环境，不是生产。** 数据库跑在容器里、没有外部备份、随时可以清空重建（见第 8 节）。
- **仓库是私有的，服务器上没有 GitHub 凭据。** 所以不是"服务器上 git clone + pull"，而是 runner 打包 → SSH 管道 → 服务器上解包构建（见第 3 节）。这也是为什么推完到生效之间有十几秒的传输时间。
- **页面有 Basic Auth，账号 `bank`，密码只存服务器的 `secrets/basic_auth`，不写入仓库。** 应用层已经改成真实账号登录（`/api/auth/login`，见 `docs/superpowers/specs/2026-10-27-p1-auth-implementation-plan.md`），`POST /api/session` 默认关闭（`ALLOW_DEMO_LOGIN=false`）；Basic Auth 暂时留作外层门禁，挡住登录页本身和静态资源。`/api/health` 与 `/version.txt` 刻意不设口令——部署脚本和监控要用。改口令见 9.7。
- **两个人同时推不会互相踩，但会排队。** 两层：workflow 层 `cancel-in-progress: false`（正在跑的不被打断，排队的只留最新一次）；服务器层 `deploy.sh` 开头抢 `/tmp/bank-agent-deploy.lock`（`flock`，最多等 900 秒）。所以 B 推的时候 A 会正常跑完、B 才开始；A 卡住超过 15 分钟，B 会以"没拿到部署锁"失败退出。

---

## 3. 一次 push 的全景

```
你的机器                GitHub Actions（ubuntu-latest）            os / demo.example.com
────────                ───────────────────────────────            ────────────────────
git push test  ───────▶ ① 检查 OS_SSH_KEY 非空
                        ② checkout（fetch-depth 1）
                        ③ git archive HEAD ──tar.gz──ssh──▶  解到 releases/<sha>/
                        ④ ssh 远程执行 deploy.sh ─────────▶  ⑤ 构建 arm64 镜像
                                                             ⑥ compose up
                                                             ⑦ 健康检查 / 首次种子
                                                             ⑧ 记 current_tag + 清理
                        ◀──────── 退出码 0 = 成功 ──────────
                        ⑨ 失败则回捞 /tmp/deploy-<sha>.log
```

实测耗时（aarch64 原生构建，4C/24G）：

| 阶段 | 只改业务代码 | 改了 lockfile | 首次冷构建 |
|---|---|---|---|
| push → 源码落到服务器 | 约 12 秒 | 约 12 秒 | 约 12 秒 |
| 构建镜像（命中缓存） | 几秒 | 1–3 分钟 | 约 12 分钟 |
| compose up → 新容器起来 | 约 8 秒 | 约 8 秒 | 约 30 秒（要建库+迁移） |
| 健康检查 + 种子 + 记账 | 约 15 秒 | 约 15 秒 | 1–3 分钟（首次种子） |
| **push 到线上生效** | **约 25 秒** | 2–4 分钟 | 12–15 分钟 |

> 结论：改业务代码基本"推完就上"；改 `uv.lock` / `frontend/package-lock.json` 会重跑依赖安装，要有耐心。

---

## 4. 日常发版

### 4.1 从 dev 发一版（99% 的情况）

```bash
# 1. 确认要发的提交已经在 dev 上、且工作区没有别的会话的改动
git checkout dev
git status
git log --oneline -3

# 2. 让 test 指向 dev 的当前提交，并强推
git branch -f test dev
git push --force-with-lease origin test

# 3. 等 Actions（浏览器开着最省事）
#    https://github.com/528085390/bank-agent/actions/workflows/deploy-test.yml

# 4. 确认线上版本
curl -s http://demo.example.com:8088/version.txt; echo
git rev-parse --short=12 HEAD      # 两个值应该一致
```

`--force-with-lease` 比 `--force` 安全：如果远端 `test` 上有别人的新提交，它会拒绝推送而不是覆盖。真被拒绝时先 `git fetch origin && git log --oneline -3 origin/test` 看一眼是谁推的。

### 4.2 只在 test 上打个补丁（不推荐）

能跑通，但下次从 `dev` 同步时会被覆盖，补丁就丢了。真要在线上试一个改动，正规做法是在 `dev` 上提交 → 再按 4.1 发。测试环境的意义就是"每个版本都能从仓库复现"，绕过仓库的改动下次部署就没了。

### 4.3 手动触发一次

Actions 页面 → 左侧 `deploy-test` → 右上 `Run workflow` → 选 `test` 分支 → Run。

它跑的是**该分支当时的 HEAD**，不是"上一次成功的提交"，所以重跑前先确认 `origin/test` 指向你要的那一版。

### 4.4 怎么确认这次真的生效了

四个证据，从快到慢：

```bash
# ① 线上版本文件（nginx 里配了 no-store，不会被缓存骗）
curl -s http://demo.example.com:8088/version.txt; echo

# ② 服务器记的当前版本 + 容器实际用的镜像
ssh os "cat /opt/stack/bank-agent/state/current_tag; echo; \
        docker ps --filter name=bank-agent --format '{{.Names}}\t{{.Image}}\t{{.Status}}'"

# ③ 后端自检（顺带看模型端点配没配）
curl -s http://demo.example.com:8088/api/health; echo

# ④ 页面（会弹 Basic Auth：bank / <basic-auth-password>）
#    http://demo.example.com:8088/
curl -s -o /dev/null -w '%{http_code}\n' -u bank:<basic-auth-password> http://demo.example.com:8088/   # 200
```

`/api/health` 正常返回（外面包了一层 envelope，真正的字段在 `data` 里）：

```json
{"request_id":"req_...","data":{"status":"ok","model_configured":true,"demo_mode":true},"error":null}
```

> **注意 `current_tag` 会晚十几秒。** 它是 deploy.sh 最后一步才写的（只有全部健康检查都过了才写）。在这十几秒里 `docker ps` 已经显示新镜像、`current_tag` 还是旧值。判断"新版本上没上"看 `docker ps` 的镜像 tag 和 `/version.txt`；判断"这次部署成没成功"看 `current_tag` 和 Actions 的绿色对勾。

---

## 5. deploy.sh 的七个步骤

服务器端的全部逻辑都在 `deploy/deploy.sh`（272 行 bash）。CI 和人工执行走的是同一个脚本。第一件事是抢部署锁（见第 2 节），抢到才往下走，所以下面七步之前可能先排一会儿队。

| 步骤 | 做什么 | 失败会怎样 |
|---|---|---|
| 1 准备目录 | `mkdir -p releases/ secrets/ state/ data/pgdata/ data/tmp/` | 基本不会失败；权限问题直接 exit |
| 2 准备密钥 | 缺 `secrets/database.docker.txt` 就生成随机密码；缺 `pg_password.txt` 直接报错退出；缺 `llm.env` 生成一份全注释的模板；由 `secrets/basic_auth` 派生 nginx 用的 `secrets/htpasswd` | 两个密码文件不一致会 exit 1（见 6.2） |
| 3 构建镜像 | `docker compose build backend web` | 打印容器状态 + 日志，回滚 |
| 4 起容器 | `compose up -d --remove-orphans --no-build` | 同上 |
| 5 健康检查 | 每 5 秒探 `/api/health`（不需要口令），最多 420 秒；再查 `model_configured`；带 Basic Auth 访问首页必须 200，不带凭据必须 401 | 超时或 401 断言不过 → 打印 `compose ps` + 最近 120 行日志 → 回滚 |
| 6 首次种子 | 只在 `state/bank_seeded` 不存在时跑：探活 bank → `POST /api/seed/reset` → 理财演示数据 → 订阅演示数据 → 断言 bank `/api/health` 200 | 任一步失败 → 回滚（数据可能半初始化，见 8.2） |
| 7 记账清理 | 写 `state/current_tag`；删旧 release（保留 3 个）；删非本次/上次的镜像 | 失败不影响已上线的容器 |

脚本输出会同时 tee 到 `/tmp/deploy-<sha>.log`，这是排障时最重要的一份文件。

---

## 6. 出问题了怎么查

### 6.1 三处日志，按顺序看

| 位置 | 能回答什么 |
|---|---|
| GitHub Actions 页面 | 卡在哪一步（上传源码 / 构建上线 / 回捞日志）；失败时最后 200 行服务器日志会贴在页面上 |
| 服务器 `/tmp/deploy-<sha>.log` | deploy.sh 的完整输出，包括回滚过程 |
| `docker compose logs` | 容器自己说的话（Python traceback、nginx 报错） |

连服务器看日志：

```bash
ssh os                                  # ~/.ssh/config 里的别名，等同 ubuntu@demo.example.com
ls -lt /tmp/deploy-*.log | head -3
tail -n 120 /tmp/deploy-<sha>.log
```

### 6.2 症状对照表（都是真实踩过的）

| 症状 | 根因 | 怎么修 |
|---|---|---|
| `deploy.sh: line 12: set: pipefail: invalid option name` | 脚本是 CRLF 换行（本机 `core.autocrlf=true` 会污染检出） | 已修：`.gitattributes` 给 `*.sh` / `*.yml` / `*.conf` 等加了 `eol=lf`。以后新增文件类型要补一条 |
| backend 起不来，日志 `FileNotFoundError: '/app/docs/contracts/sql/001_init.sql'` | 迁移脚本按相对路径读建表 SQL，而 Dockerfile 是**逐个目录 COPY**，没带 `docs/` | 已修：Dockerfile 里显式 `COPY docs/contracts/`；以后新增被代码读取的顶层目录要照做 |
| `bank 服务 300 秒内没有就绪` | bank 的 `/api/health` 要求模拟状态已初始化，而种子是**之后**才跑的 → 死锁 | 已修：bank 健康检查改 TCP 探活（`socket.create_connection(('127.0.0.1',8001),3)`） |
| `ModuleNotFoundError: No module named 'scripts'` | `docker compose exec` 起的新进程 cwd 是镜像 `WORKDIR=/app`，不是 entrypoint 里 `cd` 过去的 `bank_backend` | 已修：`sh -c "cd /app/bank_backend && ..."`。手工 exec 时也要自己 cd |
| Actions "上传源码" 报 `Permission denied (publickey)` | 仓库 secret `OS_SSH_KEY` 没配 / 配错 / 私钥带口令 | 重新粘 `~/.ssh/arm_key` 全文（含 BEGIN/END 行，无口令） |
| Actions 第一步 `::error::仓库缺少 OS_SSH_KEY secret` | secret 名拼错或没加 | 见 `deploy/README.md` 的「一次性准备」 |
| 页面 502 / 连接被拒 | 容器没起来或 web 挂了 | `compose ps` → `compose logs --tail=100 web backend` |
| 页面能开、对话全废 | `secrets/llm.env` 里 `LLM_*` 没填（`/api/health` 的 `model_configured=false`） | 填 `/opt/stack/bank-agent/secrets/llm.env` 三行，重跑部署 |
| 服务器上 `curl 127.0.0.1:8088` 通、外面连不上 | 只放行了一处：iptables **或** OCI 安全组漏了 | 两处都要（命令见 9.2） |
| 浏览器弹账号密码框 | 正常，这是全站 Basic Auth（`bank` / `<basic-auth-password>`） | 输进去；忘了就看服务器 `secrets/basic_auth` |
| 带正确口令仍 401 | `secrets/htpasswd` 没重新派生（手改了 `basic_auth` 但没重跑部署） | 重跑一次部署，或手工 `openssl passwd -apr1 '密码'` 写进 htpasswd |
| 每个请求都 500，nginx 日志 `auth_basic_user_file` | `htpasswd` 权限不是 644（nginx worker 读不到） | `sudo chmod 644 /opt/stack/bank-agent/secrets/htpasswd` |
| 部署报「Basic Auth 没生效：不带凭据访问首页返回 200」 | `nginx.conf` 的 `auth_basic` 被删了，或者 `/api/health` 被加了口令 | 恢复 `nginx.conf` 第 13-14 行；`/api/health` 与 `/version.txt` 必须 `auth_basic off` |
| 日志里 `等了 900s 还没拿到部署锁` | 另一个部署（或手工重跑）还在跑，15 分钟没结束 | 看 `ls -lt /tmp/deploy-*.log` 最新那份卡在哪；前一个跑完重跑即可 |
| 健康检查超时（首次） | 建库 + 迁移 + 种子慢，或 Postgres 起不来 | 看 `compose logs db`；真慢就 `WAIT_SECONDS=600` 重跑 |
| Postgres `password authentication failed` | `pg_password.txt` 和 `database.docker.txt` 里的密码不一致（手改过 / 只丢了一个） | 从 `database.docker.txt` 第 2 行抄回 `pg_password.txt`；实在不行清库重建（8.3） |
| 磁盘吃紧 | `/` 只剩十几 G（这台机 48G 已用 77%） | 脚本自动只留 3 个 release；再 `docker system df` + `docker image prune` 手动清 |

### 6.3 在服务器上手工重跑

CI 挂了但想先把服务弄起来（或者想看看这次到底哪一步不过）：

```bash
# 用服务器上已有的某个 release 重跑（TAG 由目录名推出）
ssh os "bash /opt/stack/bank-agent/releases/<sha>/deploy/deploy.sh"

# 或显式指定
ssh os "STACK_DIR=/opt/stack/bank-agent TAG=<sha> bash /opt/stack/bank-agent/releases/<sha>/deploy/deploy.sh"
```

`ubuntu` 已经在 `docker` 组里，不需要 `sudo`。手工跑和 CI 跑的唯一区别是日志文件的生成方式（路径一样）。

### 6.4 推之前先验证一次（手工模拟 CI 的两半）

想在推之前确认"这次改动能不能在服务器上跑起来"，把 CI 的两步手工做一遍：

```bash
SHA=$(git rev-parse --short=12 HEAD)

# ① 打包上传（等价于 workflow 的"上传源码"）
git archive --format=tar.gz HEAD \
  | ssh os "mkdir -p /opt/stack/bank-agent/releases/$SHA && tar xzf - -C /opt/stack/bank-agent/releases/$SHA"

# ② 服务器上构建上线（等价于 workflow 的"服务器上构建并上线"）
ssh os "STACK_DIR=/opt/stack/bank-agent TAG=$SHA bash /opt/stack/bank-agent/releases/$SHA/deploy/deploy.sh"
```

注意 `git archive` 只打包**已提交**的内容：工作区里未提交的改动不会上去。要连未提交的改动一起试，先 `git stash create` 拿一个临时 commit 再 archive，或者干脆先提交。

---

## 7. 回滚

### 7.1 自动回滚

`deploy.sh` 在任一步失败时会打印 `compose ps` + 最近 120 行日志，然后按 `state/current_tag` 记录的**上一个版本**做：

```bash
TAG=<上一个 sha> docker compose up -d --remove-orphans --no-build
```

两种情况它**不会**回滚，只能人工介入：

- **首次部署失败**：没有 `current_tag`，没有可回滚的历史版本。
- **上一个版本的镜像已经被删了**：脚本只保留"本次"和"上一次"的镜像，连推三次之后第三次失败就可能滚不回去。

自动回滚只是把**容器**切回旧镜像。数据库迁移是单向的（`alembic upgrade` 不会自动降级），如果这次改动带了破坏性迁移，回滚后旧代码配新库可能仍然不对——这种情况要清库重建（8.3）。

### 7.2 手工回滚到任意版本

```bash
ssh os
cd /opt/stack/bank-agent
ls -1dt releases/*/          # 有哪些版本可用
PREV=<要回到的 sha>

STACK_DIR=/opt/stack/bank-agent TAG=$PREV \
  docker compose -f releases/$PREV/deploy/docker-compose.yml up -d --no-build
echo -n $PREV > state/current_tag      # 让下一次部署知道"上一个版本"是谁
```

### 7.3 回滚之后

`state/current_tag` 必须一起改，否则下次部署的自动回滚会滚到错误的地方。回滚完按 4.4 的四条证据确认一遍。

---

## 8. 数据：种子、重置、清库

### 8.1 为什么种子只跑一次

银行侧 `POST /api/seed/reset` 会**清空整库**再按固定种子重建。所以部署脚本只在 `state/bank_seeded` 不存在时跑它——否则你每次发版都会把演示数据（余额、订阅、模拟时钟）冲掉，真机上正在测的东西全没了。

首次种子的顺序是有依赖的，别打乱：

1. `POST /api/seed/reset` → 基础种子 + 模拟时钟
2. `python -m scripts.seed_wealth_demo` → 理财演示数据（要求模拟时钟已存在）
3. `python -m scripts.seed_subscription_demo` → 订阅演示数据（造 `u_wealth` 客户，后端演示会话固定绑这个客户）
4. 断言 bank `/api/health` 200（它还会校验账户余额与种子版本一致）

两个 seed 脚本都是幂等的（重复执行不重复插入），重跑是安全的。

### 8.2 想让银行数据重来一遍

```bash
ssh os
cd /opt/stack/bank-agent
CUR=$(cat state/current_tag)
DC="docker compose -f releases/$CUR/deploy/docker-compose.yml"
export STACK_DIR=/opt/stack/bank-agent TAG=$CUR

# 清库要两把钥匙（服务凭证 + 运维口令），都在容器环境里，从 os.environ 取而不是
# 拼进命令行——命令行会留在进程列表里。
$DC exec bank /app/bank_backend/.venv/bin/python -c "
import os, urllib.request
req = urllib.request.Request('http://127.0.0.1:8001/api/seed/reset', method='POST',
    headers={'X-Service-Token': os.environ['SERVICE_TOKEN'],
             'X-Ops-Token': os.environ['OPS_TOKEN']})
urllib.request.urlopen(req, timeout=300)"
$DC exec bank sh -c "cd /app/bank_backend && /app/bank_backend/.venv/bin/python -m scripts.seed_wealth_demo"
$DC exec bank sh -c "cd /app/bank_backend && /app/bank_backend/.venv/bin/python -m scripts.seed_subscription_demo"
```

`state/bank_seeded` 还在的话，下次部署会跳过种子（这是对的）；删掉它会让下次部署再跑一遍完整种子。

### 8.3 彻底清库重建

Postgres 数据坏了、或者密码和 `pgdata` 对不上：

```bash
ssh os
cd /opt/stack/bank-agent
CUR=$(cat state/current_tag)
STACK_DIR=/opt/stack/bank-agent TAG=$CUR \
  docker compose -f releases/$CUR/deploy/docker-compose.yml down
sudo rm -rf /opt/stack/bank-agent/data/pgdata /opt/stack/bank-agent/state/bank_seeded
# 下一次部署会重新 initdb + 建库 + 迁移 + 种子（像第一次部署那样，1–3 分钟）
```

### 8.4 数据在哪儿

数据库在 `/opt/stack/bank-agent/data/pgdata`（Postgres 17 容器）。**没有外部备份**，这是测试环境；想留档就自己 `pg_dump` 出来。

账单附件和沙箱数据在 `/opt/stack/bank-agent/data/tmp`（容器内映射到 `/app/.tmp`），删掉只丢上传件。

---

## 9. 改造这套流程

### 9.1 加一个环境变量

- **普通配置**：加到 `deploy/docker-compose.yml` 对应服务的 `environment:` 下，提交进仓库。
- **密钥类**（key、密码）：放服务器 `secrets/llm.env`（compose 的 `env_file`），**不进仓库**。

> 坑：模板里不要写空赋值（`LLM_BASE_URL=`）。空字符串会被 pydantic 当成"已设置"，反而把镜像里 `.env` 的值顶掉。只有真的填了值才注入。

### 9.2 改对外端口（现在是 8088）

要动**四处**，少一处就连不上：

1. `deploy/docker-compose.yml` 的 `web.ports`（`"8088:8088"`，左边是宿主机端口）
2. `deploy/nginx.conf` 的 `listen 8088`
3. `deploy/deploy.sh` 的 `PORT` 默认值（健康检查打这个端口）
4. 服务器网络放行（两处都要）：

```bash
sudo iptables -I INPUT 1 -p tcp --dport 8088 -j ACCEPT
sudo netfilter-persistent save
```

OCI 安全组加一条 ingress：TCP 8088、源 `0.0.0.0/0`（security list `ocid1.securitylist.oc1.ap-tokyo-1.aaaaaaaalcqzdd4r2dqilktr2vqfphm7bejn67mrjsrxj2e6o65vldvj3jsq`）。

### 9.3 加依赖

改 `uv.lock` / `frontend/package-lock.json` 会让下一次构建重跑 `uv sync` / `npm ci`，从"几十秒"变成"几分钟"，属正常现象。**不要在服务器上改 lock 文件**——下次部署会被覆盖。

### 9.4 改 Dockerfile 时的两个陷阱

1. **Dockerfile 是逐个目录 COPY 的**，不是 `COPY . .`。新增一个仓库顶层目录、并且运行时要读它（像 `docs/contracts/` 那样），必须在 Dockerfile 里显式加 `COPY`。
2. **`deploy/Dockerfile.dockerignore` 决定构建上下文里有什么。** 它默认排除了 `.venv` / `node_modules` / `.git` / `dist` 和 `docs/` 的一部分。要带新目录进去，得在这里放行。

改完可以看构建上下文多大：

```bash
du -sh --exclude=.git --exclude=.venv --exclude=node_modules .
```

### 9.5 加一个服务

1. `deploy/docker-compose.yml` 加服务（记得 `pull_policy: never`、`restart: always`、健康检查、绝对路径 volume）。
2. `deploy/deploy.sh` 里 `deploy_failed()` 的 `logs --tail=120 backend bank web` 要把它加上——否则失败时日志里看不到它。
3. 要对外暴露就走 `web` 的 nginx 反代，别新开端口（这台机 80/443/8443/2283 都被别的服务占着）。

### 9.6 改流水线本身

`.github/workflows/deploy-test.yml` 里 host 从仓库变量 `OS_HOST` 读取，user / STACK_DIR 固定在 `env`（`ubuntu` / `/opt/stack/bank-agent`）；换机器时改仓库变量或对应 `env`。私钥只从 secret `OS_SSH_KEY` 取，不要写进文件。

### 9.7 改全站口令（Basic Auth）

口令只存在服务器上的 `secrets/basic_auth`，一行「账号:密码」。改完随便重跑一次部署即可——`deploy.sh` 第 2 步会用 `openssl passwd -apr1` 重新派生 `secrets/htpasswd`，nginx 挂的就是它。

```bash
ssh os
sudo nano /opt/stack/bank-agent/secrets/basic_auth     # 例如改成 bank:新密码
```

三个注意点：

1. **`htpasswd` 必须是 644。** nginx worker 以 `nginx` 用户读它，600 会让每个请求都 500（`deploy.sh` 已经 chmod 644，别手工改成 600）。
2. **`/api/health` 与 `/version.txt` 是 `auth_basic off` 的。** 部署脚本和监控要用它们；别顺手把它们也加上口令，否则部署会以"健康检查超时"失败。
3. **口令不等于身份认证。** 后端所有会话仍是同一个演示用户 `user_0001`，拿到口令的人能看到彼此创建的对话、能改 `/api/settings/llm`。要真隔离得改后端加登录。

---

## 10. 服务器上的东西

```
/opt/stack/bank-agent/
├── releases/<sha>/            每次部署解开的源码（= 仓库里已提交的内容），保留最近 3 个
├── secrets/                   700，只有 ubuntu 能读
│   ├── database.docker.txt    四行凭据：账户：postgres / 密码：<hex> / 外网地址：db / 端口：5432
│   ├── pg_password.txt        同一个密码，Postgres 容器初始化用
│   ├── llm.env                LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（服务器上唯一一处模型 key）
│   ├── basic_auth             全站口令，一行「账号:密码」（部署时派生 htpasswd）
│   └── htpasswd               派生出来的 apr1 哈希，644，nginx 挂到 /etc/nginx/.htpasswd
├── data/
│   ├── pgdata/                Postgres 数据目录（删掉 = 清库）
│   └── tmp/                   账单附件、沙箱（容器内 /app/.tmp）
└── state/
    ├── current_tag            当前版本的 sha，也是自动回滚的目标
    └── bank_seeded            银行演示数据初始化过的时间戳（存在就跳过种子）
```

三个 secrets 文件的硬约束：

- `database.docker.txt` **必须四行、必须用全角冒号「：」**。`read_credentials()` 只按全角冒号切分，用半角冒号会读出一堆空值，然后连不上库。
- `pg_password.txt` 和 `database.docker.txt` 第 2 行**必须是同一个密码**。前者给 Postgres 初始化用，后者给两个 Python 进程连库用；不一致就是 `password authentication failed`。
- 三个文件都不进 git。丢了 `pg_password.txt` 且 `pgdata` 已初始化，只能清库重建。
- `htpasswd` 不能是 600（nginx worker 读不到 → 每个请求 500），也不能手写明文——它要的是 apr1 哈希，交给 `deploy.sh` 生成。

容器与镜像命名：

| 东西 | 名字 |
|---|---|
| compose 项目 | `bank-agent` |
| 容器 | `bank-agent-db-1` / `bank-agent-backend-1` / `bank-agent-bank-1` / `bank-agent-web-1` |
| 镜像 | `bank-agent-app:<sha>`（backend + bank 共用）、`bank-agent-web:<sha>` |

`backend` 和 `bank` 是同一个镜像换入口脚本（`entrypoint-backend.sh` / `entrypoint-bank.sh`），省一次 arm64 构建。

---

## 11. 红线

1. **不要在服务器上 `git clone` / 手改 `releases/<sha>/` 里的文件。** 下次部署整目录覆盖，改了也白改；要改就改仓库再推。
2. **不要 `docker compose down -v`。** 数据用的是 bind mount，`-v` 帮不上忙还容易误伤；清库请按 8.3 显式删目录。
3. **secrets 不进仓库。** `llm.env`、`pg_password.txt`、`database.docker.txt`、`basic_auth` 都只在服务器上。（例外：`deploy.sh` 里写了 Basic Auth 的**初始口令**，方便首次部署自举；换真口令请只改服务器上的 `basic_auth`。）
4. **不要在 `test` 分支上开发。** 它是部署开关，会被 `git branch -f test dev` 强推覆盖。
5. **不要动 443。** 那台机的 443 是 sing-box 的 Reality，占了就没法上网；本方案只用 8088。
6. **别把 `/` 撑爆。** 磁盘 48G 已用 77%，脚本自动保留 3 个 release 和 2 个镜像；手工 `docker build` 实验完记得 `docker rmi`。

---

## 12. 命令速查

| 意图 | 命令 |
|---|---|
| 发一版 | `git branch -f test dev && git push --force-with-lease origin test` |
| 手动触发 | Actions → deploy-test → Run workflow → 选 `test` |
| 看线上版本 | `curl -s http://demo.example.com:8088/version.txt; echo` |
| 看后端状态 | `curl -s http://demo.example.com:8088/api/health; echo`（不需要口令） |
| 验证口令生效 | `curl -s -o /dev/null -w '%{http_code}\n' http://demo.example.com:8088/` → 401；加 `-u bank:<basic-auth-password>` → 200 |
| 连服务器 | `ssh os` |
| 看容器状态 | `ssh os "cd /opt/stack/bank-agent && C=\$(cat state/current_tag) && STACK_DIR=\$PWD TAG=\$C docker compose -f releases/\$C/deploy/docker-compose.yml ps"` |
| 看服务日志 | 上面把 `ps` 换成 `logs --tail=100 backend` |
| 看部署日志 | `ssh os "ls -lt /tmp/deploy-*.log | head -3"` |
| 重跑某次部署 | `ssh os "bash /opt/stack/bank-agent/releases/<sha>/deploy/deploy.sh"` |
| 手工回滚 | 见 7.2 |
| 重置银行数据 | 见 8.2 |
| 清库重建 | 见 8.3 |

---

## 13. 附录：一次性准备（换机器 / 新人接手时才需要）

这台机已经配好了。以下是**已经做过的事**，换机器时照着做：

1. **仓库 secret**：Settings → Secrets and variables → Actions → `OS_SSH_KEY` = `~/.ssh/arm_key` 全文（含 `-----BEGIN/END-----`，无口令）。
2. **服务器目录**：`sudo mkdir -p /opt/stack/bank-agent/{releases,secrets,state,data/pgdata,data/tmp}` + `sudo chown -R ubuntu:ubuntu` + `chmod 700 secrets`。
3. **docker 权限**：`sudo usermod -aG docker ubuntu`（不加的话 deploy.sh 连不上 docker socket）。
4. **端口放行**：iptables `-I INPUT 1` + `netfilter-persistent save`；OCI 安全组加 TCP 8088 ingress。
5. **模型端点**：`/opt/stack/bank-agent/secrets/llm.env` 填 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（取值见仓库根 `key.txt` 或本机 `backend/.env`），`chmod 600`。
6. **全站口令**：不用手工建，首次部署时 `deploy.sh` 会生成随机口令写入 `secrets/basic_auth` 并派生 `htpasswd`；口令只存在服务器上。换口令见 9.7。

服务器环境：Oracle 东京，aarch64，4C/24G，Ubuntu，Docker 29.9.2 + Compose v5.6.1，对外端口 8088；同机还跑着 sing-box（443）和 Immich（2283）。
