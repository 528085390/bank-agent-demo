# Spec 7 — 会话并行：处理中可切换、多会话同时运行

- 状态：**设计已确认，未实现**（本文只定设计。用户 m00109：「写spec」）
- 日期：2026-10-13
- 基线：`D:\ai_project\agent` @ `7ef8364`（dev），行号基于**工作树**（前端自 `5a9258d` 之后未再改动）
- 上位文档：
  - `docs/superpowers/specs/2026-10-13-conversation-switch-latency-spec.md`（Spec 5：会话切换延迟。**本文覆盖其 §3.7 与 §8 里「回复进行中仍不允许切换」的那条决定**，其余机制照旧沿用）
  - `docs/superpowers/specs/2026-09-27-conversation-persistence-spec.md`（会话持久化：账本是持久事实，checkpoint 是唯一执行状态来源）
- 用户原话：
  - m00002：「前端页面有个问题，当前会话在处理中的时候不能切换会话，要做成能切换会话的，并且要多会话可并行」
  - m00109：「写spec」
- 本轮 brainstorm 确认的四个口径（用户逐条选择）：
  1. **并行范围**：同一条会话仍串行。A 在跑时可以切到 B、可以在 B 发消息；**不能**给 A 再发一条（不排队）。
  2. **侧栏状态**：显示「运行中」与「跑完/停在确认卡但你没看过」。
  3. **侧栏写操作**：除「归档/删除正在跑的会话」外全部放开。
  4. **切回后台会话**：保留完整过程，直到你切回去看过；再切走切回才收敛成干净账本。

## 0. 结论

### 0.1 症状与根因

现象：A 会话正在流式回复时，侧栏点任何别的会话都**点不动**，整块面板被「处理中」接管。

根因是三处**页面级**（而非会话级）的状态与门禁：

1. `frontend/src/App.vue:52` 的 `busy` 是**全局** ref，注释就写着「页面级操作：发送、归档、删除。它显示全局「处理中」遮罩，期间别的都不能开始」；`App.vue:120` `locked = computed(() => busy.value || switching.value)`。
2. `App.vue:238-243` `action(work)` 把**所有**写操作（发送、归档、删除、新建、启动会话、设置保存）套在同一层里；发送消息因此把整页锁死，遮罩是 `App.vue:1184` 的 `<div v-if="busy" class="pending">处理中</div>`。
3. `App.vue:372-384` `switchConversation` 开头 `if (busy.value || ...) return`；侧栏每一行按钮都是 `:disabled="locked"`（`App.vue:1065-1069`、`1072`），行本身是普通 `div`、没有任何禁用样式 —— 于是看起来就是「全部会话不可选择」。

另有两处**单例**在并行时会直接出错：

4. `frontend/src/conversations.ts:17` `PENDING_SUBMISSION_KEY = 'bank-agent.pending-submission'` 是**一个槽**。两条未完成提交会互相覆盖，破坏「原样重放」（`conversations.ts:190-212` `runSubmission` 清的就是这个槽）。
5. `App.vue:107` `live` 是**一个** `LiveTurn`：后台会话的流式内容无处安放。

### 0.2 后端不用改（已核查）

跨会话并行**已经**是后端的既有语义，本次不需要任何后端改动：

- 锁按会话分：`backend/app/agents/runtime.py:177` `self._locks: dict[str, asyncio.Lock] = {}`；`:239-246` `_lock_for(session_id)`；`:248-257` `conversation_lock(conversation_id)` 返回 `self._lock_for(f"conversation:{conversation_id}")`。
- 回合准入按会话分：`backend/app/conversation/turns.py:164-169` `acquire(conversation_id)` 取该会话的锁，返回 `HeldLock`（`turns.py:80-97`，release 幂等）；`backend/app/api/routes.py:375-429` `_turn_response` 全程持有到流结束，`finally` 里 `held.release()`。
- 进程里没有全局信号量（`Semaphore` 全仓库 0 处）。唯一的进程级锁是 LangGraph 自带的 checkpoint 写锁（`backend/app/storage/checkpointer.py:26-44`，`self.lock` 在 `:44`），临界区只包住一次 checkpoint 写入，不覆盖模型调用。
- 会话状态按 thread 隔离：`backend/app/agents/runtime.py:86-108` `TurnContext` 的 `thread_id` 是**会话自己存下来的事实**，理财 agent 的槽位存在 graph state（`wealth_slots`）里，不共享进程内存。
- 连接池默认 5+10（`backend/app/storage/database.py:12-17`），两条并行回合各自短暂 checkout，不构成瓶颈。

### 0.3 本次要做的

把「当前会话」从**一组全局 ref** 改成**一个会话一份**，于是：

- 全局 `busy` 与页面级遮罩消失，切换在任何时候都立刻生效；
- 每个会话一条独立的在途链，N 个会话同时跑各自的回合；
- 同会话仍串行（前端拒绝第二次发送）；
- 后台回合结束/停在确认卡时，侧栏出「需要你看」的点；切回去能看到完整过程；
- 待重放记录从单槽改成按会话。

### 0.4 明确不做

- **不加取消/停止按钮，不引入 `AbortController`**。切走不打断在途回合（`api.ts:250-282` `readEventStream` 本来就没有 signal 参数，本次也不加）。
- **不改后端任何代码**（见 §0.2）。
- 不做同会话排队、不做未读计数/声音/标题闪烁、不因为「在跑」把会话排到侧栏顶部。

## 1. 需求

| # | 需求 | 验收 |
| --- | --- | --- |
| R-1 | 有回合在流式进行时，点击别的会话必须立刻切过去 | 新增前端用例：A 的流未结束时点 B，B 的面板立刻可用；全程不出现全局「处理中」 |
| R-2 | 切走不得打断在途回合，回合永远留在它自己的会话上 | A 的流在切到 B 之后继续收事件；切回 A 能看到它已经流到哪 |
| R-3 | 不同会话的回合可同时进行，且互不串台 | 新增用例：A 在跑时在 B 发消息，两条流各自渲染到各自面板；A 的事件不落到 B 上 |
| R-4 | 同一条会话保持串行 | A 在跑时它的输入框禁用、`send` 直接拒绝；B 完全不受影响 |
| R-5 | 后台回合结束（或停在确认卡）时侧栏必须提示 | 该行出现「需要你看」的点；切回 A 的面板 = 账本（水印前）+ 完整过程（工具调用/旁白/回复原文） |
| R-6 | 未查看的那一轮不得因为账本刷新而在面板上重复出现 | 用户消息与回复在面板上各只出现一次（§3.4 水印切片） |
| R-7 | 已看过的会话，切走再切回必须收敛成干净账本 | 第二次打开 A 只剩账本：无过程块、无重复 |
| R-8 | 侧栏写操作不再被别的会话挡住 | A 在跑时，B 的置顶/重命名/归档/删除/恢复/新建/加载更多都可用 |
| R-9 | 正在跑的会话不得被归档或删除 | 该行的归档/删除按钮禁用（其余行不受影响） |
| R-10 | 并行的多条未完成提交在刷新后各自原样重放 | 两条记录各占一个槽；刷新后各自用原 key、原 body 重放（`pendingMatches` 语义不变） |
| R-11 | 既有不变量不回归 | Spec 5 的三条 `describe('conversation switching')` 用例仍绿；`loadHistory`/`loadDetail` 的竞态语义仍成立（改为按会话写入后由结构保证）；`npm test` 与 `npm run build` 全绿 |

## 2. 既有事实

### 2.1 前端「当前会话」的全部状态（`frontend/src/App.vue`，工作树行号）

单例 ref：`97 current`、`98 messages`、`99 historyCursor`、`100 historyBusy`、`102 pending`、`104 recoveryRequired`、`105 renamingId`、`106 renameDraft`、`107 live`、`113 replyNotice`、`115 canRetry`、`57 error`、`58 trace`、`59 draft`、`63 answers`、`64 cardVerificationCode`、`65 cardAuthorizationError`、`73 confirmationCode`。

派生：`117 turns = computed(() => historyTurns(messages.value))`（面板内容**只**由它派生）、`118 writable`、`120 locked`、`121-123 composerDisabled`、`124-142 composerHint`、`149 pendingFields`。

生命周期函数：
- `254-263 clearConversationView()`：`messages=[]`、`historyCursor=null`、`live=null`、`replyNotice=null`、`recoveryRequired=false`、`renamingId=''`、`setPending(null)`、`draft=''`。
- `269-273 resetView()`：`clearConversationView()` + `error=''` + `trace=''`。
- `276-283 setPending(card)`：清空 `answers` 并按 `card.fields` 重建。

### 2.2 打开、读取与竞态守卫

- `370 let switchGeneration = 0`；`372-384 switchConversation(conversation)`：`if (busy.value || conversation.conversation_id === current.value?.conversation_id) return`，`++switchGeneration`，`finally` 里只有代次相同才清 `switching`。
- `393-413` 预取：`PREFETCH_DELAY_MS = 120`，`prefetchConversation` 开头 `if (busy.value || ...) return`；`415-431 readAhead` 并发上限 1，只写缓存。
- `434-456 openConversation(conversation)`：`current = conversation`；`resetView()`；`saveLastConversation(id)`；用 `recall(id)` 的**半份**缓存同步铺满；最后 `await Promise.all([loadHistory(), loadDetail()])`。
- `467-470` 在途键：`firstPageKey(id) = 'messages:' + id + ':first'`、`earlierPageKey(id, cursor) = 'messages:' + id + ':after:' + cursor`、`detailKey(id) = 'detail:' + id`。
- `472-487 loadHistory()`：`share(firstPageKey(id), ...)` → `remember(...)` → **`if (current.value?.conversation_id !== conversationId) return`** → 才写 `messages`/`historyCursor`。
- `490-510 loadEarlier()`：走 `action(...)`（全局锁），`mergeHistory` 后写 `messages`，同样带守卫。
- `516-528 loadDetail({syncPending})`：`share(detailKey(id), ...)` → `remember` → 同一守卫 → 写 `current`/`recoveryRequired`/可选 `setPending`。
- `531-533 refreshHistory()`：`loadHistory()` + `loadDetail()`，失败静默。

### 2.3 提交、流式与结算

- `585-613 send(text, context)`：`if (!message || !session.value || busy.value || pending.value) return`；不可写或 `recoveryRequired` 也直接返回；然后 `action(...)` 里（必要时 `createConversation`）`loadPending()` → `pendingMatches` 复用旧 key 或 `newIdempotencyKey()` → `savePending({key, conversation_id, message, client_context?})` → `submitTurn`。
- `637-697 submitTurn(conversationId, body, key, userText)`：`forget(conversationId)` → 建 `LiveTurn` → `live.value = turn` → **`const view = live.value as LiveTurn`**（`App.vue:655-660` 的注释记录了教训：必须经 ref 代理写，否则流式不渲染）→ `replyNotice=null` → `draft=''` → `runSubmissionTurn({submit: onEvent => submitMessage(...), onEvent})`。事件分发：`interrupt`→`setPending`；`reply_delta`→`view.replyDraft += text`；`narration`→`view.activity.push` + 清 `replyDraft`；`final`→`view.reply` + `replyNotice = terminalNotice(...)`；其余→`view.activity.push`；每次都 `scrollToEnd()`。结束后 `canRetry.value = loadPending() !== null`；`refused`/`failed` 抛出；`settled` → `settle(id)`；否则 `view.unconfirmed = true`。
- `704-715 settle(conversationId)`：**若 `current` 仍是它**才 `loadHistory()` + `loadDetail()`；`live.value = null`；`refreshConversations()`；`void pollForAutoTitle(id)`。
- `718-722 repeatPending(id)` / `724-736 retryPending()`：都读**同一个单槽** `loadPending()`。
- `738-760 submitInterrupt()`：确认卡字段 → `send('补充信息：…', resumeFor(card, value))`；`762-789 submitCardAuthorization` 走独立端点。
- `840-898 submitAction(decision)`：确认卡按钮，走 `decidePendingAction`，自己建 `LiveTurn`、自己分发事件、自己 `settle`。
- `211-232 showError(exc)`：写全局 `error`/`trace`；按错误码分流（`SESSION_REQUIRED`→清 session；`CONVERSATION_NOT_WRITABLE`/`NOT_FOUND`→`refreshConversations()`；`INVALID_INTERRUPT_ANSWER`→`refreshHistory()`；`EXECUTION_RECOVERY_REQUIRED`→`recoveryRequired = true`）。
- `1036-1050 restoreView()`：`loadConversationList()` → 取 `loadLastConversation()` 或第一条 → 无目标时 `resetView()` → `openConversation(target)` → `repeatPending(target.conversation_id)` → `canRetry`。

### 2.4 模板里要改的接线

- 错误横幅 `App.vue:1020`（含 `:disabled="locked"` 的「重试发送」）。
- 侧栏：新建 `1026`；行 `1034-1071`（`1036-1040` 是 `@click="switchConversation"` / `@pointerenter`；`1058-1062` 是置顶/归档/删除标记）；行按钮 `1065-1069` 全是 `:disabled="locked"`；加载更多 `1072`。
- 面板：`1084` `switching && !messages.length` 的「正在载入会话…」；`1086` 加载更早；`1088-1101` 账本 turns；`1103-1127` live 过程块；`1129` replyNotice；`1131-1177` 确认卡（按钮 `1166`、`1169`、`1170`、`1174` 用 `busy`/`locked`）；`1179-1182` recovery；`1184` 全局遮罩；`1187` 输入框（`:disabled="composerDisabled"`）。

### 2.5 纯模块

- `frontend/src/conversationCache.ts:18-21`：`MAX_ENTRIES = 20`、`CACHE_TTL_MS = 5 * 60_000`；`24-28 CachedView`；`31-33 CachedHalf`；`50-64 recall`（LRU touch，返回副本，有哪半交还哪半）；`66-81 remember`（合并半个 entry）；`84-90 patch`；`92-94 forget`；`96-98 resetCache`。
- `frontend/src/inflight.ts:12-25`：`share<T>(key, read)` —— 同 key 在途复用，settle 即删，不缓存结果。
- `frontend/src/conversations.ts`：`17 PENDING_SUBMISSION_KEY`；`19 LAST_CONVERSATION_KEY`；`22-26 StorageLike`；`32-37 PendingSubmission{key, conversation_id, message, client_context?}`；`96-104 savePending`（只保留文档化的键）；`107-116 loadPending`；`118-126 clearPending`；`140-144 pendingBody`；`150-159 pendingMatches`；`169-177 TurnResult = settled|unfinished|refused|failed`；`190-212 runSubmission`（`settled`/`refused` 时清记录）；`265-280 historyTurns`（末轮无回复即 `unconfirmed`）；`287-294 mergeHistory`；`300-305 pendingInterruptOf`；`319-322 isWritable`；`353-362 orderConversations`；`364-369 unwritableHint`。
- `frontend/src/api.ts`：`164-176 submitMessage(conversationId, body, key, onEvent)`；`202-218 decidePendingAction(conversationId, pendingId, body, key, onEvent)`；`227-243 readSubmission`（按 `Content-Type` 决定 SSE 还是 JSON receipt）；`250-282 readEventStream`（**无 AbortSignal 参数**）。

### 2.6 测试基线

- 前端：最近一次记录见 `5a9258d` 提交信息 —— `npm test` **215 passed | 1 skipped**，`npm run build`（`vue-tsc --noEmit && vite build`）干净。Spec 5 §9.1 更早一次记录是 180 passed, 1 skipped (9 files)。**实现开始前必须重新实测**：`5a9258d` 之后 `7ef8364` 又动过 `start.ps1`/`database_file.py`，前端未复核。**已于 2026-10-13 实测**：`7ef8364` 工作树上 `npm test` 215 passed | 1 skipped、`npm run build` 干净，与 `5a9258d` 一致。
- 后端：`uv run pytest --collect-only -q` = 1567 tests；最近一次全量 `852 passed, 2 skipped, 2 errors in 3706.83s`（2026-10-07）。**本次不改后端，因此不跑全量后端回归**（在公网 RDS 上按小时计，见 Spec 5 §9.1）；一旦实现中出现后端改动，这条豁免立即失效。

## 3. 设计

### 3.1 新模块 `frontend/src/sessions.ts`

一个会话一份的易变视图状态。纯模块、无 `fetch`、可单测，风格照 `conversationCache.ts`（模块级 Map + `reset`）。

```ts
/** The turn this page is submitting, for one conversation. Moved out of App.vue unchanged,
 *  plus the watermark below. */
export interface LiveTurn {
  user: string
  at: string
  activity: AgentEvent[]
  reply: FinalEvent | null
  replyDraft: string
  unconfirmed: boolean
  /**
   * The message_id of the last ledger message this page had for the conversation when the
   * turn started; null when the ledger was empty. The pane renders the ledger up to here and
   * the live turn after it, so a turn that is already in the ledger is not shown twice.
   */
  watermark: string | null
}

export interface ConversationSession {
  messages: ConversationMessage[]
  historyCursor: string | null
  historyBusy: boolean
  /**
   * The ledger in `messages` has been read from the server at least once. A turn needs this before
   * it can record its watermark: an empty `messages` means "the ledger was empty" only when the
   * ledger has actually been read (§3.4).
   */
  historyLoaded: boolean
  live: LiveTurn | null
  pending: InterruptEvent | null
  recoveryRequired: boolean
  replyNotice: string | null
  error: string
  trace: string
  canRetry: boolean
  answers: Record<string, string | number>
  confirmationCode: string
  cardVerificationCode: string
  cardAuthorizationError: string
  /** A turn is streaming for this conversation right now. */
  running: boolean
  /** The turn ended (or paused on a card) while this conversation was not the pane. */
  unseen: boolean
}

export function sessionOf(id: string): ConversationSession   // 按需创建（reactive Map）
export function peekSession(id: string): ConversationSession | undefined
export function focusSession(id: string | null): void        // 标记面板当前显示的会话；LRU 不淘汰它
export function forgetSession(id: string): void
export function resetSessions(): void                        // 测试用
export const EMPTY_SESSION: ConversationSession              // 无会话打开时的空壳，供 v-model 有合法左值
```

- 实现为 `reactive(new Map<string, ConversationSession>())`；`sessionOf` 在模板渲染之外被调用（`openConversation` / `submitTurn` 里显式建），因此渲染期不会写 Map。
- 上限 `MAX_SESSIONS = 8`，LRU；**永不淘汰 `running`、`unseen`、挂着 `live` 或 `pending` 的，以及面板当前打开的那个**（`focusSession(id)` 由 `openConversation` 更新）—— 淘汰面板自己的会话会让正在填的确认卡、`answers`、重试横幅凭空消失。八条会话全都处于这些状态时**软上限被越过**（宁可多留，不可丢工作）。被淘汰后切回来靠 `conversationCache` 的账本同步铺满（Spec 5 的机制不变）。
- 写值纪律沿用 `App.vue:655-660` 的教训：一律通过 `sessionOf(id)` 返回的代理写（`const view = sessionOf(id); view.live!.activity.push(...)`），绝不写自己捕获的裸对象。

### 3.2 `App.vue` 的接线

- **消失**：`busy`（`52`）、`locked`（`120`）、`action()`（`238-243`）、全局遮罩（`1184`）；`messages`/`historyCursor`/`historyBusy`/`pending`/`recoveryRequired`/`live`/`replyNotice`/`canRetry`/`error`/`trace`/`answers`/`confirmationCode`/`cardVerificationCode`/`cardAuthorizationError` 这些单例 ref 全部移入 session。
- **保留为页面级**：`session`、`connected`、`modelConfigured`、`draft`（今天切走就清空，行为不变；无会话时也能先打字）、`conversations`/`conversationCursor`/`showArchived`/`showDeleted`/`listBusy`、`renamingId`/`renameDraft`、`settings*`、`llm*`、`messageList`、以及新增的 `starting`（进入演示/首次 bootstrap）与 `pageError`/`pageTrace`（不属于任何会话的失败：启动、设置）。
- `const pane = computed<ConversationSession>(() => current.value ? peekSession(current.value.conversation_id) ?? EMPTY_SESSION : EMPTY_SESSION)`；模板里 `live`→`pane.live`、`messages`→`pane.messages`、`answers`→`pane.answers`、`pending`→`pane.pending`、`recoveryRequired`→`pane.recoveryRequired`、`historyCursor`→`pane.historyCursor`、`historyBusy`→`pane.historyBusy`、`replyNotice`→`pane.replyNotice`、`confirmationCode`/`cardVerificationCode`/`cardAuthorizationError`→`pane.*`、`turns`→`paneTurns`（§3.4）。
- 横幅：一个，内容取 `pane.error || pageError`，`canRetry` 取 `pane.canRetry`。
- `switching` 保留（它只管「面板正在载入」），但**不再挡侧栏点击**，并且从 `composerDisabled` 里只保留「当前面板正在载入」这一条作用（见 §3.3 的水印竞态）。

### 3.3 提交、流式与结算（每个会话一条独立的在途链）

- `send(text, context, target?)`：守卫改为 `pane.running || pane.pending || pane.recoveryRequired || !writable || switching`。**不含任何别的会话的状态**。`target` 只在确认卡路径上用（§3.6）：授权请求返回时用户可能已经切到别的会话，这条「确认/取消本次银行卡操作」必须回到它自己的会话，所以传了 `target` 就按 `peekSession(target)` 判守卫、按 `target` 提交，并且不检查 `switching`；**该会话的可写性仍要查**（授权返回前用户可能把它归档了：`conversations` 里那一行的 `isWritable` 为假时不发）。发送成功后写 `sessionOf(id).live`、`running = true`。
- `submitTurn(id, body, key, userText)`：
  1. `forget(id)`（保留 Spec 5 R-3 的「回合开始即作废缓存」）；
  2. 若 `session.historyLoaded === false` 先 `await loadHistory(id)`（水印是相对账本记的；账本还没到就不能把它当成空账本 —— 见 §3.4 的不变量），然后 `watermark = session.messages.length ? session.messages[session.messages.length - 1].message_id : null`（§3.4）；
  3. 建 `LiveTurn`，写进 `sessionOf(id).live`，`running = true`，`replyNotice = null`，`draft = ''`（`draft` 是页面级，只在发送的那条路径上清）；
  4. `onEvent` 全部落到 `sessionOf(id)`（`interrupt`→该会话的 `pending`；`reply_delta`→`replyDraft`；`narration`→`activity` + 清 `replyDraft`；`final`→`reply` + `replyNotice`；其余→`activity`）；只有该会话是当前面板时才 `scrollToEnd()`；
  5. 结束后 `session.canRetry = loadPending(id) !== null`；`settled`→`settle(id)`；`refused`/`failed`→`showError(exc, id)`；`unfinished`→`view.unconfirmed = true`。**失败与未确认同样算「需要你看」**：该会话不是当前面板时一并置 `unseen = true`，侧栏出点。
- `submitAction(decision)` 与 `submitCardAuthorization` 同样改成按会话：目标会话 = `current.value.conversation_id`（确认卡只可能开在当前面板上），`running` 与该会话的按钮门禁一一对应。
- `settle(id)`：
  - 把最新账本与详情读进 **`sessionOf(id)`**（`loadHistory(id)`/`loadDetail(id)` 现在接收 id，不再看 `current`；**竞态守卫整类消失**，因为每个读只写它自己那个会话）并写穿缓存；用一个 `let read = false` 记录这次复核是否成功；
  - `session.running = false`（在 `finally` 里）；
  - 该会话是当前面板 → **复核成功时** `live = null`（收敛成干净账本，与今天 `App.vue:704-715` 的观感一致）；复核失败时保留 `live`，流式答案不因为复核失败而被换成错误；
  - 不是当前面板 → **保留 `live`**，`unseen = true`（侧栏出点）—— **与复核成败无关**：回合确实在后台结束了，侧栏就该出点；
  - `refreshConversations()` + `void pollForAutoTitle(id)`（不变）。
- `openConversation(conversation)`：`current = conversation`；`sessionOf(id)`（确保存在）；`focusSession(id)`（面板当前会话，LRU 不淘汰它）；**不动该会话的 `error`/`trace`**（后台失败必须留到用户切回来时还能看见；它在下一次 `attempt(id, ...)` 开始时清）；`saveLastConversation(id)`；用 `recall(id)` 的半份缓存同步铺满该会话（有哪半铺哪半，含 `detail` 里的确认卡）；`await Promise.all([loadHistory(id), loadDetail(id)])`。
- **打开时不再重置任何别的东西**：`pending`/`recoveryRequired`/`answers`/`messages` 都属于这个会话，切回来就该还在（这正是按会话分片的意义）。`loadDetail` 会按服务端重新同步确认卡。
- 打开一个 `unseen` 的会话：清 `unseen`，**保留 `live`**（用户选的行为，过程块继续显示）。
- 离开一个会话（面板切到别的会话）时，只有它的 `live` 已结束**且已经看过**（`running === false && unseen === false`）**且没有未确认结果**（`live?.unconfirmed !== true`）才丢掉 `live` —— 于是「看过后再切走切回」收敛成干净账本（R-7）；而一个还没看过的后台结果（`unseen === true`）即使被越过也不会丢掉过程。**第三个条件不可省**：流中断的回合（`unfinished`）在账本里没有任何助手回复，`live` 是它唯一的记录（状态表最后一行的「保留」），若按前两个条件丢掉，`结果未确认` 提示与已流出的半截答案会在切走时永久消失（`running` 已被 `finally` 清掉、`unseen` 也不会置位，因为用户当时就在看着它）。
- 读与预取：`loadHistory`/`loadEarlier`/`loadDetail`/`readAhead` 全部带 `conversationId` 参数，写进对应 session（以及缓存）；`prefetchConversation` 删掉 `busy` 守卫。

### 3.4 水印切片（R-6 的关键）

只要一个会话还挂着 `live`，面板渲染的就是「**水印之前的账本** + `live` 过程块」，而不是完整账本：

```ts
const paneTurns = computed(() => {
  const turn = pane.value.live
  if (!turn) return historyTurns(pane.value.messages)
  const cut = turn.watermark
  if (cut === null) return []                       // 回合开始时账本是空的（见下面的不变量）
  const at = pane.value.messages.findIndex(m => m.message_id === cut)
  // 找不到水印（理论不该发生）：宁可重复也不隐藏真实历史
  return historyTurns(at === -1 ? pane.value.messages : pane.value.messages.slice(0, at + 1))
})
```

为什么必须有这一条：后台回合结束后 `settle` 会把**含这一轮**的新账本读进 session；切回来时若同时渲染完整账本与 `live`，用户消息与回复会各出现两遍。水印同时覆盖三种情形：

1. 后台还在跑时切回去（`loadHistory` 已读到落库的 user 消息）；
2. 后台跑完、未查看时切回去（账本里有 user 消息 + 回复）；
3. 前台正在跑、且期间发生过一次账本复核。

配套的竞态约束（两条）：`send` 在当前面板正在载入（`switching`）时拒绝；`submitTurn` 在 `session.historyLoaded === false` 时先读账本再记水印。两条都服务于同一条不变量：**`watermark === null` 严格等价于「回合开始时账本为空」**。少了它们，`restoreView` 对一条从未打开过的会话重放（`messages` 为空只是因为没读过）会记下 `null`，用户随后打开它时 `paneTurns` 返回 `[]`，整本账本被藏起来 —— 那正是「宁可重复也不隐藏真实历史」要避免的失败模式。账本真的为空时 `historyTurns([])` 本来就是 `[]`，所以 `null` 分支不是冗余，而是「账本内容整段属于本回合」的声明。

### 3.5 侧栏状态与门禁

- 每一行新增两个状态位（来自 `sessionOf(item.conversation_id)`，用 `peekSession` 读，不在渲染期创建）：
  - `running` → 行上一个转圈点（复用 `.spin`）；
  - `unseen` → 一个「需要你看」的点，`title` 渲染期派生：`pending` → 「等你确认」；`error || live.unconfirmed` → 「未完成」（失败与未确认同样算「需要你看」，见 §3.3）；否则「已回复」。
- 行按钮（`App.vue:1065-1069`）：`:disabled="locked"` 全部换成
  - 置顶/重命名/恢复：`:disabled="rowBusy(item)"`；
  - **归档/删除**：`:disabled="rowBusy(item) || isRunning(item)"`（R-9）；
  - 新建（`1026`）：`:disabled="starting"`；
  - 加载更多（`1072`）：`:disabled="listBusy"`。
- Set 叫 `busyRows`（页面级 `reactive(new Set<string>())`）：一次元数据写只锁那一行；判定函数叫 `rowBusy(item)`（一个名字不能既是 Set 又是函数）。
- `isRunning(item)` = `peekSession(item.conversation_id)?.running === true`；`isUnseen(item)` = `peekSession(item.conversation_id)?.unseen === true`；`rowBusy(item)` = `busyRows.has(item.conversation_id)`。三者在渲染期只读，绝不创建 session。
- 面板内不再有全局遮罩：该会话在跑时，`composerHint` 显示「本会话正在处理中…」、`composerDisabled` 为真；过程块里已有的 `Agent 处理中` 转圈（`App.vue:1108`）就是运行中的可见信号。提示的优先级沿用 `App.vue:126-129` 的「Writability comes first」：**不可写（归档/删除）> pending 卡 > running > recoveryRequired**，否则「运行中且已归档」会提示成「本会话正在处理中」，而既有用例断言的是「取消归档后才能继续发送」。
- 确认卡按钮（`1166`/`1169`/`1170`/`1174`）的 `busy` 换成 `pane.running`。

### 3.6 待重放记录：单槽 → 按会话（R-10）

`frontend/src/conversations.ts`：

```ts
export const PENDING_SUBMISSION_KEY = 'bank-agent.pending-submission'        // 旧键，只读兼容
export const PENDING_SUBMISSION_PREFIX = 'bank-agent.pending-submission:'    // 新键：前缀 + conversation_id

export function savePending(record: PendingSubmission, storage?: StorageLike): void
export function loadPending(conversationId: string, storage?: StorageLike): PendingSubmission | null
export function clearPending(conversationId: string, storage?: StorageLike): void
export function listPendingConversationIds(storage?: StorageLike): string[]
```

- `loadPending(id)` 先读新键；没有再读旧键，且仅当旧记录的 `conversation_id === id` 时返回它（升级瞬间未完成的请求不丢）。
- `clearPending(id)` 同时删新键与该 id 的旧键。
- `StorageLike`（`conversations.ts:22-26`）需要补上 `length: number` 与 `key(index: number): string | null`，扫描才成立；测试里的假 storage 也要补这两项。
- `listPendingConversationIds()` 扫描 `localStorage` 里所有以 `PENDING_SUBMISSION_PREFIX` 开头的键（`localStorage.key(i)` + `length`），用于 `restoreView` 时判断哪些会话要重放、以及「重试发送」按钮的可见性。注意它**不列** legacy 单槽键（`PENDING_SUBMISSION_KEY` 不带前缀），所以 `restoreView` 不能只遍历它。
- `runSubmission`（`conversations.ts:190-212`）改为 `runSubmission({ submit, onEvent, conversationId, storage })`，内部清的是该会话的槽。语义不变：`settled`/`refused` 清记录，其余保留。
- `repeatPending(id)` 与 `retryPending()` 改成按会话；`restoreView()` 依次重放**该浏览器里所有未完成记录**，而不是只看当前会话那一条：重放集合 = `new Set([target.conversation_id, ...listPendingConversationIds()])`。被恢复的会话必须显式并入，因为 `listPendingConversationIds()` 不列 legacy 单槽键，而升级留下的那条记录只有经 `loadPending(target)` 的 legacy 回退才读得到。

## 4. 决策记录

| # | 决策 | 依据 |
| --- | --- | --- |
| D-1 | 同一条会话保持串行，前端不排队 | 用户选择（问题一）。后端锁本来就是按 `conversation_id` 串行的；排队要新增每会话待发队列，属 YAGNI |
| D-2 | 侧栏显示「运行中」与「未查看」 | 用户选择（问题二）。否则后台跑完或停在确认卡上，用户不会知道 |
| D-3 | 除「归档/删除正在跑的会话」外，侧栏写操作全部放开 | 用户选择（问题三）。后端语义是「归档/软删除不取消在途回合」（`conversations.ts:319-322` 注释），把跑到一半的会话移出列表只会让用户之后要恢复才看得到结果 |
| D-4 | 后台跑完保留完整过程，切回去看过；再切走切回才收敛 | 用户选择（问题四）。过程（工具调用/旁白）不在账本里，`settle` 一读账本就永久丢失 |
| D-5 | 账本放进 session，删掉 `loadHistory`/`loadDetail` 的 `current` 竞态守卫 | 守卫的存在是因为"面板只有一个"；每个会话自己持有账本后，读只会写它自己的会话，整类竞态消失（Spec 5 §3.4 的守卫语义由结构保证，不是被删掉） |
| D-6 | `LiveTurn` 增加 `watermark`，面板按水印切片 | 唯一能同时满足「后台过程可见」与「不重复渲染」的规则；否则 `settle` 后的账本与 `live` 会各画一遍用户消息与回复 |
| D-7 | 页面级 `busy` 与 `action()` 删除，改为每会话 `running` + 每行 `rowBusy` + 页面级 `starting` | 全局门禁就是本 bug 的根因；保留它就无法并行 |
| D-8 | `error`/`trace`/`canRetry` 移入 session | 它们本来就是按会话的语义（`resetView()` 在切走时清 `error`）；并行后后台会话的失败不能污染当前面板 |
| D-9 | `draft` 保持页面级 | 无会话时也要能先打字；今天切走即清空，行为不变。把 draft 放进 session 会在「无当前会话」时失去绑定目标。**已知取舍**：并行会话恰恰鼓励「打一半切去看别的」，而切走会清空草稿；R-2 只要求流与过程状态跨切换保留，草稿不在其中，这里选择行为不变（要改应单独提需求） |
| D-10 | 待重放记录改为按会话，并兼容读旧单槽键 | 单槽在并行下必然互相覆盖；旧键只读兼容让升级瞬间的未完成请求仍能重放 |
| D-11 | 不加 `AbortController` / 停止按钮 | 用户没有要求；切走本来就不该打断回合，加了反而引入新的取消语义要设计 |
| D-12 | 不改后端 | §0.2 已核查：锁按会话分，进程内无全局信号量，会话状态按 thread 隔离 |

## 5. 每会话状态迁移

| 事件 | `running` | `live` | `unseen` | 面板（若它是当前面板） |
| --- | --- | --- | --- | --- |
| 提交一条消息 | true | 新建（带 watermark） | false | 账本（水印前）+ 过程块 |
| 收到 `reply_delta`/`narration`/工具事件 | true | 追加 | 不变 | 同上，继续追加 |
| 收到 `interrupt` | true | 保留 | 不变 | 过程块 + 确认卡 |
| `settle` 且它是当前面板 | false | **置 null** | false | 干净账本（重新读到的） |
| `settle` 且它不是当前面板 | false | 保留 | **true** | —（侧栏出点） |
| 打开一个 `unseen` 会话 | false | 保留 | **false** | 账本（水印前）+ 完整过程 |
| 从它切走（`live` 已结束、已看过、且没有 `unconfirmed`） | false | **置 null** | false | — |
| 越过一个未查看的后台结果（`unseen`） | false | 保留 | true | — |
| 流中断（`unfinished`） | false | 保留，`unconfirmed = true` | 后台时置 true | 未确认提示 + 重试横幅 |
| 刷新页面 | false | 由 `restoreView` 重建 | false | 重放该浏览器里每条未完成记录（含被恢复会话的 legacy 单槽记录） |

## 6. 范围外

- 取消/停止按钮、`AbortController`、流的中断与重连（`api.ts:250-282` 的 `readEventStream` 不动）。
- 同会话排队或同会话并行（需要改后端账本顺序与 checkpoint 语义）。
- 未读计数、声音、浏览器通知、标题闪烁。
- 「在跑的会话排到侧栏顶部」这类排序改动（`orderConversations` 与后端排序键都不动）。
- 后端任何改动；`pool_size`/`max_overflow` 调整（并行回合数在演示场景下远小于 5）。
- Spec 6 记录的 checkpointer 单连接失效问题（另一份 spec，本次不碰）。

## 7. 测试清单（TDD，先红后绿）

前端单测：

- T-1 `frontend/src/sessions.test.ts`（新）：`sessionOf` 复用同一个对象；`resetSessions` 清空；淘汰只淘汰不在跑、无未查看、无 `live`/`pending`、且不是面板当前会话的；`peekSession` 不创建（ghost 读了也不进 Map）；八条会话全都受保护时软上限被越过而不是丢工作。— §3.1
- T-2 `frontend/src/conversations.test.ts`：两条记录各占一槽、互不覆盖；`loadPending(id)` 只读自己那条；旧单槽键在 id 匹配时仍能读到；`clearPending(id)` 只删自己那条；`listPendingConversationIds` 列出全部。— R-10

`frontend/src/App.test.ts`（复用既有 `FakeServer`/`holdRequests` 装置）：

- T-3 A 的流未结束时点 B，面板立刻显示 B，且全程没有全局「处理中」— R-1
- T-4 A 在跑时在 B 发消息，两条流各自渲染到各自面板，A 的事件不落到 B — R-3
- T-5 切回正在跑的 A，过程块继续追加（切走期间收到的事件没有丢）；账本为空的回合在后台结束后，用户消息与回复各只出现一次（`watermark === null` 的语义）— R-2、R-6
- T-6 A 在后台跑完：侧栏 A 行出现未查看标记；切回 A 能看到完整过程 — R-5
- T-7 再切走切回 A：只剩干净账本，且用户消息与回复各只出现一次 — R-6、R-7
- T-8 A 在跑时给 A 发消息被拒（输入框禁用、`send` 返回）— R-4
- T-9 A 在跑时：归档/删除 A 的按钮禁用，而 B 的置顶/重命名/归档/删除可用，新建与加载更多可用 — R-8、R-9
- T-10 两个会话各有一条未完成提交，刷新后各自用原 key 重放；另有升级留下的 legacy 单槽记录属于被恢复的会话时，刷新后也必须用原 key 重放 — R-10
- T-11 后台失败只属于那个会话（B 的面板不被 A 的错误打断，切回 A 看得到）；「重试发送」只出现在欠着请求的那个会话里 — R-11、§3.3
- 既有 `describe('conversation switching')` 全部用例保持绿（17 条，`App.test.ts:452-854`；本分支只新增 import 与追加用例，未改任何既有断言）— R-11
- 变异校验：对水印切片（T-7）、每会话事件落点（T-4）、`running` 门禁（T-8）各注入一次缺陷，确认对应用例变红。

全量回归见 §8。

## 8. 验证命令与基线校准

```text
cd frontend && npm test
cd frontend && npm run build          # vue-tsc --noEmit && vite build
```

- 后端本次无改动 ⇒ 不跑全量后端回归（公网 RDS 上按小时计，见 §2.6）。若实现中出现后端改动，必须补 `cd backend && uv run pytest -q` 与 `ruff check/format`。
- 前端基线必须在实现开始前重新实测。已于 2026-10-13 实测（`7ef8364` 工作树）：`npm test` **215 passed | 1 skipped**，与 `5a9258d` 的记录一致；`npm run build` 干净。实现完成后（`a83a85e`）为 **253 passed | 1 skipped (254)**。
- 浏览器实测建议（沿用 Spec 5 的 `_probe/` 手段，解释器 `D:\ai_project\douyin-auto-fire\.venv\Scripts\python.exe`）：A 在跑时点 B 的切换耗时、并行两轮的端到端完成时间、侧栏状态点在两种状态下的可见性。
