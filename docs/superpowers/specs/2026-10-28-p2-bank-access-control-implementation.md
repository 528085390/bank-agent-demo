---
id: specs.p2-bank-access-control
parent: specs
summary: P2 落地记录：客户池自愈补位、银行服务凭证与运维口令、资源归属、健康检查解耦
when: 要复查 P2 改了什么、为什么这么改、验收口径是什么时
updated: 2026-10-28
---

# P2 实施记录：独享客户初始化与银行访问控制

对应 `2026-10-27-auth-and-user-memory-isolation-design.md` §9 的 P2：

> 独享客户初始化、服务凭证、银行归属、种子/健康检查 —— 完成口径：A/B 资金与资源独立，初始化重试幂等。

P1 已经把「账号、登录态、CSRF、限速」做完。P2 面对的是另一半：**银行侧 8001 此前没有任何
鉴权**，而且「客户初始化」只有「预生成池」一种实现、池子会耗尽且耗尽后没有回补路径。

## 1. 结论先说

| 问题 | P1 之后的状态 | P2 之后 |
| --- | --- | --- |
| 谁能调 8001 | 任何能发 HTTP 的进程，含本机任何程序 | 只有持有服务凭证的 app 后端 |
| 谁能清库 | 同上 | 只有同时持有服务凭证与运维口令的运维动作 |
| 谁是谁 | 不写 `user_id` 就当成 `u_zhang` | 缺身份直接 422；写了身份还要校验资源归属 |
| 用别人的账户转账 | 可以 | 404，且与「不存在」不可区分 |
| 池子用完后新用户 | 永久没有银行客户，所有银行工具 fail closed | 注册时直接补建一位；平时每领一格就补一格 |

## 2. 三处相对原设计的偏离

### 2.1 客户初始化：自愈补位，不做任务表

原 P2 计划是「跨两库 provision + 初始化任务表 + 状态机 + 超时恢复」，P1 评审时换成了预生成池。
池子的代价是容量上限，而原方案里「重试」的那部分能力被一起砍掉了，于是池空用户无处可去。

选的是第三条路：**领走一格就补一格**，池空时直接为这位用户建号。

- 银行侧新增 `POST /api/pool/provision`：算出下一个可用池号、建客户、号码已存在就跳过。
- app 侧 `app/identity/pool.py::PoolRefiller.refill()` 在注册事务**提交之后**尽力补位；
  `provision_for(user_id)` 处理「池空」这一条，并把号绑到用户。
- 任何一步失败只记日志：补位是水位问题，不能变成注册失败。
- 注册时没绑上的用户，**登录时再试一次**；登录是本人主动动作，所以不需要额外的重试接口与前端入口。

为什么不上任务表：这个项目里客户是 mock 数据、创建是毫秒级，20 套和 200 套没有体验差别；
原设计真正需要的是「真实开户有成本、需要留痕与 SLA」的场景。跨库不一致的代价在这里被从
「真实用户卡在准备中」挪到了「池子少一格，下次再补」，后者是可幂等重试的。

### 2.2 运维口令的范围：只保护清库，不保护时钟

用户选的是「服务凭证 + 独立运维口令」，理由是「app 被打穿也清不了库」。落地时发现一个冲突：
**app 自己的演示控制面就在代理 `POST /clock`**（前端「推进一天／一小时」按钮走
`/api/demo/clock/advance` → 银行 `/clock`）。给 `/clock` 加第二把钥匙，等于让这个按钮失效。

因此运维口令只覆盖 `/api/seed/*`：

- 清库不可逆，app 明确不需要它 → 两把钥匙。
- 推时钟是全局副作用但可逆（推回去就行），且 app 是它的合法调用者 → 只要服务凭证。

这条边界有测试钉住：`test_service_token_alone_can_still_move_the_clock` 与
`test_the_clock_still_needs_a_service_token` 分别守两侧，谁把 `/clock` 划进运维范围，第一条就红。

### 2.3 健康检查：最小就绪与对账分开

原来的 `/api/health` 顺带逐账户校验 `available_minor == balance_minor`。这在多用户下是错的：
一个新客户或一次正常交易不该让容器探针 503（部署脚本还会因此回滚）。

- `/api/health`：库可达、核心表可读、`simulation_state` 存在 —— 容器探针用，不需要凭证。
- `/api/health/deep`：种子版本 + 逐账户对账 —— 排障用，仍返回 503 表示「不就绪」。

## 3. 改动清单

银行侧（`bank_backend`）：

| 文件 | 改动 |
| --- | --- |
| `app/main.py` | `_guard` 中间件：除 `/api/health` 与文档路由外全部要求 `X-Service-Token`；`/api/seed/*` 另需 `X-Ops-Token`。用中间件而非逐路由依赖：新增端点默认受保护，漏写依赖不会变成漏洞 |
| `app/core/config.py` | `service_token` / `ops_token`，空值 = 拒绝一切 |
| `app/core/errors.py` | 1001（401）、1002（403）、1013（422） |
| `app/api/deps.py` | `resolve_user_id()` → `require_user_id()`：缺身份直接拒绝，不再回落 `u_zhang` |
| `app/domain/ownership.py` | `owned(session, Model, id, user_id, label)`：不属于本人一律 404 |
| `app/domain/*_service.py` | 账户/卡/订阅/订单/持仓/定时任务/AA/审计的按 ID 访问统一走归属校验 |
| `app/domain/seed_service.py` | `health()` 拆成最小就绪与 `deep_health()` |
| `app/domain/pool_service.py` | `provision_next()`：下一个可用池号，撞号重算 |
| `app/api/routes.py` | 新增 `POST /pool/provision`；各端点接上 `user_id` |

spec 之外补的两个洞（查代码时发现，属于同一类越权）：

- `transfer_service._writable_account()` 只校验账户存在，不校验归属 —— **可以用别人的账户转账**。
  规则钉在唯一必经的门上（即时转账、定时转账、到期执行三处都过它）。
- `birthday_service` / `subscription_service` 的扣款账户、`aa_service._resolve_receiving_account()`
  的收款账户同样只校验存在。前者等于用别人的钱下单，后者等于把 AA 收款指向第三方。

app 侧（`backend`）：

| 文件 | 改动 |
| --- | --- |
| `app/core/config.py` | `bank_service_token` |
| `app/integration/bank_client.py` | 客户端级默认头 `X-Service-Token`；新增 `service_request()`（无客户身份的调用，目前只有补位）；空 token 时启动告警（症状是「所有工具都失败」，原因却是少一个环境变量） |
| `app/identity/pool.py` | `PoolRefiller`：`refill()` / `provision_for()`，永不抛异常 |
| `app/identity/service.py` | 注册提交后补位、池空时直接建号；登录时对未绑定用户再试一次 |

配置与部署：

- `start.ps1` 新增「准备服务间凭证」：按机器生成一次，写进 `backend/.env`（`BANK_SERVICE_TOKEN`）
  与 `bank_backend/.env`（`SERVICE_TOKEN`、`OPS_TOKEN`）。**故意不导出到进程环境**——子进程会继承，
  那样 app 会连运维口令一起拿到。写文件同时覆盖「手工起 8001」「只重启一个进程」「pytest 直连」这些
  不经过脚本的路径。
- `deploy/deploy.sh`：首次部署生成 `secrets/bank_backend.env` 与 `secrets/app_bank.env`（分成两个文件
  就是为了让 backend 读不到 OPS_TOKEN）；种子步骤从 `os.environ` 取两把钥匙，不拼进命令行。
- `deploy/docker-compose.yml`：backend 挂 `app_bank.env`，bank 挂 `bank_backend.env`。
- 两侧 `.env.example`、`bank_backend/README.md`、`API接口文档.md`、`deploy/README.md`、
  `docs/deploy-cicd.md` 同步。

## 4. 验收

新增测试：

- `bank_backend/tests/api/test_access_control.py`（19 条）：无凭证 401、错凭证 401、只有服务凭证清不了库、
  `/clock` 两侧边界、缺 `user_id` 422、越权读余额/卡片/流水/审计全 404 且**状态码不能用于探测存在性**、
  补位两次得到两个号且跳过已存在号、最小健康检查对余额漂移不敏感而 `/health/deep` 敏感。
- `backend/tests/test_pool_refill.py`（13 条）：注册后补位一次、池空走直接建号、补位器抛异常不影响注册、
  登录补绑、补位写池行、`ON CONFLICT` 幂等、银行 401 时静默、目标用户不存在时把格子退回池尾。
- 既有用例改为「以资源主人身份操作」：AA 分摊项由各自账户主人支付（并断言张三替李四付会被拒 404）、
  对端余额以对端身份读取、领域服务调用补上 `user_id`。

结果：银行侧 351 条全绿；app 侧 `test_pool_refill`、`test_auth_api`、`test_identity_binding`、
`test_bank_client`、`test_demo_clock_api` 全绿。

## 5. 仍未覆盖（明确留到后续阶段）

- 银行侧没有「按角色」的区分：服务凭证是单把钥匙，泄漏即整个客户面失守。真正的租户级隔离要等真实
  银行接口与 mTLS/OAuth。
- `user_id` 的可信度仍来自 app 后端：银行只校验归属，不校验「这个 user_id 是不是调用方绑定的」。
  这条由 app 侧 `customer_resolver` 保证，属于 P2 的既定边界。
- 前端账号页、401 跳转、切换账号时的内容清理是 P3。
