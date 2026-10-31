# P0：注册登录 P1 技术落点

日期：2026-10-27。状态：**已定稿，随代码同批实施**。
上游文档：`2026-10-27-auth-and-user-memory-isolation-design.md`（规划层）。

本文只回答一件事：**P1 到底改哪些文件、建哪些表、定哪些错误码**。规划层的取舍（用户名+密码、
不做自助找回、独享客户、显式偏好）已由用户确认，本文不重复论证。

## 0. 相对规划文档的三处收敛

| 规划写法 | P1 实际做法 | 理由 |
| --- | --- | --- |
| P2「跨两库 provision + 初始化任务表 + 状态机 + 超时恢复」 | **预生成客户池**：`u_pool_01..u_pool_NN` 在银行侧一次性建好，app 侧建池表，注册事务内 `FOR UPDATE SKIP LOCKED` 原子领取 | 隔离效果等价，砍掉整张任务表与状态机；领取在单库事务内，天然幂等 |
| 「每请求查库，移除缓存」 | 照做，但**只对认证路径**；缓存优化留 `session_epoch` 钩子不实现 | P1 正确性优先，热点优化有明确后续路径 |
| 密码 15–128 字符 | 12–128 字符 | 与「首版不做自助找回」配套，15 位必然制造失联账号 |

## 1. 数据模型（迁移 `0018_auth_credentials`）

### 1.1 `app_user` 加列

| 列 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `status` | String(16) NOT NULL | `active` | `active` / `disabled`。`disabled` 时任何登录态立即失效 |
| `role` | String(16) NOT NULL | `user` | `user` / `admin`。管理员只能由运维脚本设置 |
| `created_at` | timestamptz NOT NULL | `now()` | |
| `updated_at` | timestamptz NOT NULL | `now()` | |

`user_0001` 迁移后保持 `role='user'`，作为受控演示档案；管理员由 `scripts/set_password.py --role admin` 显式设置。

### 1.2 新表 `auth_credential`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | uuid PK | |
| `user_id` | String(64) | UNIQUE，FK → `app_user.user_id` |
| `login_name` | String(64) | **UNIQUE**，存归一化后的值 |
| `password_hash` | Text | argon2id 完整编码串 |
| `password_updated_at` | timestamptz | |
| `created_at` | timestamptz | |

登录名归一化：`strip()` → `casefold()`。字符集 `^[a-zA-Z0-9_]{3,32}$`（存原样小写）。
重名靠唯一约束兜底，捕获 `IntegrityError` 转 `LOGIN_NAME_TAKEN`，不做「先查后插」。

### 1.3 `session` 加列（不改表名，保外键）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `token_hash` | String(64) UNIQUE NULL | `sha256(token)` 十六进制；**浏览器凭证的唯一来源** |
| `csrf_hash` | String(64) NULL | `sha256(csrf_token)` |
| `last_seen_at` | timestamptz NULL | 空闲超时基准，节流写入（≥60s 才更新） |
| `revoked_at` | timestamptz NULL | 非空即失效，优先于过期判断 |
| `absolute_expires_at` | timestamptz NULL | 绝对有效期；历史行回填为 `expires_at` |

保留 `session_id` 作为**内部标识**（现有外键与追踪在用），它不再是浏览器凭证。
`expires_at` 保留并继续写，避免历史读路径空值。

### 1.4 新表 `bank_customer_pool`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `customer_id` | String(64) PK | `u_pool_01`… |
| `claimed_by_user_id` | String(64) NULL | UNIQUE，FK → `app_user.user_id` |
| `claimed_at` | timestamptz NULL | |

领取语句（单语句原子，并发安全）：

```sql
UPDATE bank_customer_pool
SET claimed_by_user_id = :uid, claimed_at = now()
WHERE customer_id = (
    SELECT customer_id FROM bank_customer_pool
    WHERE claimed_by_user_id IS NULL
    ORDER BY customer_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING customer_id
```

池空时不阻塞注册：`bank_customer_id` 留 NULL，银行工具按现有契约 fail closed。

**池子的恢复路径是脚本，不是接口**：`bank_backend/scripts/seed_customer_pool.py --count N` 幂等，
已存在的客户跳过、不覆盖余额。银行库被 `POST /api/seed/reset` 清过之后重跑它，会把 1..N 全部
补回来，app 库里的领取记录不动，用户的绑定因此自动恢复。不做 HTTP 补齐接口——它需要一个
服务凭证、一条从 app 到 bank 的内部调用和一个新的失败分支，而运维脚本已经把这件事做完。

### 1.5 新表 `login_attempt`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | uuid PK | |
| `scope` | String(16) | `account` / `ip` |
| `key` | String(255) | 归一化登录名，或客户端 IP |
| `window_start` | timestamptz | |
| `failure_count` | Integer | |
| `locked_until` | timestamptz NULL | |

UNIQUE(`scope`,`key`)。放库里而不是进程内，多 worker 共享。
窗口 15 分钟、5 次失败锁定 15 分钟，按 `account` 与 `ip` 各算一份，命中任一即拒。

## 2. 接口

全部走现有响应信封（`request_id` / `data` / `error`）。

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 无 | 建号 + 领客户，返回 201；**不自动登录** |
| POST | `/api/auth/login` | 无 | 校验后签发 token（HttpOnly Cookie）+ 返回 `csrf_token` |
| GET | `/api/auth/me` | 登录 | `user_id` / `login_name` / `role` / `bank_ready` |
| POST | `/api/auth/logout` | 登录 | 撤销当前登录态 |
| POST | `/api/auth/logout-all` | 登录 | 撤销本人全部登录态 |
| POST | `/api/auth/password/change` | 登录 | 验旧密码 → 改密 → 撤销全部登录态 |
| POST | `/api/session` | 无 | **仅 `allow_demo_login=true` 时可用**，否则 403 `DEMO_DISABLED` |
| GET | `/api/session` | 登录 | 兼容视图，等同 `/auth/me` 的子集 |

Cookie：`bank_session`，`HttpOnly`、`SameSite=Strict`、`Path=/api`、`Secure` 跟 `COOKIE_SECURE`。
CSRF token 走另一个可读 Cookie `bank_csrf`，**`Path=/`** —— 页面在 `/`，而 `document.cookie`
只暴露路径覆盖当前文档的 Cookie，`/api` 作用域下页面读不到它（2026-10-28 由浏览器验收发现
并修正，详见 `_accept/auth-acceptance.md` §6.2）。

### 2.1 CSRF

双保险，缺一即拒（仅对非 GET/HEAD/OPTIONS）：

1. `Origin` / `Referer` 出现时必须与 `Host`（或 `X-Forwarded-Host`）同源；**缺失时放行**，
   因为 curl、健康检查、CLI 工具都不带它，拒绝只会把非浏览器客户端挡在门外。出现就必须
   匹配 —— 这条对**所有带身份的修改类请求**生效，挂在 `routes.identity()` 上，不只是
   `/api/auth/*`（2026-10-28 补齐：此前只覆盖了 auth 路由）。
2. 请求头 `X-CSRF-Token` 的 `sha256` 必须等于当前登录态的 `csrf_hash`（`hmac.compare_digest`）。
   只对 Cookie 通道生效；Bearer 通道不校验，因为浏览器不会自动附加它。

`/api/auth/login` 与 `/api/auth/register` 无登录态，只做第 1 条。

## 3. 错误码

| code | status | 场景 |
| --- | --- | --- |
| `LOGIN_NAME_INVALID` | 422 | 归一化后不满足 3–32 位字母数字下划线 |
| `LOGIN_NAME_TAKEN` | 409 | 归一化后重名 |
| `INVALID_CREDENTIALS` | 401 | 账号不存在 / 密码错 / 账号禁用，**同一文案同一码** |
| `LOGIN_RATE_LIMITED` | 429 | 命中限速 |
| `SESSION_REQUIRED` | 401 | 无 cookie / token 无效 / 已撤销 / 已过期 |
| `SESSION_MISMATCH` | 401 | cookie 与 Bearer 同时出现且不一致（保留原码） |
| `CSRF_REJECTED` | 403 | Origin 或 CSRF token 校验失败 |
| `PASSWORD_POLICY` | 422 | 长度不达标，或与登录名相同 |
| `PASSWORD_UNCHANGED` | 422 | 改密时新旧密码相同 |
| `CURRENT_PASSWORD_INVALID` | 422 | 改密时旧密码错 |
| `FORBIDDEN_ROLE` | 403 | 非管理员访问管理面 |
| `DEMO_DISABLED` | 403 | 演示旁路已关闭（沿用现有码） |

`INVALID_CREDENTIALS` 的文案固定为「账号或密码不正确」，不区分三种失败原因，也不暴露账号是否存在。

## 4. 代码落点

| 文件 | 动作 |
| --- | --- |
| `backend/pyproject.toml` | 加 `argon2-cffi` |
| `backend/app/core/config.py` | 加 `auth_*`、`allow_demo_login`、`bank_pool_size`、`service_token` |
| `backend/app/storage/models.py` | 上述表/列 |
| `backend/app/storage/migrations/versions/0018_auth_credentials.py` | 新增 |
| `backend/app/identity/password.py` | 新增：argon2id 哈希/校验/需重哈希 |
| `backend/app/identity/tokens.py` | 新增：token 生成、sha256、常量时间比较 |
| `backend/app/identity/ratelimit.py` | 新增：DB 限速 |
| `backend/app/identity/service.py` | 重写：注册/登录/退出/改密/鉴权，移除 `_cache` 放行 |
| `backend/app/api/auth.py` | 新增：`/api/auth/*` |
| `backend/app/api/security.py` | 新增：Origin + CSRF 校验依赖 |
| `backend/app/api/routes.py` | `session_id` 只读 cookie 且改语义为 token；`identity()` 走新鉴权；`POST /session` 加开关；LLM 配置收敛管理员 |
| `backend/app/api/demo.py` | `/clock*` 三个接口补管理员校验 |
| `backend/app/storage/seed.py` | 池表播种（幂等） |
| `backend/scripts/set_password.py` | 新增：运维设密码/提权 |
| `bank_backend/app/domain/pool_service.py` | 新增：按 id 幂等 provision 客户 |
| `bank_backend/scripts/seed_customer_pool.py` | 新增 |
| `deploy/deploy.sh` | 首次部署时补跑池播种 |
| `frontend/src/auth.ts` | 新增：认证 store |
| `frontend/src/AuthView.vue` | 新增：登录/注册页 |
| `frontend/src/api.ts` | 带 `X-CSRF-Token`，401 回调 |
| `frontend/src/App.vue` | 去掉免凭据入口，未登录显示 `AuthView` |

## 5. 验收映射（对应上游 A01–A22）

P1 交付后可判定的：A01、A02、A03、A04、A05、A06、A07（对话/审计 ID）、A14（全局配置）、
A18（CSRF/Cookie）、A20（无找回旁路）。
A08–A13、A15–A17、A19、A21、A22 依赖 P2/P3/P4，本轮不宣称通过。

## 6. 明确不做

- 自助找回密码、恢复邮箱、恢复令牌表（用户已确认）。
- 跨对话偏好记忆（原 P4，2026-10-28 取消：安全的偏好只能靠用户手动保存，而手动入口不会被点开）。
- 银行服务凭证全量覆盖（P2）；本轮只保护新增的内部 provision 接口。
- 前端 localStorage 正文迁移（P3）；本轮只在退出时清空。

## 7. 已知遗留（P1 交付后仍然存在，必须在正式对外前处理）

| 遗留 | 影响 | 归属 |
| --- | --- | --- |
| nginx Basic Auth 口令 `bank/<basic-auth-password>` 明文写在 `docs/deploy-cicd.md` 里，已入库 | 外层门禁等于不存在 | 立即：换口令并改成只存服务器 |
| 线上仍是 HTTP + `COOKIE_SECURE=false` | 登录 Cookie 可被同链路窃听 | 对外前：加 HTTPS 终止并开 Secure |
| `bank_backend` 的 `resolve_user_id()` 仍信任传入的 `user_id`，缺省回落 `u_zhang` | ~~任何能连到 8001 的进程可冒充任何客户~~ **P2 已修**：改为 `require_user_id()`，缺身份直接 422，且每个按 ID 的访问都校验归属 | 已关闭 |
| 银行库 `POST /api/seed/reset` 会清空客户池 | 清库后所有已注册用户失联，需重跑 `scripts/seed_customer_pool.py` | 运维流程，已写进脚本头注释 |
| 无 `session_epoch` 缓存，认证每请求一次 DB 查询 | 高并发下是热点，但正确 | 后续优化，需先有跨 worker 撤销机制 |

