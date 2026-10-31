# Spec 5 — 会话切换延迟：前端缓存/预取 + 后端往返压缩

- 状态：**已实现**（B、C 均已落地，第二轮补强见 §3.7；前端 215 passed | 1 skipped 全绿，后端全量回归见 §9）
- 日期：2026-10-13
- 基线：`D:\ai_project\agent` @ `1fa63ae`（dev），行号基于**工作树**（已含第一轮切换修复，未提交）
- 上位文档：`docs/superpowers/specs/2026-09-27-conversation-persistence-spec.md`（会话持久化，已冻结 @ `ed47abb`）
- 用户原话：
  - m00001：「该项目的前端页面 http://127.0.0.1:5173/，每次切换会话都要处理中，卡住一小段时间」
  - m00240：「载入会话感觉还是有点慢，像网页版deepseek，豆包，gpt这种切会话基本上都是秒切，还能更快吗」
  - m00274：「将来上线肯定不用本机啊，还是用阿里云不能动，做b和c」
  - m00299：「关，写spec文档」
  - m01065：「切换会话的时候会卡一下，并且全部会话会不可选择，这是什么问题」（第二轮）
  - m01156：「可以了，那边的任务已经完成了，这边的修复可以提交」

## 0. 结论

### 0.1 症状与根因

切换会话时整块对话面板被「处理中」接管约 1.25 s。瓶颈不在渲染、不在 JS，在**串行的远程数据库往返**：

1. `frontend/src/App.vue:335-343 openConversation()` 串行 `await` 两个 HTTP 读（先 `loadHistory()` 再 `loadDetail()`）；
2. 每个读在后端要 3–4 次连接池 checkout，每次 checkout 都要跨公网到阿里云 RDS；
3. `busy` 是**全局** ref（`App.vue:46`），切换通过 `action(...)` 就地把整页交给 `App.vue:1000` 的 `<div v-if="busy" class="pending">处理中</div>`。

关键实测：响应体只有 450–1700 字节，耗时却与负载无关 ⇒ 是**固定的每往返开销**，不是查询或传输成本。

### 0.2 已完成（本 spec 的基线之内，尚未提交）

第一轮修复已落在 `frontend/src/App.vue`（`git status` 为 `M`，`git diff --stat` 41 insertions / 20 deletions）：

- 两个读改成 `await Promise.all([loadHistory(), loadDetail()])`；
- 切换不再走接管页面的 `action(...)`，改用 `switching`（`App.vue:50`）+ 面板内的「正在载入会话」提示。

Playwright 实测（127.0.0.1:5173，30 行，各 3 次）：

| 指标 | 修复前（HEAD） | 修复后 |
| --- | --- | --- |
| 面板空白起点 | 18/26/24 ms | 17/32/16 ms |
| 首次内容可见 | 537/542/522 ms | 506/524/523 ms |
| 面板可用（pane_settled） | 1254/1201/1251 ms（均 1235） | 720/738/733 ms（均 730） |
| 全局「处理中」 | 18/23/13 ms 出现 | **从未出现** |
| 两个请求发出间隔 | 507.2/504.6/486.0 ms（串行） | 0.1 ms（并行） |

省 505 ms（41%）。面板不可用从 1235 ms 降到 730 ms。

### 0.3 本次要做的

剩余 730 ms 全是远程往返，分两刀：

- **B（前端）**：会话视图缓存 + 悬停预取 + 命中后后台复核（stale-while-revalidate）。再次切回 **~0 ms**。
- **C（后端）**：identity 缓存、合并 `get()`/`messages()` 的双 session、关闭 `pool_pre_ping`。首次进入 **~730 ms → ~300 ms**。

两刀互不依赖：B 让重复切换免网络，C 让首次进入更快。

### 0.4 明确不做

**不把开发环境切到本机 PostgreSQL。** 用户已裁定（m00274）：上线走阿里云，数据库不能动。本机 `postgresql-x64-18` 服务在跑、`127.0.0.1:5432` 可达，仅作为将来离线调试的备选，本 spec 不启用。

## 1. 需求

| # | 需求 | 验收 |
| --- | --- | --- |
| R-1 | 再次切回已看过的会话必须「秒切」 | 命中缓存时**同步**渲染账本；期间把该会话的请求全部拦住也不影响内容出现（新增前端用例） |
| R-2 | 命中缓存后仍要复核，且晚到的响应不得覆盖用户已经切走的会话 | 命中后仍发出 `GET /api/conversations/{id}/messages` 与 `GET /api/conversations/{id}`；只有 `current.value?.conversation_id === id` 时才写 `messages`/`current`（新增前端用例） |
| R-3 | 账本变化后缓存不得残留旧账本 | 回合开始（`submitTurn`）即 `forget`；`settle`/`refreshHistory`/`loadEarlier` 经写穿刷新（新增前端用例） |
| R-4 | 元数据变更后缓存不得残留旧元数据 | `applyConversation` 同步 `patch` 缓存中的 `detail.conversation`（改名/置顶/归档/软删除/恢复） |
| R-5 | 悬停即预热，且不得干扰当前视图 | `pointerenter`/`focus` 后去重触发一次后台双读；不写 `current`/`messages`；同时最多 1 个预取在跑；`busy` 时不预取（新增前端用例） |
| R-6 | 后端 `get()`/`messages()` 每次请求只 checkout 一次 | 用 `before_cursor_execute` 计数断言 checkout 数 2 → 1（新增后端用例） |
| R-7 | identity 缓存不得放松过期语义 | 命中不发查询；`expires_at` 已过的 session 仍返回 401 `SESSION_REQUIRED`（新增后端用例） |
| R-8 | 关闭 `pool_pre_ping` 不得让连接复用失效 | 配 `pool_recycle=1800`；后端全量保持 577 passed, 2 skipped |
| R-9 | 既有不变量不回归 | `frontend/src/App.test.ts` 现有 3 条切换用例保持绿；`docs/superpowers/specs/2026-09-27-conversation-persistence-spec.md:177` 的「切换不带走在途回合」仍成立 |

## 2. 既有事实

### 2.1 前端切换路径与状态（`frontend/src/App.vue`，工作树行号）

- 相关 ref：`46 busy`（接管页面的操作 → 全局「处理中」）、`50 switching`（打开会话，纯读）、`81 conversations`、`91 current`、`92 messages`、`93 historyCursor`、`94 historyBusy`、`96 pending`、`98 recoveryRequired`、`101 live`。
- `114 const locked = computed(() => busy.value || switching.value)`；`115-117 composerDisabled = !session || locked || !!pending || recoveryRequired || (current && !writable)`。
- `111 const turns = computed(() => historyTurns(messages.value))` —— 面板内容**只**由 `messages` 派生。
- `236-245 clearConversationView()`：`messages=[]`、`historyCursor=null`、`live=null`、`replyNotice=null`、`recoveryRequired=false`、`renamingId=''`、`setPending(null)`、`draft=''`。
- `251-255 resetView()`：`clearConversationView()` + `error=''` + `trace=''`。
- `288-299 refreshConversations()`：`loadConversationList()`；若打开的会话已不在列表里则 `await loadDetail({ syncPending: false })`。
- `323-327 applyConversation(updated)`：替换 `conversations` 中同 id 的行，并同步 `current`。
- `329-332 switchConversation(conversation)` → `action(() => openConversation(conversation), { blocking: false })`。
- `335-343 openConversation(conversation)`：`current = conversation`；`resetView()`；`saveLastConversation(id)`；`await Promise.all([loadHistory(), loadDetail()])`。
- `353-362 loadHistory()`：`listMessages(id, { limit: 50 })`；`messages.value = page.items`；`historyCursor = page.next_cursor`。
- `365-377 loadEarlier()`：`listMessages(id, { limit: 50, after: cursor })`；`messages.value = mergeHistory(...)`。
- `383-390 loadDetail({ syncPending })`：`getConversation(id)`；`current = detail.conversation`；`recoveryRequired = detail.recovery_required`；`if (options.syncPending !== false) setPending(pendingInterruptOf(detail))`。
- `393-395 refreshHistory()`：`try { await loadHistory(); await loadDetail() } catch {}`。
- `397-401 togglePinned` / `403-407 toggleArchived` / `410-414 removeConversation` / `416-419 undeleteConversation` / `427-435 commitRename` —— 全部 `applyConversation(await updateConversation|deleteConversation|restoreConversation(...))`。
- `499-549 submitTurn(conversationId, body, key, userText)`：`live.value = turn`；`runSubmissionTurn({ submit, onEvent })`；`result.kind === 'settled'` → `await settle(conversationId)`。
- `556-567 settle(conversationId)`：若 `current` 是该会话则 `await loadHistory(); await loadDetail()`；`live = null`；再 `await refreshConversations()`；再 `void pollForAutoTitle(conversationId)`。
- `718 decidePendingAction(...)` 之后 `738 await refreshHistory()`。
- `753-755 startSession()` 只做 `session.value = await request<Session>('/session', {})`；`757-765 bootstrap()` 才在已有 session 时 `await restoreView()`（`772`）。**这是另一处既有缺陷，见 §8。**

### 2.2 后端往返构成（实测，`_probe/detail_breakdown.py`）

方法：`sys.path.insert(0,"backend")` → `Runtime(get_settings())` → 挂 sqlalchemy `before_cursor_execute` 监听器计数 → `run()`/`arun()` 取 4 轮最小值。

| 操作 | 耗时 | 查询数 |
| --- | --- | --- |
| `engine.connect + SELECT 1` | 148.5 ms | 1 |
| `identity.get` | 187.2 ms | 2 |
| `conversations.get` | 305.0 ms | 2 |
| `turns.recovery` | 192.6 ms | 1 |

- 187.2 + 305.0 + 192.6 = 684.8 ms ≈ 实测 HTTP 730 ms（`GET /api/conversations/{id}`）。**没有任何时间花在计算或渲染上**，全是 checkout 与 SQL。
- `pool_pre_ping=True` 3 次 checkout 最小值 439.2 ms；`pool_pre_ping=False` 最小值 303.9 ms ⇒ **pre_ping ≈ 45 ms / checkout**。
- 每次 `with self.database.sessions()` 都付一次 checkout；detail 路径共 4 次（identity 1 + get 2 + recovery 1）。

对照实测（Node fetch → 127.0.0.1:8000，热）：

| 请求 | 耗时 |
| --- | --- |
| `GET /api/conversations/{id}/messages?limit=50` | 517.1 ms |
| `GET /api/conversations/{id}` | 730.8 ms |
| 串行两者 | 1249.0 ms（3 轮均值） |
| 并行两者 | 724.8 ms（3 轮均值） |

### 2.3 双 session 的具体位置（`backend/app/conversation/service.py`）

- `445-449 get()`：`row = self._owned(...)` 开一个 session → `return self._view_of(row)` 再开一个。
- `509-522 require_writable()`：同样 `_owned()` + `_view_of()`。
- `526-551 messages()`：`536 if self._owned(conversation_id, user_id) is None` 开一个 session，`545 with self.database.sessions() as db` 再开一个。
- `808-815 _owned(conversation_id, user_id)`：`with self.database.sessions() as db` → `select(Conversation)`。
- `852-854 _view_of(row)`：`with self.database.sessions() as db` → `_view(row, _message_count(db, row.conversation_id))`。
- `857-865 _message_count(db, conversation_id)`：`select(func.count())`。
- `66-67 DEFAULT_MESSAGE_PAGE_SIZE = 50`、`MAX_MESSAGE_PAGE_SIZE = 200`。
- 注意 `database.py:10` 是 `sessionmaker(self.engine, expire_on_commit=False)`，所以 `with` 退出后 ORM 对象属性仍可读 —— 合并 session 不会让 `row.archived_at` 之类的读取失效。

### 2.4 连接池与数据库

- `backend/app/storage/database.py:9`：`create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})`。没有 `pool_recycle`，没有显式 `pool_size`/`max_overflow`（SQLAlchemy 默认 5/10）。
- `backend/app/storage/database.py:7-8`：非 PostgreSQL 直接 `ValueError("运行时需要 PostgreSQL；不以 SQLite 替代资金行锁语义")`。
- `backend/app/core/config.py:18-20`：`database_url` 默认 `sqlalchemy_url(DATABASE_NAME)`，注释明确 `DATABASE_URL` 是**文档化的覆盖点**。
- `database.txt`（仓库根，**故意提交**）：账户 `root01` / 外网地址 `rm-cn-rct4ynao80001jso.rwlb.rds.aliyuncs.com` / 端口 5432。实测：新建连接 144.5–160.3 ms，单查询 67.8–75.7 ms，复用连接下 10 次查询均摊 44.3 ms。

### 2.5 identity 与 session

- `backend/app/api/routes.py:67-68`：`async def identity(request): return await run_in_threadpool(runtime(request).identity.get, session_id(request))` —— **每个路由都付**。
- `backend/app/api/routes.py:56-64 session_id(request)`：cookie `bank_session` 或头 `X-Session-Id`（都有且不一致 → 401 `SESSION_MISMATCH`；都没有 → 401 `SESSION_REQUIRED`）。
- `backend/app/identity/service.py:47-55 Identity.get(session_id)`：`with self.database.sessions() as db` → `require_session(db, session_id)`（1 查询）+ `select(AppUser)`（1 查询）。
- `backend/app/identity/service.py:10-19 require_session(db, session_id, user_id=None)`：`row.expires_at <= datetime.now(timezone.utc)` 即 `AppError("SESSION_REQUIRED", "会话已失效，请重新进入演示", 401)`。
- `backend/app/core/config.py:23 session_hours: int = 24`。
- **全仓库没有任何登出/吊销路径**（只有 `create_demo_session`，`identity/service.py:26-45`）⇒ session 的唯一失效来源是过期 ⇒ 缓存可以精确校验过期后安全使用。

### 2.6 测试基线（`docs/superpowers/specs/2026-09-27-conversation-persistence-spec.md:162-171`）

- `cd backend && uv run pytest -q` → 577 passed, 2 skipped
- `uv run ruff check app tests scripts` / `uv run ruff format --check app tests scripts`
- `cd frontend && npm test` → 7 passed | 1 skipped (8 files)；163 passed | 1 skipped (164 tests)
- `npm run build`（`vue-tsc --noEmit && vite build`）→ clean

## 3. 设计 B — 前端缓存 + 悬停预取

### 3.1 新模块 `frontend/src/conversationCache.ts`

纯内存、无依赖、可单测：

```ts
export interface CachedView {
  // 三字段皆可选：两个读并行，谁先回来就先交还哪半（§3.7）
  messages?: ConversationMessage[]
  historyCursor?: string | null   // 见 §3.6：next_cursor 是字符串，文档原写的 number 是错的
  detail?: ConversationDetail
}
type CachedHalf =
  | { messages: ConversationMessage[]; historyCursor: string | null }
  | { detail: ConversationDetail }
export function recall(id: string): CachedView | undefined   // LRU touch，返回副本
export function remember(id: string, half: CachedHalf): void // 合并半个 entry（§3.2 的两个读并行）
export function patch(id: string, conversation: Conversation): void  // 只改 detail.conversation
export function forget(id: string): void
export function resetCache(): void                          // 测试用
```

- LRU 上限 `MAX_ENTRIES = 20`；TTL `CACHE_TTL_MS = 5 * 60_000`（内存与陈旧度的兜底；正确性不依赖 TTL，见 §5）。
- 刷新页面即失效，不落 localStorage：账本里有资金相关事实，不跨会话留存。

### 3.2 写穿（write-through）

`loadHistory()` 与 `loadDetail()` 读到的数据**直接写进缓存**。这一条是设计的关键简化：`settle()`、`refreshHistory()`、`loadEarlier()`、`refreshConversations()` 都只是调用这两个函数，因此**自动**刷新缓存，不需要各自再写失效逻辑。

- `loadHistory()` 成功后：`remember(id, { messages: page.items, historyCursor: page.next_cursor, detail: recall(id)?.detail ?? null-ish })`。
- `loadDetail()` 成功后：把 `detail` 写进同一 entry。
- 两个读并行时，谁先回来谁先建 entry，另一个只补自己的字段（`remember` 做合并而非整体覆盖）。

### 3.3 切换

```
openConversation(conversation):
  current = conversation; resetView(); saveLastConversation(id)
  cached = recall(id)
  if cached: 同步填充 messages / historyCursor / current / recoveryRequired / setPending(...)   // 0 网络
  await Promise.all([loadHistory(), loadDetail()])   // 未命中=首次加载；命中=后台复核
```

命中时同步填充必须**在 `await` 之前**完成，这样「秒切」才是真的同步渲染，而不是「快一点的异步」。

### 3.4 后台复核（stale-while-revalidate）与竞态保护

命中缓存后仍然发出两个读。**新增守卫**：`loadHistory()`/`loadDetail()` 写 `messages`/`current`/`recoveryRequired`/`pending` 之前，必须确认 `current.value?.conversation_id` 仍等于本次加载的 id；不等则只写缓存、不写视图。

这条守卫同时修掉一个**既有的隐患**：今天 `loadHistory()`（`App.vue:359`）无条件 `messages.value = page.items`，只要一次慢响应晚于一次切换回来，就会把旧会话的账本画到新会话上 —— 与 `docs/superpowers/specs/2026-09-27-conversation-persistence-spec.md:177` 的不变量直接冲突。缓存让切换变快后这条竞态更容易命中，因此必须一起修。

### 3.5 悬停预取

- 触发点：`.conversation-row` 的 `pointerenter` 与 `pointerleave`（文档原写 `focus`，但该元素是没有 `tabindex` 的 `div`，键盘聚焦不到 —— 见 §3.6）。
- 延迟 `PREFETCH_DELAY_MS = 120`（鼠标扫过不算），去重：同一 id 已有在途预取则跳过。
- 并发上限 1：同时只跑一个预取，避免鼠标快速扫过列表时打出一串请求。
- `busy.value` 为真时不预取。
- 预取只调 `listMessages` + `getConversation` 并写缓存，**不碰** `current`/`messages`/`pending`/`recoveryRequired`。

### 3.6 实现与本文档的偏差（落地后补记）

| 位置 | 文档原写法 | 实际实现 | 原因 |
| --- | --- | --- | --- |
| §3.1 | `historyCursor: number \| null` | `string \| null` | `frontend/src/types.ts:186` 的 `MessagePage.next_cursor` 就是 `string \| null`（`frontend/src/App.vue:93` 亦然），文档写错了 |
| §3.1 | `remember(id, view: CachedView)` | `remember(id, half: CachedHalf)` | §3.2 要求两个并行的读合并同一个 entry；整体覆盖会把先到的另一半抹掉 |
| §3.5 | `pointerenter` 与 `focus` | 只接 `pointerenter` / `pointerleave` | `.conversation-row` 是没有 `tabindex` 的 `div`，键盘根本聚焦不到，接 `focus` 是死代码 |
| §3.3 | 载入提示 `v-if="switching"` | `v-if="switching && !messages.length"` | 命中缓存时面板已经有内容，再显示「正在载入会话…」自相矛盾 |
| §3.1 | `recall` 只在两半齐全时返回 | 有哪半交还哪半（§3.7） | 半份就能用：账本可单独铺满面板、详情可单独设确认问题；等两半齐全会让点击白等一个详情往返 |

### 3.7 第二轮补强（切换被静默吞掉 + 半份缓存）

用户 m01065 报「切换会话的时候会卡一下，并且全部会话会不可选择」。诊断（`_probe/switch_storm.py`、`_probe/layer_breakdown.py`、`_probe/route_profile.py`）给出两条**前端**成因：

1. **切换期间的点击被静默丢弃**：`switchConversation` 开头 `if (locked.value || ...) return`，而 `locked = busy || switching`（`App.vue:119`）。一次切换把 `switching` 举住整个读过程（0.6–1.3 s），这期间**每一行的点击都直接 return**，而行是普通 `div`、没有任何禁用样式 ⇒ 看起来就是「全部会话不可选择」。回复流式进行中（`busy`）窗口更长。
2. **预取与点击各发一份同样的请求**：悬停触发的预取在途时点击同一行，`readAhead` 与 `openConversation` 的 `Promise.all` 不共享请求 ⇒ 一次切换发 4 条而不是 2 条。

修法：

- **新模块 `frontend/src/inflight.ts`**：`share<T>(key: string, read: () => Promise<T>): Promise<T>` —— 同一 key 的在途 promise 直接复用，settle 后立即移除（**不缓存结果**，缓存职责仍归 `conversationCache`）。`read` 用 `Promise.resolve().then(read)` 推迟到下一个微任务，保证「先登记、后执行」；清理挂在 `started.then(done, done)`，失败也清理且不留未处理的 rejection。
- **键**：`firstPageKey(id) = 'messages:' + id + ':first'`、`earlierPageKey(id, cursor) = 'messages:' + id + ':after:' + cursor`、`detailKey(id) = 'detail:' + id`；`loadHistory`/`loadEarlier`/`loadDetail`/`readAhead` 全部改走 `share(...)`。
- **后点的切换取代先点的**：`switchConversation` 用 `switchGeneration` 计数器，进入时 `const generation = ++switchGeneration`；`finally` 里只有 `generation === switchGeneration` 才清 `switching`。守卫从 `locked` 改为 `busy`（回复进行中仍不允许切换，见 §8），于是 0.6 s 内的第二次点击不再被吞掉。
- **半份缓存可见**：`recall` 有哪半交还哪半；`openConversation` 分别填 `cached.messages` 与 `cached.detail`；`readAhead` 每半到手就 `remember`（`share(...).then(...)`），不再等两半齐全。

实测（`_probe/hover_click.py`，1440x900，真实演示库）：

| 场景 | 点击后 5/10/20 ms | 到 240 ms |
| --- | --- | --- |
| 悬停预取已取回账本、详情仍在途时点击 | **4 条 turn 已在**、无「正在载入会话」、无「处理中」 | 不变（只补标题） |
| 悬停后立刻点击（两半都还没回来） | 0 turn + 「正在载入会话」 | 240 ms 出 5 条 turn |

`_probe/switch_race.py` 复测：冷切换 `title_ms 52 / first_ms 223`，全程无「处理中」；点第 2 行后 40 ms 点第 3 行，最终停在**第 3 行**（修复前停在第二行）；悬停后点击共 3 条请求（预取 2 条 + 复核 1 条）。

## 4. 设计 C — 后端往返压缩

### 4.1 C1 合并 session（纯读，语义不变）

在 `backend/app/conversation/service.py` 内把「先 `_owned()` 再 `_view_of()`」改成单 session 内两条查询：

```python
def _owned_in(self, db, conversation_id, user_id) -> Conversation | None: ...
def _view_in(self, db, row) -> ConversationView:      # _view(row, _message_count(db, row.conversation_id))
    ...
```

- `get()`（`445-449`）、`require_writable()`（`509-522`）、`messages()`（`526-551`）各改为**一个** `with self.database.sessions() as db` 内完成所有权校验与取数。
- 保留现有 `_owned()`/`_view_of()` 作为单次使用的便捷包装，或直接删除 —— 由实现决定，但**不允许**留下半合并状态。
- 语义不变：这些都是只读路径，合并前后查询集合完全相同，只是少一次 checkout。

### 4.2 C2 identity 进程内缓存

在 `backend/app/identity/service.py` 的 `Identity` 上加：

```python
class Identity:
    _cache: dict[str, dict]           # session_id -> {user_id, display_name, expires_at}
    _lock: threading.Lock             # 路由在 run_in_threadpool 里跑，必须线程安全
    _MAX_ENTRIES = 1024
```

- 命中路径：取出条目 → **仍然**用 `expires_at <= datetime.now(timezone.utc)` 判定过期 → 过期即删除并抛 `SESSION_REQUIRED`（与 `require_session` 完全一致）。
- 未命中路径：走现有 2 条查询，成功后写缓存。
- 淘汰：超过 `_MAX_ENTRIES` 时清理已过期项；仍超则按插入顺序淘汰最旧。
- 安全性论证（§2.5）：全仓库没有登出/吊销路径，session 唯一失效来源是过期，而过期在命中时精确复核 ⇒ 缓存不引入新的授权窗口。
- 多进程（uvicorn workers）各自持有一份，不共享 —— 正确性不依赖共享。

### 4.3 C3 关闭 `pool_pre_ping`（用户已裁定：关）

`backend/app/storage/database.py:9` 改为：

```python
self.engine = create_engine(
    url,
    pool_pre_ping=False,
    pool_recycle=1800,
    connect_args={"connect_timeout": 5},
)
```

- 理由：pre_ping 每次 checkout 多一个 `SELECT 1` ≈ 45 ms（§2.2 实测）。C1+C2 后 detail 路径只剩 2 次 checkout，再省 ≈ 90 ms。
- 代价（明确记录）：RDS 侧掐掉的空闲连接不再被透明替换，首次使用会以 `OperationalError` 冒到调用方。缓解是 `pool_recycle=1800`（30 分钟回收，短于常见云 RDS 空闲断连阈值）。
- 这是本 spec 中唯一牺牲可靠性的取舍，由用户 m00299 明确选择。

### 4.4 C 之后的预期构成

| 环节 | 现在 | 之后 |
| --- | --- | --- |
| `identity.get` | 187 ms（2 查询 + 1 checkout） | ~1 ms（缓存命中） |
| `conversations.get` | 305 ms（2 查询 + 2 checkout + pre_ping） | ~148 ms（2 查询 + 1 checkout，无 pre_ping） |
| `turns.recovery` | 193 ms（1 查询 + 1 checkout + pre_ping） | ~148 ms（无 pre_ping） |
| 合计（detail 关键路径） | ~685 ms（实测 HTTP 730 ms） | **~300 ms** |

`messages` 路径：2 查询 + 2 checkout（517 ms）→ 2 查询 + 1 checkout、无 pre_ping（~148 ms），不再是关键路径。

## 5. 缓存失效矩阵

| 写点 | 代码位置（工作树） | 缓存动作 |
| --- | --- | --- |
| 打开会话 | `App.vue:335-343` | 命中→同步填充；未命中→并行双读后写穿 |
| 读账本 | `App.vue:353-362` | 写穿 `messages` + `historyCursor`（带 §3.4 守卫） |
| 读更早历史 | `App.vue:365-377` | 写穿 `mergeHistory` 后的数组 + 新游标 |
| 读详情 | `App.vue:383-390` | 写穿 `detail`（带 §3.4 守卫） |
| 回合开始 | `App.vue:499-549` `submitTurn` | `forget(id)` —— 在途回合会让账本变旧 |
| 回合结束 | `App.vue:556-567` `settle` | 经 `loadHistory`/`loadDetail` 自动写穿 |
| 决定待决动作 | `App.vue:718` → `738 refreshHistory` | 经 `refreshHistory` 自动写穿 |
| 元数据变更 | `App.vue:397-435` `applyConversation` | `patch(id, updated)` |
| 会话从列表消失 | `App.vue:288-299` `refreshConversations` | `forget(id)` |
| 新建会话 | `App.vue:345-351` `newConversation` | 无缓存，无需动作 |

TTL（5 分钟）只是兜底：正确性来自上表的显式失效 + 每次打开都复核。

## 6. 测量与验收方法

沿用第一轮已验证的手段（`_probe/` 下的 Playwright 脚本，解释器 `D:\ai_project\douyin-auto-fire\.venv\Scripts\python.exe`）：

1. **切换计时**：`_probe/switch_timing.py` 逐次记录 `pane_blank_at_ms`、`first_content_painted_ms`、`pane_settled_ms`；`_probe/switch_cached.py`（本轮新增）按「面板标题已是目标会话 + 有账本 + 无任何载入提示」计时，并单独记录面板是否空过。验收：命中缓存的切换 `first_content_painted_ms < 50 ms`，且全局 `.pending` 从未出现。
   - 注意 `switch_timing.py` 的计时条件要求面板先变空，命中缓存的切换**永远不会变空**，所以它对这类切换只报 `null` —— 这是正确行为，不是失败。
2. **后端计数**：`_probe/detail_breakdown.py`（`before_cursor_execute` 计数 + 4 轮取最小）。验收：`conversations.get` 与 `messages` 各 1 次 checkout；`identity.get` 命中时 0 查询。
3. **端到端**：`_probe/switch_ux.py` 截图比对，确认「正在载入会话」与账本都正确。
4. **回归**：§7 列出的全量命令必须全绿。

注意（第一轮踩过的坑）：新浏览器点「进入演示」后侧栏为空（`startSession` 不调 `restoreView`，§8），脚本里需要在点击后 `page.reload()` 让 `bootstrap()` 走通，才能拿到 30 行会话列表。

### 6.1 实测结果（B + C 全部落地、服务重启加载新代码后）

| 观测点 | 修复前 | 第一轮（并行化） | 本轮（B + C） | 手段 |
| --- | --- | --- | --- | --- |
| 面板不可用 `pane_settled_ms` | 1235 ms | 730 ms | **204 ms**（冷）/ **29 ms**（命中缓存） | `_probe/switch_timing.py`、`_probe/switch_cached.py` |
| 全局「处理中」 | 每次切换都出现 | 不再出现 | 不再出现 | Playwright |
| 命中缓存时面板是否空过 | 空 | 空 | **不空**（同步填充） | `_probe/switch_cached.py` |
| `GET /conversations/{id}` | 730 ms | 730 ms | **~330 ms** | 直连 `127.0.0.1:8000` 计时 |
| `GET /conversations/{id}/messages?limit=50` | 517 ms | 517 ms | **~155 ms** | 同上 |
| 两个读串行合计 | 1250 ms | 1250 ms | ~490 ms | 同上 |
| 两个读并行 | 725 ms | 725 ms | **~350 ms** | 同上 |

命中缓存的切换实测 28 / 30 ms，满足 §6 的 `< 50 ms` 验收；`global_pending_ever_seen = false`。

第二轮（m01065）复测：悬停预取已取回账本、详情仍在途时点击，**点击后 5 ms 面板已有 4 条 turn**、无任何载入提示（`_probe/hover_click.py`）；对照组「悬停后立刻点击」仍要 240 ms。冷切换 `title_ms 52 / first_ms 223 / settled 225`，全程无「处理中」（`_probe/switch_race.py`）。

新浏览器进入演示（§8 的修复）：点一下「进入演示」→ 930 ms 后侧栏 30 行、面板已打开首个会话，全程无刷新（`_probe/demo_entry.py`）。

## 7. 决策记录

| # | 决策 | 依据 |
| --- | --- | --- |
| D-1 | 不做本机 PostgreSQL | 用户 m00274：「将来上线肯定不用本机啊，还是用阿里云不能动」 |
| D-2 | 关闭 `pool_pre_ping` | 用户 m00299：「关」；代价与缓解见 §4.3 |
| D-3 | 缓存只放内存、不落 localStorage | 账本含资金相关事实，不跨页面会话留存 |
| D-4 | 缓存命中后仍复核（而非纯 TTL 缓存） | 后端有后台工作（自动标题、在途回合收尾），纯 TTL 会显示陈旧账本 |
| D-5 | 同时修 `loadHistory` 的无条件覆盖 | 它与 `2026-09-27-conversation-persistence-spec.md:177` 的不变量冲突，且被缓存放大（§3.4） |
| D-6 | 后点的切换取代先点的（不再用 `locked` 守卫） | 用户 m01065 的「全部会话不可选择」就是这个守卫把点击吞掉；行没有禁用样式，静默丢弃无法解释 |
| D-7 | 在途读按 key 复用（`inflight.share`），settle 后即弃 | 悬停预取与点击各发一份同样的请求，一次切换 4 条；只去重、不缓存结果，缓存职责仍归 `conversationCache` |
| D-8 | 缓存允许「半份」 | 账本能单独铺满面板、详情能单独设确认问题；等两半齐全会让点击白等一个详情往返（实测 5 ms vs 240 ms） |

## 8. 范围外

- ~~**`startSession` 不调 `restoreView`**（原 `frontend/src/App.vue:836-838`）：新浏览器点「进入演示」后拿到 session 但侧栏始终为空，必须刷新才出现会话列表；`bootstrap()`（`840-848`）路径正常。~~ **已修**（用户 m00674：「这个也改了」）：`startSession` 拿到 session 后同样 `await restoreView()`，两条入口行为一致。用例 `fills the sidebar as soon as the demo session starts, without a reload`；Playwright 实测新浏览器点一下 930 ms 出 30 行、面板同步打开首个会话、无需刷新（`_probe/demo_entry.py`）。
- 会话列表接口（`GET /api/conversations`，383 ms）本身未优化；它只在打开页面和列表刷新时调用，不在切换关键路径上。
- 不新增/合并 HTTP 端点。B 与 C 完成后首次进入已约 300 ms，合并端点的收益不足以抵消接口变更成本。

## 9. 测试清单（TDD，先红后绿）

前端（`frontend/src/App.test.ts`，复用既有 `FakeServer`/`holdRequests` 装置）：

- T-1 命中缓存的第二次进入同步渲染（拦住该会话全部请求仍能出内容）— R-1
- T-2 命中后仍发出复核请求，且晚到响应只在仍是当前会话时生效 — R-2
- T-3 回合结束后缓存作废，切回看到的是新账本 — R-3
- T-4 归档/改名后缓存里的元数据同步更新 — R-4
- T-5 悬停预热后切换立即出内容；预取不改变 `current` — R-5
- T-6 慢响应晚于切换回来时不覆盖新会话（`loadDetail` 守卫）— §3.4 守卫（对应 R-9）
- T-12 新浏览器点「进入演示」后侧栏立即出现会话列表，不需要刷新 — 修 `startSession` 不调 `restoreView`（§8）。用例 `fills the sidebar as soon as the demo session starts, without a reload`；测试装置新增 `FakeServer.hasSession`，为 false 时 `GET /api/session` 回 401 `SESSION_REQUIRED`（真实浏览器没有 cookie 的样子），`POST /api/session` 成功后置为 true。
- T-6b 慢的**账本**读晚于切换回来时不覆盖新会话（`loadHistory` 守卫）— §3.4 守卫（对应 R-9）。用例 `does not let a slow ledger from the conversation the page left paint over the one now open`：先制造一次 `INVALID_INTERRUPT_ANSWER`，让 `showError` 发出那次**不被 await** 的后台 `refreshHistory()`（这是唯一能在 `busy` 释放后仍在飞的账本读），再切走，最后放行该响应。
- T-13 点第二个会话时第一个会话的读仍在途 → 面板停在**第二个**（后点的取代先点的）— D-6。用例 `opens the conversation clicked while another one is still on its way`
- T-14 悬停预取在途时点击同一行 → 请求数不增加 — D-7。用例 `does not read the same conversation twice when the pointer got there first`；单测 `frontend/src/inflight.test.ts`（5 例：同 key 复用、不同 key 独立、settle 后重跑、失败不缓存、失败传给所有等待者）
- T-15 预取只取回账本、详情仍在途时点击 → 立刻显示账本、不显示载入提示 — D-8。用例 `shows the half a read-ahead has already brought back`；单测 `frontend/src/conversationCache.test.ts` 的三条半份用例
- 既有 3 条切换用例（`describe('conversation switching')`）保持绿 — R-9
- 变异校验：对 B 的 6 处接线逐一注入缺陷，5 处被既有用例捕获；`loadHistory` 的守卫最初**没有被任何用例捕获**，T-6b 就是为此补的，补后该变异被且仅被 T-6b 捕获。

后端：

- T-7 `conversations.get` 单 session（`before_cursor_execute` 计数）— R-6
- T-8 `conversations.messages` 单 session — R-6
- T-9 identity 命中不发查询 — R-7
- T-10 过期 session 仍 401 `SESSION_REQUIRED`（缓存不得绕过）— R-7
- T-11 未知 session 仍 401 — R-7

全量回归：

```
cd backend && uv run pytest -q
cd backend && uv run ruff check app tests scripts
cd backend && uv run ruff format --check app tests scripts
cd frontend && npm test
cd frontend && npm run build
```

### 9.1 基线校准（本轮实测，2026-10-13）

上位文档 `2026-09-27-conversation-persistence-spec.md:162-171` 记的后端基线（577 passed, 2 skipped）**对这个 HEAD 已经过时**，据此无法判断本轮是否引入回归。实测：

| 项 | 上位文档记的 | 本 HEAD 实测 | 出处 |
| --- | --- | --- | --- |
| 后端用例总数 | 577 passed, 2 skipped | `uv run pytest --collect-only -q` = **1567 tests** | 本轮 |
| 最近一次全量结果 | — | `852 passed, 2 skipped, 2 errors in 3706.83s (1:01:46)`（2026-10-07，改动前） | `test_reports_20261007/logs/pytest_backend.log` |
| `ruff check app tests scripts` | All checks passed! | **47 errors**，全部在 `app/modules/transfer/**` 与 `tests/transfer/**` | 本轮 |
| `ruff format --check app tests scripts` | — | **13 files would be reformatted**，同样只在 transfer | 本轮 |
| 前端用例 | 101 passed (5 files) | **180 passed, 1 skipped (9 files)** | 本轮 |

两点结论：

- 全量后端回归在公网 RDS 上是**按小时计**的（每个用例自建 `test_runtime_*` schema 并跑一次 alembic upgrade，`backend/tests/conftest.py:112-129`），所以 1567 个用例的耗时约 1.5–2 小时。
- 仓库级 ruff 在 HEAD 上本来就是红的，且红的文件（transfer 模块）不在 `git diff --name-only HEAD` 里，与本轮无关；本轮改动的 4 个文件 `ruff check` 与 `ruff format --check` 均干净。

另：两个 pytest 会话**不能并发**——`drop_stale_test_schemas`（`backend/tests/conftest.py:72-103`）无年龄过滤，会删掉另一个会话正在用的全部 `test_runtime_%` schema，两边都会报 `UndefinedTable`/`InvalidSchemaName` 的 fixture 错误。
