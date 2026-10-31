# 模拟银行系统后端（bank_backend）

面向 AI Agent 演示的完整功能 Mock 银行后端。提供稳定的 PostgreSQL 数据、可重复的
业务状态和完整 REST API，**不连接**真实银行、支付、短信、商户或理财市场。

这是独立服务。Agent 后端的卡片管理模块已通过 HTTP 接入；其他五个业务领域尚未注册生产工具。本服务 API 仍可独立运行和测试。

配套契约文档（本目录内）：

- `银行系统需求文档.md`
- `数据库设计文档.md`
- `API接口文档.md`（OpenAPI 3.1，服务实现的 operationId 与之逐字对应）

本目录是**自包含工程**：所有源码、迁移、种子、测试、配置和依赖都在这里，
不依赖也不修改主项目的 `backend/`、`tests/` 或根目录配置。

默认连接信息由仓库根目录的 `database.txt` 提供；本服务通过 `app/core/database_file.py`
读取该文件，应用库名为 `bank_db`，测试库名为 `test`。可在 `bank_backend/.env` 中设置
`DATABASE_URL` 或 `TEST_DATABASE_URL` 指向其他 PostgreSQL 数据库。

## 技术栈

Python 3.12+、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL、pytest、httpx、ruff。

## 快速开始

```bash
cd bank_backend
uv sync --frozen              # 创建 .venv 并安装锁定依赖
uv run alembic upgrade head   # 升级到 0004_card_transaction_password，保留已有数据并初始化密码哈希
uv run uvicorn app.main:app --port 8001
```

服务起来后：`http://localhost:8001/api/health` 返回 `status=ok`（唯一不需要凭证的接口）。
交互式文档在 `http://localhost:8001/docs`。

**首次运行要先有凭证**，否则所有业务接口返回 401：在仓库根跑一次 `启动项目.bat`
（或 `start.ps1`），它会生成 `bank_backend/.env` 的 `SERVICE_TOKEN`/`OPS_TOKEN`，并把
服务凭证同步到 `backend/.env` 的 `BANK_SERVICE_TOKEN`。也可以手工填，见「访问控制」。

默认端口为 8001。Agent 后端使用 `http://127.0.0.1:8001/api` 调用卡片查询与操作接口。
卡片写操作采用准备 → 用户授权 → 执行，详见 [卡片管理说明](../docs/card-management.md)。
新接口要求显式客户号并校验资源归属，仍属于内部 Mock，不提供真实银行认证或真实 MFA。
卡片与理财迁移已合并为 `0003_card_wealth_merge`，其后新增 `0004_card_transaction_password`；两条已部署分支及早期已追加卡片结构的理财环境均可直接升级。银行配置 `DEMO_CARD_PASSWORD` 提供现有演示卡和新申请卡的初始六位交易密码（演示默认 `246810`），只保存随机盐 scrypt 哈希；更改该配置不重置已经初始化的卡。
若共享开发库包含仓库尚未收录的其他外部模块迁移，运行 `uv run python scripts/migrate_cards.py` 只追加卡片结构并保留原迁移历史；完整升级仍需补回对应迁移文件。
演示验证码由 `MOCK_MFA_CODE` 配置，默认 `123456`；连续三次错误取消该操作。

跑测试：

```bash
uv run pytest -q
```

## 访问控制

两层凭证 + 一个必填身份，都不涉及终端用户登录（那是 app 后端的事）。

| 名称 | 请求头 | 环境变量 | 保护什么 | 谁持有 |
|---|---|---|---|---|
| 服务凭证 | `X-Service-Token` | `SERVICE_TOKEN` | 除 `/api/health` 外的全部接口 | app 后端（`BANK_SERVICE_TOKEN`） |
| 运维口令 | `X-Ops-Token` | `OPS_TOKEN` | `/api/seed/*`（清库、重建演示数据） | `deploy.sh`、运维脚本 |
| 客户身份 | `user_id` | — | 请求以谁的身份操作 | app 后端按已绑定客户注入 |

`/api/clock*` 只要服务凭证：app 的演示控制面（前端「推进一天／一小时」）就是在代理它，
加第二把钥匙会让按钮失效。推时钟是全局副作用但可逆，真正不可逆的只有清库。

- **缺服务凭证 → 1001 / 401；缺运维口令 → 1002 / 403；缺 `user_id` → 1013 / 422。**
  空的环境变量等于「拒绝一切」，不是「不校验」。
- 服务凭证证明的是**调用方是谁**，不证明**以谁的身份**：后者由 `user_id` 给出，服务端再
  校验资源归属。不属于本人的账户/卡/订阅/订单/审计一律 1004，与「不存在」不可区分，
  避免用状态码探测别人的资源是否存在。
- 所以下面所有 `curl` 示例都要带 `-H "X-Service-Token: $SERVICE_TOKEN"`；清库、推时钟
  再加 `-H "X-Ops-Token: $OPS_TOKEN"`。凭证从 `bank_backend/.env` 取。

```bash
# 取本机凭证（.env 由 start.ps1 生成，不入库）
SERVICE_TOKEN=$(grep '^SERVICE_TOKEN=' .env | cut -d= -f2)
OPS_TOKEN=$(grep '^OPS_TOKEN=' .env | cut -d= -f2)

curl -H "X-Service-Token: $SERVICE_TOKEN" \
     "http://127.0.0.1:8001/api/accounts/a_zhang_current/balance?user_id=u_zhang"

curl -X POST -H "X-Service-Token: $SERVICE_TOKEN" -H "X-Ops-Token: $OPS_TOKEN" \
     http://127.0.0.1:8001/api/seed/reset
```

## 初始化演示数据

种子不是自动执行的，需要显式调用（或直接用测试库）：

```bash
curl -X POST -H "X-Service-Token: $SERVICE_TOKEN" -H "X-Ops-Token: $OPS_TOKEN" \
     http://localhost:8001/api/seed/reset
```

如需一次准备所有业务域的可查询假数据（持仓、订单、任务、AA、订阅状态、审计和幂等样例），调用：

```bash
curl -X POST -H "X-Service-Token: $SERVICE_TOKEN" -H "X-Ops-Token: $OPS_TOKEN" \
     http://localhost:8001/api/seed/demo
```

`POST /api/seed/reset` 清空业务数据并按数据库设计文档 §3 的固定种子重建，
生成新的 `run_id`。可选传 `{"expected_run_id": "<当前 run_id>"}`，
不匹配则拒绝重置。

### 理财子智能体的专用演示数据（显式、可重复）

理财验收需要一组专用用户、产品和账户，它们**不随服务启动写入**，也不会改动
`p_yuebao` 等原有固定产品：

```bash
uv run python -m scripts.seed_wealth_demo --dry-run   # 只看状态
uv run python -m scripts.seed_wealth_demo             # 写入或校验复用
```

脚本默认使用配置的 `DATABASE_URL`（本工程主库 `bank_db`），可用 `--database-url` 指定
其他演示库；目标库名以 `test` 结尾时默认拒绝，需显式加 `--allow-test-database`。
执行前模拟时钟必须已存在（先跑一次 `POST /api/seed/reset`）。重复执行不会重复入金：
实体齐全时校验后复用，只存在一部分或余额与预期不符时明确拒绝，不静默覆盖。

准备顺序：确认目标数据库 → `uv run alembic upgrade head` → `POST /api/seed/reset`
（基础种子与模拟时钟）→ `uv run python -m scripts.seed_wealth_demo` → 启动服务。

### 一步到位：`scripts.init_demo_data`（只补空库）

上面那串顺序可以用一个脚本代替，它在进程内完成同样的四步（基础种子 → 理财 → 订阅 →
注册客户池），**且只在库为空时动手**：

```bash
uv run python -m scripts.init_demo_data --check   # 只判断，不写数据（退出码 0=已初始化，3=空库）
uv run python -m scripts.init_demo_data           # 空库才初始化，已有数据直接跳过
```

存在的理由是给 `start.ps1` 用：一键启动时服务还没拉起来，走不了 HTTP，而且全新库上
`/api/health` 必然 503（它要求 `simulation_state` 存在）。判断依据是 `simulation_state`
有没有行——库为空才 reset，所以「清空业务数据」的顾虑在这里不成立。
容器化部署仍走 `deploy/deploy.sh` 的 HTTP 路径，两边产出的数据完全一致。

写入内容见数据库设计文档 §6：四款虚构产品（`p_demo_cash`／`p_demo_stable`／
`p_demo_growth`／`p_demo_closed`）、`u_wealth` 的 20000 元主账户与 5000 元备用账户、
另一个客户账户，以及本金 1000 元／价值 1005 元的样例持仓。

**验收用专用 schema**：理财子智能体的端到端验收会真的申购／赎回，所以它跑在一个本次创建、
用完即删的 schema 上（`CREATE SCHEMA test_wealth_accept_<后缀>` + `DATABASE_URL`／`PGOPTIONS`
指向它），不碰 `public`，也不碰别人正在用的 schema。Agent 侧的准备、运行与验收步骤见
`backend/app/modules/wealth/README.md` 与 `backend/tests/wealth_integration/验收记录.md`。

### 理财写接口的模拟验证

理财（申购／赎回）**不按金额分级**：无论金额大小，固定需要 `confirm=true` 且
`mfa_code` 等于配置的演示验证码（`MOCK_MFA_CODE`，默认 `123456`），否则返回 1008；
连续 5 次错码后该资金工具被锁定 15 分钟，锁定期间返回 1009（银行侧 message 固定为
英文 token `locked`，中文文案只在对话侧）。
申购还要求本人有**有效测评**（`POST /api/wealth/risk-assessments` 保存，30 天有效，
以银行模拟时钟为准）且产品风险不高于测评等级；`term_months` 缺省取产品期限，显式传入
必须一致。赎回按产品 `redeem_rule` 与到期日判断可赎回额，不需要重新测评。
转账、订阅、生日等其他域仍沿用原有的金额分级策略，未被本次改动影响。

## 演示主路径（需求文档 §5）

1. `GET /api/contacts?q=爱人` → `POST /api/transfers`（200 元）
2. `POST /api/transfers/scheduled` → `POST /api/clock` 拨钟 → `POST /api/clock/run-tasks`
3. `POST /api/birthdays/orders`（鲜花）→ 拨钟到生日前两天 → `POST /api/clock/run-tasks`
4. `GET /api/bills/month?month=2026-09`、`GET /api/bills/categories`、`POST /api/bills/analyze`
5. `GET /api/products` → `POST /api/wealth/risk-assessments`（测评）→
   `POST /api/products/purchase`（1000 元，带 `confirm` 与演示验证码）→ 持仓 → 模拟收益 → 赎回
6. `GET /api/cards` → 冻结/解冻 → `POST /api/cards/{id}/limit`
7. `GET /api/subscriptions` → `POST /api/subscriptions/sub_netflix/charge` → 取消
8. `GET /api/audit`（每条写操作都有 request_id 与业务 ID）

## 关键约定

- 金额统一为 CNY **整数分**（`*_minor`，数据库 BIGINT），余额不得为负。
- 部分写操作接受请求头 `Idempotency-Key` 或请求体 `idempotent_key`。支持范围与行为以
  [API 文档](API接口文档.md)中各操作的参数为准，不应假设所有写接口都支持幂等键。
  理财写的幂等键会连同业务指纹一起保存：同键同参数返回原回执，同键不同产品／持仓／
  账户／金额／期限返回 1005，不会重复扣款。
- `user_id` 缺省 `u_zhang`；首版不做登录、JWT、严格资源授权或真实 MFA。
- 所有响应统一为 `{code, message, data}`：成功 `code=0`，失败用统一错误码
  （1004 资源不存在、1005 业务状态非法、1006 余额不足、1007 待确认、1008 验证码不正确、
  1009 资金工具已锁定、1012 参数校验失败）。
- **部署顺序**：先部署对话侧 `backend`，再部署 bank_backend。反向部署时，旧的对话侧还不
  认识 1008／1009，会把它们按 HTTP 409 归为通用客户端错误（不会置 `verification_failed`，
  也不会重新等确认，用户看到的是通用报错而不是「验证码不正确。」）。
- 权限分级按金额固定计算：< 1000 元 green，≥ 1000 元 yellow（需 `confirm=true`），
  ≥ 5000 元 red（需 `confirm=true` 且 `mfa_code` 为数字）。**该规则不适用于理财**：
  申购／赎回始终是红色强验证（见上节）。
- 理财只读查询：`GET /api/products`、`GET /api/holdings` 返回 `redeem_rule`、
  `redeemable_minor`、`realized_income_minor`；`GET /api/wealth/risk-assessments/latest`
  返回本人最近测评；`GET /api/wealth/orders/{order_id}` 与
  `GET /api/wealth/orders/by-idempotency-key` 用于核实本人订单与幂等回执。
  这些查询都不计息、不改余额；`POST /api/holdings/{id}/simulate-interest` 只供开发者
  准备演示数据，不作为收益查询开放给模型。
- 模拟时钟单行存储、只能向前拨；拨钟后用 `POST /api/clock/run-tasks` 显式执行到期任务，
  不使用常驻调度器。定时转账、订阅扣款、生日订单共用同一张任务表和同一套执行逻辑。
- 资金写操作在一个事务内同时更新余额、业务记录、流水、审计和幂等结果；
  失败会回滚资金变更，但失败状态与审计保留（任务失败不重试）。
- 服务启动只建立连接，不初始化也不清空业务数据；重置与演示数据初始化都必须显式调用。

## 测试

```bash
uv run pytest -q          # 零配置：默认连共享 test 库
uv run ruff check app tests
```

测试在目标库中创建私有 schema 并跑完整迁移，结束后 CASCADE 删除，因此不会触碰库中已有的表，
也不会互相污染。为控制耗时，schema 是**会话级共享**的（`test_bank_shared_<hex>` 建一次给整轮用例用），
只有 schema 契约类用例才各自建 `test_bank_private_<hex>`；不是每个用例都建一次 schema。

测试库默认是共享 `test` 库，凭据同样来自根目录 `database.txt`。集成测试在专用 schema
中运行并在结束后清理；可通过 `TEST_DATABASE_URL` 覆盖连接。

**测试绝不能指向 `bank_db`**：那是本项目的主库，用例会清空并重建业务表。

## 安全边界与限制（理财相关）

- `user_id` 来自请求参数，不是登录态，也不做严格资源授权；演示服务只能部署在受控环境，
  不声称具备真实银行级鉴权。理财接口按 `user_id` 校验账户、持仓和订单归属，
  不属于本人的资源统一按 1004 拒绝。
- 模拟验证码是配置值（`mock_mfa_code`），不是真实短信或 MFA；验证码错误会写失败审计并
  计入 `security_state` 的失败次数，但验证码本身不落审计、不回显。
- 收益率、收益金额、到期规则全部是模拟数据，不接行情系统，不构成收益承诺。
- 幂等只保证同一 `(user_id, operation_type, idempotent_key)` 不重复执行；它不是分布式锁，
  同一用户的理财写通过行锁串行化，仅覆盖本服务的单库部署。
- 理财写在外层事务里按 用户行 → 账户／持仓行 的顺序加 `FOR UPDATE` 后再复检归属、状态、
  余额与可赎回额；行锁只加在理财写入口，转账／订阅等其他域保持原有实现，因此**跨域的
  同一账户并发写不在本次范围**（计划限定只修理财链路，不建设分布式协调）。
- 服务不自动初始化或清空业务数据；`/api/seed/reset`、`/api/seed/demo` 和
  `scripts/seed_wealth_demo.py` 都必须由开发者在明确的演示或测试库显式执行。

## 本次理财支持的验证记录（2026-10-05）

- `ruff check app scripts tests` 通过；`pytest -q` 全量 301 项通过（含新增的测评、
  订单核实、理财写契约、行锁复检、迁移与验收用例）。
- 环境说明：本机执行 `uv sync --frozen` 时无法访问 PyPI（TLS 握手失败），因此
  `bank_backend/.venv` 未能创建；验证改用同一台机器上已装好相同依赖（FastAPI 0.141、
  SQLAlchemy 2.0.54、psycopg 3.3.5、pytest 9.1）的解释器并显式设置
  `PYTHONPATH=bank_backend`，命令与依赖范围不变。网络可用时应回到 `uv run pytest -q`。
- 手工用真实进程验证重启：专用测试 schema 跑迁移 → 启动 uvicorn → `/api/seed/reset` →
  `scripts/seed_wealth_demo.py`（首次 created、再次 reused）→ 测评 → 申购 2000 元
  （余额 20000→18000 元）→ 停止并重启银行进程 → 测评与订单仍可查、同幂等键重放返回原
  订单且不重复扣款 → 部分赎回 500 元（余额 18500 元、新仓 1500 元），样例持仓收益 5 元
  不受影响；验证结束后只删除该次创建的 schema。

## 理财端到端验收的复核（2026-10-07，任务12）

- 本工程 `pytest -q` 全量 **301 项通过**（44 分 44 秒，连共享 `test` 库、自建自删
  `test_bank_shared_*` schema）。
- 银行进程重启由 Agent 侧的端到端验收再复核一次：专用 schema 上 `POST /api/seed/reset` →
  `seed_wealth_demo` → 测评 + 固定幂等键申购 2000 元（余额 20000→18000 元）→ **停止并重启
  uvicorn 进程**（同一 schema）→ 订单、持仓、测评仍可查，同键重放返回原 `order_id` 且
  `replayed=true`、余额仍为 18000 元，`GET /wealth/orders/by-idempotency-key` 也查得到。
  过程与原始输出见 `backend/tests/wealth_integration/验收记录.md` §5。
