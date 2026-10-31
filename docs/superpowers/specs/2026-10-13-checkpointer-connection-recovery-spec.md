# Spec 6 — Checkpointer 单连接失效：会话详情 500 的根因与修法（待实现）

- 状态：**未实现**（本文只标记问题与修法。用户 m00823 明确「先写个spec文档标记这个问题」，暂不动手；临时处置是重启后端）
- 日期：2026-10-13
- 基线：`D:\ai_project\agent` @ `1fa63ae`（dev），行号基于**工作树**（已含 Spec 5 的 B/C 未提交改动）
- 上位文档：`docs/superpowers/specs/2026-09-27-conversation-persistence-spec.md`（会话持久化：唯一执行状态来源是 LangGraph checkpoint）
- 用户原话：
  - m00823：「checkpointer 那条长连接先写个spec文档标记这个问题，其他的现在修」

## 0. 结论

### 0.1 症状

侧栏能正常列出会话，但打开会话时整页横幅「服务暂时异常，请稍后重试」（HTTP 500）。实测 request_id `req_b0038ea319fc41138325e5d14b56d917`，后端日志 `unhandled_error ... type=OperationalError`。**一旦发生，同一进程内每次读会话详情都 500**，重启后端即恢复。空闲 20–30 分钟后必现。

### 0.2 根因

`backend/app/storage/checkpointer.py:70` 全进程只建立**一条** async 连接：

```python
async with LockedAsyncPostgresSaver.from_conn_string(checkpointer_url(database_url)) as saver:
    saver._rebind_loop_lock()
    yield saver
```

阿里云 RDS 会掐掉长时间空闲的连接，而 `AsyncPostgresSaver` 不会在失败后重建它：它的 `_cursor()` 继续使用这条已关闭的连接，抛 `psycopg.OperationalError: the connection is closed`。异常从 `backend/app/api/routes.py:243` 的 `turns.recovery(...)` 冒泡成 `INTERNAL_ERROR` 500。

实测调用链（logs/backend.err.log:925）：

1. `backend/app/api/routes.py:243` `hint = await runtime(request).turns.recovery(...)`
2. `backend/app/conversation/turns.py:415` → `backend/app/runtime.py:188 recovery_hint`
3. `backend/app/agents/runtime.py:371` `interrupts, unfinished = await self._pending(graph, config)`
4. `backend/app/agents/runtime.py:283` `state = await get_state(config)`
5. `langgraph/checkpoint/postgres/aio.py:402` `async with conn.cursor(binary=True, row_factory=dict_row) as cur:`
6. `psycopg/_connection_base.py:541 _check_connection_ok` → `psycopg.OperationalError: the connection is closed`

对外文案来自 `backend/app/api/sse.py:73` 与 `backend/app/main.py:116` 的 `AppError("INTERNAL_ERROR", "服务暂时异常，请稍后重试", 500, retryable=True)`。

### 0.3 为什么只有会话详情坏

- `GET /conversations`、`GET /conversations/{id}/messages` 只走 SQLAlchemy 池，不碰 checkpointer，所以侧栏和账本一直可用；
- `GET /conversations/{id}` 会调 `turns.recovery()` → `recovery_hint()` → `aget_state`，只有它踩这条坏连接。

区别在**坏连接的处置**：SQLAlchemy 池在 checkout 时换一条（一个请求 500 后自愈），checkpointer 的这条是进程级的、永久坏掉，直到重启。Spec 5 的 C3 关闭 `pool_pre_ping` 之后，池侧零 `OperationalError`，10 个 500 全部落在 checkpointer 这条连接上。

### 0.4 这是既有问题，不是本轮引入

`logs/backend.err.20261012-2318.log:406/407` 与 `:518/519` 是同一条 `the connection is closed`，`:303` 还有 `server closed the connection unexpectedly`（2026-10-12，早于 Spec 5 的改动）。

### 0.5 临时处置（已执行）

备份 `logs/backend.err.log` → `_probe/backend.err.before_restart.log`，只重启后端（前端与 bank 未动），重启后 `/api/session`、列表、详情（383/343/329 ms）、消息全部 200，新日志 0 个 `unhandled_error`。

## 1. 需求

| 编号 | 需求 | 验收 |
| --- | --- | --- |
| R-1 | 空闲掐连接之后，读会话详情不再 500 | 连接被掐后 `aget_state` 仍成功 |
| R-2 | 恢复是自动的，不需要重启进程 | 复现步骤后同一进程继续可用 |
| R-3 | 不改变 checkpoint 的读写语义与表结构 | 既有 checkpoint 用例全绿 |
| R-4 | 启动期的表检查行为不变 | `CHECKPOINTER_NOT_INITIALIZED` 仍在缺表时 503 |

## 2. 既有事实

### 2.1 `backend/app/storage/checkpointer.py`（102 行）

- `CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")`（:18-23），只用于启动自检；
- `class LockedAsyncPostgresSaver(AsyncPostgresSaver)`（:26）的 `_rebind_loop_lock()`（:36-44）：Windows 上 saver 构造时的 `asyncio.Lock` 绑在 Proactor loop，应用跑 Selector loop，首个写入会报 "is bound to a different event loop"，所以重建这个锁。**修法必须保留它**；
- `checkpointer_url(database_url)`（:47-58）：剥掉 `postgresql+psycopg://` 前缀；其它 `postgresql+` 抛 `CHECKPOINTER_DRIVER_UNSUPPORTED`；
- `postgres_checkpointer(database_url)`（:61-72）：`asynccontextmanager`，就是 :70 那条连接；
- `missing_checkpoint_tables()` / `require_checkpoint_tables()`（:75-102）：用一次性 `AsyncConnection.connect` 查 `information_schema`，缺表抛 `CHECKPOINTER_NOT_INITIALIZED` 503。它自己建连接，**不受本问题影响**。

### 2.2 谁在用它

`backend/app/main.py:44-45` 的 lifespan 里 `require_checkpoint_tables(...)` + `postgres_checkpointer(settings.database_url)`，saver 交给 Runtime 持有到进程结束。

### 2.3 复现

1. 启动后端，打开任意会话（成功）；
2. 静置 20–30 分钟（或手工在 psql 里 `SELECT pg_terminate_backend(pid)` 掐掉那条空闲连接）；
3. 再打开会话 → 500，且此后每次都 500。

## 3. 修法（待实现）

### 3.1 设计

把单连接换成 `psycopg_pool.AsyncConnectionPool`，交给 `AsyncPostgresSaver` 使用；池自己负责健康检查与替换：

```python
from psycopg_pool import AsyncConnectionPool

@asynccontextmanager
async def postgres_checkpointer(database_url: str):
    pool = AsyncConnectionPool(
        conninfo=checkpointer_url(database_url),
        min_size=1,
        max_size=4,
        open=False,
        check=AsyncConnectionPool.check_connection,   # checkout 时验活
        max_idle=120,
        max_lifetime=1800,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    )
    await pool.open()
    try:
        saver = LockedAsyncPostgresSaver(pool)
        saver._rebind_loop_lock()
        yield saver
    finally:
        await pool.close()
```

`AsyncPostgresSaver` 的连接参数在 `langgraph-checkpoint-postgres` 里是 `AsyncConnection | AsyncConnectionPool`，`_cursor()` 走 `pool.connection()`；**实现时必须先核对该版本 `__init__` 的签名与 `from_conn_string` 的 kwargs**（`autocommit`/`row_factory`/`prepare_threshold` 这些由 `from_conn_string` 现在的实现提供，改手工构造就要自己补齐）。

### 3.2 为什么是池，而不是"失败就重连"的包装

- 包装需要在每次 `aget_state`/`aput` 前后判活，等于把 LangGraph 的每个调用点都改一遍；
- 池只改一处构造，且把 `max_idle`/`max_lifetime` 交给 RDS 的空闲超时之前；
- `check=AsyncConnectionPool.check_connection` 与 SQLAlchemy 的 `pool_pre_ping` 是同一件事，只是作用在这条连接上。

### 3.3 范围

只动 `backend/app/storage/checkpointer.py`，加一条用例。表结构、`setup()` 时机、`require_checkpoint_tables()` 都不变。

## 4. 决策记录

- D-1：修法选连接池（R-2 要求自动恢复；包装式重连要动所有调用点）。
- D-2：不在启动时预建多条连接：`min_size=1` 就够，演示环境不需要更大。
- D-3：本文只标记不实现（用户 m00823）；在实现之前，线上/演示遇到 500 的处置就是**重启后端**（§0.5）。

## 5. 范围外

- 不引入应用侧的重试/降级：checkpoint 是唯一执行状态来源，读不到就是读不到（上位文档 §1 已冻结这个语义）；
- 不改 `pool_pre_ping`（Spec 5 的 C3 已按用户决定关闭，且它自愈）；
- 不做连接级告警/监控。

## 6. 测试清单

| 编号 | 用例 | 断言 |
| --- | --- | --- |
| T-1 | 连接被掐后仍能读 checkpoint | 从池里取出连接并 `await conn.close()`，随后 `saver.aget_state(config)` 成功 |
| T-2 | 空闲超过 `max_idle` 会被替换 | 用很小的 `max_idle` 起池，两次取到的连接不是同一条 |
| T-3 | 既有行为不变 | `checkpointer_url()` 的前缀处理、缺表 503、Windows 锁重绑的既有用例全绿 |

## 7. 交付物

`backend/app/storage/checkpointer.py`（连接池）、`backend/tests/`（T-1..T-3），以及本文件状态从「未实现」改为「已实现」。
