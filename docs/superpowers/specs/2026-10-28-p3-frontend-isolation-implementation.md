---
id: specs.p3-frontend-isolation
parent: specs
summary: P3 落地记录：账号页、401、多标签页与迟到响应、报告本地副本按账号分键、上下文归属校验
when: 要复查 P3 改了什么、四个取舍怎么定的、验收口径是什么时
updated: 2026-10-28
---

# P3 实施记录：换人之后，页面上不留上一个人的东西

对应 `2026-10-27-auth-and-user-memory-isolation-design.md` §9 的 P3：

> 前端账号页、401、退出/多页/迟到响应、报告持久化、上下文范围 —— 完成口径：切换账号与并发不残留他人内容。

P1 做完「账号、登录态、CSRF、限速」，P2 做完「独享客户、服务凭证、银行归属」。两者都在
服务端。P3 面对的是最后一段：**服务端已经分开了，浏览器里还没有**。`localStorage` 是整个
源的，退出登录不会清空它；一个标签页退出，另一个标签页什么都不知道；一条上一个人发出去的
SSE 还在流。这些都不报错，只会把 A 的内容显示给 B。

## 1. 四个取舍（用户 2026-10-28 确认）

| 问题 | 选的是 | 一句话理由 |
| --- | --- | --- |
| Q1 报告要不要继续存在浏览器里 | **A** 留副本，键改成「账号:会话」，身份一变就清 | 刷新秒出报告的体验保留，隔离靠键和清场，不靠「删掉功能」 |
| Q2 多标签页同步 | **A** 广播 + 聚焦复核 | 只做聚焦复核要等用户切回去；只等 401 会让空闲页面长时间挂着别人的内容 |
| Q3 账号页做到多大 | **A** 并进现有「系统设置」弹层 | 登录名/角色/银行就绪 + 改密 + 退出全部设备，三件事本来就是一件事 |
| Q4 上下文范围 | **A** 读历史/摘要时自己核对归属，后台任务显式带用户号 | 不动核心装配签名，改动集中在 SQL 与调用点 |

## 2. Q1：报告本地副本按账号分键

报告在服务端已经有一份（跟着助手消息的 metadata），浏览器里这份是「本机刷新立刻看到完整
报告」的加速通道。删掉它就要退化成先看到占位卡再点开，所以留。

留的代价是它落在 `localStorage` 上，而那是**整个源共享**的。于是两条规则：

1. **内容键带账号段**：`<基名>:<账号>:<会话>`，读写都按当前账号拼键。账号段做 URL 编码，
   `:` 不会出现在里面，所以「切出账号段」是按 `:` 分一次，不会因为用户号带冒号串键。
2. **身份一变就清**：登录时清掉不属于当前账号的内容键（含旧版本没带账号段的键），退出/掉线/
   改密/退出全部设备时全清。见 `frontend/src/storageScope.ts`。

键升级到 `bank_agent.settled_reports.v2`。旧键（`v1:会话`、裸基名 `bank-agent.pending-submission`）
不迁移：按设计 §7.3，升级前留下的正文**不能**自动交给下一个登录的人，而「猜它属于谁」比
「让它消失」更危险。这条规则有一个测试盯着（`App.test.ts` 里「does not replay a legacy
single-slot submission」），它第一次跑出来就抓到一个真 bug：清理只匹配 `基名:`，裸基名那
一个键永远留着。

身份未知（还没确认登录的是谁）时内容键一律不读不写：宁可少一份缓存，也不把内容写到一个
下一个账号可能读到的位置。

## 3. Q2：广播 + 聚焦复核

两条独立的网，因为它们的失效场景不重叠：

- **广播**（`BroadcastChannel`）：本页退出/改密时广播一条「这个人变了」，同源其它标签页立刻
  清场回登录页。只在**本页发起的**变化上广播，收到广播的那一页 `broadcast: false` 自我清理，
  否则两个标签页互相回响。
- **聚焦复核**：窗口重新获得焦点时问一次 `GET /api/auth/me`，间隔不少于 15 秒。它盖住广播到
  不了的情况（标签页休眠、浏览器不支持 `BroadcastChannel`）。后端不可达**不**当作被退出 ——
  网络抖动不是撤权，把用户踢回登录页只会让他重输一遍密码再被踢一次。

复核回来的身份和当前不一致（同一台浏览器换了个人）也走同一条出口。

## 4. Q3：账号页并进设置弹层

设置入口对所有人开放，里面三块东西的可见范围不同：

| 区块 | 谁能看 |
| --- | --- |
| 账号（登录名 / 角色 / 银行数据是否就绪 / 改密 / 退出全部设备） | 每个人自己 |
| 收件邮箱 | 每个人自己 |
| 模型配置 | 只有管理员（后端也 403） |

以前整个入口只给管理员，普通用户连自己的邮箱和密码都改不了。

改密和「退出全部设备」都不是「保存成功」，而是一次登出：服务端会把包括当前这个在内的所有
登录态撤销，页面必须回登录页重新登录。所以它们成功后调用的不是「刷新设置」，而是
`signOut('password' | 'logout-all')`，登录页给的话也不同（「密码已修改，请重新登录」）。

## 5. Q4：读历史的时候自己核对归属

`read_ledger`、`latest_valid_summary`、`generate_summaries`、`ContextAssembler` 此前只收一个
`conversation_id`，安全性靠「调用它之前那一步已经校验过归属」。这条链的弱点不是现在的代码，
而是**将来的调用者**：自动起标题、生成摘要、发报告邮件这些后台任务最容易漏那一步，或者把上
一个请求的会话号带过来。

改法是让读的地方自己校验（`backend/app/context/ownership.py`）：

- `require_owner(db, conversation_id, user_id)`：归属查询跑在调用方已有的 session 上，与它
  要守的那次读同一快照；不是本人就抛 `NOT_FOUND`，与「不存在」同一文案同一状态码 —— 分开
  说等于送一个「哪些会话号是真的」的探测器。
- `read_ledger` / `latest_valid_summary` / `_next_version` 都先校验再读。**失败关闭**而不是
  返回空页：返回空会让一次装配在没有历史的情况下照常回答，那是同一个泄漏的静默版本。
- `ContextAssembler` 把 `user_id` 放进**构造函数**，不放 `assemble()`：装配器是按次构建的，
  它构建时绑定的身份就是它唯一能读的身份。`AgentRuntime._graph_for` 把 `context.user_id`
  传给工厂，`app/runtime.py::build_context_assembler` 再传给装配器。
- 404 的唯一措辞提到 `app/core/errors.py::conversation_not_found()`，`ConversationService`
  的 `_not_found()` 改为调它，两处不会各写一句。

成本是每次装配多一次主键查询（会话行）。API 边界仍然先校验一次，所以这是第二道网，不是
替代品。

## 6. 401 与迟到响应

- **401 只有一个出口**：`api.ts` 里每个失败响应都经过 `raise()`，`SESSION_REQUIRED` /
  `SESSION_MISMATCH` 一律回调 `onUnauthorized` → `expireSession()`。放在传输层而不是某个页面，
  因为消息、SSE、上传、账单卡片走的是同一个模块，只在其中一条路上发现 401 就是一页停在别人
  内容上的页面。
- **迟到响应靠世代编号作废**（`frontend/src/identity.ts`）：每个异步动作开始时取一个编号，
  回来先比一次；换人时编号 +1，并且把在飞请求（长 SSE）直接掐断，而不是等它回来再丢掉。
- **敏感响应不缓存**：`/api/*` 统一补 `Cache-Control: no-store`（SSE 自己声明的
  `no-cache, no-transform` 保留），对应设计 §7.3 与 A18。

## 7. 落点

| 文件 | 动作 |
| --- | --- |
| `frontend/src/storageScope.ts` | 新增：账号作用域、内容键、换人清理 |
| `frontend/src/identity.ts` | 新增：世代编号、在飞请求中止、同源广播 |
| `frontend/src/auth.ts` | 退出唯一出口 `signOut`（四条离场路径共用） |
| `frontend/src/AuthView.vue` | 登录/注册；登录页带「为什么回来了」那句话 |
| `frontend/src/api.ts` | CSRF 头、401 回调、`AbortSignal`、`details` 透传 |
| `frontend/src/App.vue` | 未登录只渲染登录页；账号区；广播订阅；聚焦复核；清场函数 |
| `frontend/src/conversations.ts` | 未完成提交按「账号:会话」分键 |
| `frontend/src/reportStore.ts` | 报告键换成 `v2` + 账号段；无身份不读不写 |
| `backend/app/context/ownership.py` | 新增：`require_owner` |
| `backend/app/context/{ledger,summary,view}.py` | 读/写历史前校验归属；装配器持有 `user_id` |
| `backend/app/agents/runtime.py`、`app/runtime.py` | 装配器工厂带上 `context.user_id` |
| `backend/app/core/errors.py`、`conversation/service.py` | 404 措辞收敛到一处 |
| `backend/app/main.py` | `/api/*` 补 `Cache-Control: no-store` |

## 8. 测试

- 前端 `vitest run`：437 条全过。新增 `storageScope.test.ts`（9 条：分键、编码、换人清理、
  旧版残留、偏好键不被误删）、`identity.test.ts`（9 条：世代、中止、广播不回响、无
  `BroadcastChannel` 降级），`App.test.ts` 增加「身份同步」7 条与「账号设置」5 条。
- 后端 `tests/test_context_ownership.py`（7 条）：别人读历史/摘要/写版本/装配一律 404，且
  写版本那条断言「一行都没写」；本人读仍然正常；会话路径（无会话号）不触发校验。
- 后端 `tests/test_auth_api.py` 增加 2 条：带身份的响应 `no-store`，SSE 保留自己的头。
- 顺手修好一条本来红着的用例：`test_conversation_api.py::test_another_users_conversation_reads_as_missing`
  的 `second_session()` 还在造 P1 之前的会话行（只有 `session_id`，没有 `token_hash`），
  `client_for` 取 `session["token"]` 直接 KeyError。现在按 `_issue_session` 的形状造行 —— 它守的
  正是「别人的会话读起来像不存在」，不能是一条跑不起来的测试。

对应验收：A11（迟到响应）、A12（多标签页/刷新/后退）、A08（同用户两对话不串线，归属校验
覆盖摘要链）、A18（敏感响应不缓存）。A17 属于 P4，本轮不宣称。

## 9. 遗留

| 遗留 | 影响 | 归属 |
| --- | --- | --- |
| 本地报告副本仍然存在浏览器里 | 隔离靠「键带账号 + 换人清空」，不是加密边界；同一账号在别的标签页被顶掉后，那份副本直到清场前都还在 | 已接受的取舍（Q1=A） |
| 广播与聚焦复核都是「尽力」 | 标签页被浏览器冻结时，可能到下一次请求才被发现 | 已接受的取舍（Q2=A） |
| 每次装配多一次会话行查询 | 高频装配下是额外一次 PK 查询 | 可接受；如需优化应连同 `session_epoch` 一起做 |
| 后台任务目前只有自动起标题，已带 `user_id` | 将来的摘要/邮件批处理必须显式带用户号，否则会 404（这是设计意图） | 新任务开发时遵守 |
