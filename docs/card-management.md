# 卡片管理

卡片模块已通过 `BankClient` 接入独立模拟银行，沿用现有 Supervisor、工具网关、会话账本与 LangGraph checkpoint。其他五个领域的接入状态保持不变。

## 功能与权限

| 操作 | 工具 | 代码判定的权限 |
| --- | --- | --- |
| 查询所有卡片、指定卡片、交易限制 | `card_cards` / `card_detail` / `card_restrictions` | 查询直接执行 |
| 申请借记卡、信用卡、虚拟卡 | `card_apply` | 黄色：用户确认 |
| 修改日/月交易限额 | `card_update_limit` | 调低为黄色；任一项调高为红色 |
| 添加、移除、覆盖商户黑名单 | `card_update_restrictions` | 增加限制为黄色；移除已有限制为红色 |
| 临时冻结 | `card_freeze` | 黄色 |
| 修改银行卡交易密码 | `card_change_password` | 红色：明确确认、旧交易密码和模拟验证码 |
| 解冻、挂失、解除挂失 | `card_unfreeze` / `card_report_loss` / `card_release_loss` | 红色：确认并通过模拟验证码 |
| 执行/取消已准备的操作 | `card_execute` / `card_cancel` | 只由模块代码调用，不进入模型工具菜单 |

用户输入限额以人民币元的十进制字符串传入，由代码精确转换为整数分，最多两位小数、单项最多一百万元。只修改日限额时保留月限额，反之亦然；日限额不得高于月限额，零作为禁止额度配置。多卡歧义由实际查询结果生成选卡表单，禁止模型使用内部 card_id、account_id 或选择其他客户。

`frozen + freeze_reason=lost` 表示挂失，普通解冻不能恢复，必须解除挂失；历史 frozen 卡按普通冻结处理。blocked 是既有永久阻断状态，不能解冻或修改配置。

## 确认与恢复

1. 模型只解析意图和补全必要信息，调用准备工具。
2. 银行持久化不可变操作参数、原始卡片快照和风险级别；此时不修改卡片。
3. 页面展示选定卡片及限额/商户名单的前后变化（`kind=confirmation`，`pending_id` = 银行 `action_id`；红色操作 `requires_code=true`）。
4. 用户只通过唯一确认入口提交：`POST /api/conversations/{conversation_id}/pending-actions/{pending_id}/decide`（`pending_id` 即 `action_id`）。同一请求内 Agent 先按登录会话校验 question_id / pending_id，再用可信客户绑定调用银行 `authorize`，然后 `Command(resume=...)` 恢复图并执行。
5. 旧的 `POST .../card-actions/{action_id}/authorize` 已退役，返回 **410 Gone**；页面不得再单独授权后再发消息恢复。

银行侧接口仍为 `POST /api/cards/actions`（必须传 Idempotency-Key）、`POST /api/cards/actions/{action_id}/authorize` 和 `POST /api/cards/actions/{action_id}/execute`。具体字段见模拟银行 API 文档。准备请求重试复用幂等键，执行使用不可变 action_id 并锁定记录；重复提交不会重复发卡或重复变更。

授权有效期为真实 UTC 时间十分钟，不受模拟时钟影响。卡片在准备后发生变化时拒绝执行，需要重新办理。验证码错误三次取消该操作；默认演示码为 `123456`，可配置银行服务的 `MOCK_MFA_CODE`。这是模拟验证，不连接短信或真实银行 MFA。错误验证码在 decide 上以 `VERIFICATION_FAILED`（422）拒绝，不消耗账本序号，确认卡保持打开。

刷新页面从 checkpoint 恢复当前确认卡。若执行中断后 checkpoint 无法证明可安全恢复，沿用底座的 `EXECUTION_RECOVERY_REQUIRED`，不自动重放不确定的执行。

验证码不会进入模型提示词、工具参数、聊天账本、checkpoint、localStorage 或审计；它只出现在 decide 请求体并交给银行 authorize。卡片内部 ID 仅存在于银行请求/响应和服务端工具 facts，页面和模型收到脱敏卡类型、尾号、实际配置和公开操作编号。

## 数据声明：card

- 场景：确认前准备、独立用户验证、跨请求恢复和幂等执行。
- 唯一所有者与权威来源：`bank_backend`；Agent 不直连银行业务表。遵循[数据归属契约](superpowers/specs/2026-09-23-module-data-ownership-contract.md)。
- 数据实体：`cards.freeze_reason` 与 `card_actions`。详细字段、约束和索引见[银行数据库设计](../bank_backend/数据库设计文档.md)。
- 持久化原因：银行必须独立判断授权与执行状态，不能信任模型或浏览器传入的确认标记。记录含客户标识、卡片快照和配置等金融信息，不含验证码。
- 读写者：卡片业务服务与既有审计服务；Agent 经 HTTP 使用。取消只更新操作状态，不改卡片；过期禁止授权/执行，仍允许取消。
- 事务：准备用 `(user_id, idempotency_key)` 唯一键与事务锁；授权/执行用行锁。卡片/订单、完成状态、执行结果和成功审计原子提交。失败执行回滚后单独留审计。
- 保留：当前演示记录保留至显式 seed/reset；未实现定期清理或单用户数据删除接口。seed/reset 按外键顺序同时清理操作记录。
- Agent checkpoint：只保留公开 action_id、脱敏问题、用户选择和必要解析参数。银行仍是执行状态的权威来源。
- 迁移：`0002_card_actions` 增量新增字段与表，保留既有卡片；降级会删除新增字段和操作记录。测试使用随机隔离 schema。

## 最小底座扩展

`DomainDeclaration.graph_builder` 是可选构图入口，默认领域仍使用原来的通用子图；只有具有已注册工具的卡片模块启用自己的流程。卡片确认与理财共用 `kind=confirmation` 的 11 键 payload（`pending_id` / `requires_code` 等）；`permission_level` 可作为 confirmation 扩展字段保留，前端以 `requires_code` 为准。没有新增 SSE 事件类型，也没有把确认或验证码参数加入公共 ToolCall。

## 启动与演示

已有安装环境先升级银行表结构，再重启 Agent 和银行进程：

```powershell
cd bank_backend
.venv\Scripts\python.exe -m alembic upgrade head
```

卡片与理财两条迁移通过 `0003_card_wealth_merge` 合并为单个 head。已有环境无论停在 `0002_card_actions` 还是 `0002_wealth_support`，均使用上述命令升级。早期兼容脚本已追加卡片结构但保留理财版本号的环境，也可直接升级；迁移会识别完整的已有卡片结构，不会重复建表。结构仅完成一部分时会拒绝升级并要求检查。

若环境包含仓库仍未收录的其他外部迁移，兼容脚本只追加卡片表和字段，保留原迁移编号与现有数据，且可重复执行：

```powershell
.venv\Scripts\python.exe scripts\migrate_cards.py
```

未知外部迁移仍需补回对应文件后统一合并；已知卡片和理财分支会正常升级到合并 head。隔离测试覆盖两条已部署分支、早期兼容部署和未知外部版本。银行客户端对 localhost/127.0.0.1/::1 的调用绕过系统代理，其他银行地址仍遵循原代理配置。

启动三个服务后，在页面设置模型，再尝试：

- “查看我的银行卡和交易限额。”
- “申请一张虚拟卡。”
- “把尾号 8801 的卡日交易限额调到 1000 元。”
- “把测试商户加入尾号 8801 的卡商户黑名单。”
- “我的借记卡丢了，帮我挂失。” → 选卡 → 确认 → 演示验证码。
- “我找回了刚才挂失的卡，帮我解除挂失。”

额度调整目前指日/月交易限额，没有信用授信额度或真实发卡审批。商户黑名单和限额保存为卡片配置，既有模拟转账/订阅资金路径不会执行卡片配置限制。原始 `/cards/{id}/limit` 等接口仍保留供内部 Mock 演示，不能作为面向外部的授权 API；Agent 只开放新的确认流程。

验证覆盖生产模块、真实 HTTP 客户端到银行的八条场景、越权和伪造恢复、超时重试、并发/重复提交、错误验证码、授权过期、快照变化、前端恢复和验证码隔离。

## 银行卡交易密码修改（2026-10-28）

本功能明确覆盖旧版“不做密码”的范围限制，只管理模拟银行卡的六位数字交易密码，不管理登录密码、忘记密码或找回密码。

用户输入“修改尾号 8801 的银行卡密码”，模型只调用 `card_change_password` 并给出卡片选择器；多卡歧义沿用银行实际查询生成的选卡表单。准备操作仅保存卡片引用和公开操作号，不接收密码。确认卡显示独立密码表单，填写旧密码、新密码、确认新密码和模拟验证码。密码只随现有 `pending-actions/{pending_id}/decide` 的 `transaction_password` 字段发送，服务端校验身份与当前待办后，直接调用银行独立接口 `POST /api/cards/actions/{action_id}/password/authorize`，不把密码放入 turn、消息、工具参数或 checkpoint。

三项密码均为六位 ASCII 数字，新密码必须与确认密码一致且不同于旧密码。旧密码、验证码连续错误均有按卡片保存的服务端限次；重新准备操作不会清零，三次失败锁定改密验证十五分钟。格式不合规、验证失败、取消、过期或准备后密码已变化时都不会修改密码。成功执行只更新一次并显示“银行卡交易密码修改成功”。前端刷新、切换会话及提交后均清空密码输入，待办从 checkpoint 恢复；密码不进入 localStorage 或普通聊天恢复请求。

银行仅保存随机盐 scrypt 哈希；新哈希只在银行私有字段暂存，不进入卡片查询、工具 facts、SSE、快照、账本或审计。密码版本在授权和执行时分别复核，防止并发操作覆盖已更新的密码；取消、执行与过期访问清理暂存哈希。相关字段与事务边界见[数据库设计](../bank_backend/数据库设计文档.md)。公共接口只增加可选敏感表单字段，其他业务领域的确认调用不变。

### 初始化与演示

在银行服务的环境变量或 `bank_backend/.env` 设置 `DEMO_CARD_PASSWORD`（演示默认 `246810`）与 `MOCK_MFA_CODE`（默认 `123456`），然后运行：

```powershell
cd bank_backend
.venv\Scripts\python.exe -m alembic upgrade head
```

新的 head 为 `0004_card_transaction_password`。迁移保留现有卡、账户、余额、订单和审计，只初始化现有卡尚不存在的密码凭据；无需 seed/reset。新申请卡必须通过独立确认表单设置初始交易密码，只保存用户自选密码的独立随机盐哈希；不会使用演示默认密码。以后修改 `DEMO_CARD_PASSWORD` 不会重置已经初始化或已经修改的卡片密码。

重启对应服务，配置真实模型后输入“修改尾号 8801 的银行卡密码”，在独立表单填写配置的旧交易密码、不同的新六位密码与验证码后确认。再次办理时旧密码应验证失败，新密码应验证通过；可在验证成功前取消后续操作。测试中的 `ScriptedModel` 只用于验证编排、HTTP、鉴权和数据持久化，不能视作真实模型的自然语言验收。

### 本机接入排查

代码更新后用仓库根 `start.ps1 -Restart -SkipMigrate` 重启三个进程，保留现有数据。确认前端 5173、Agent 8000、银行 8001 均来自当前最终工程；银行 OpenAPI 必须包含 `/api/cards/actions/{action_id}/password/authorize`。页面恢复模型配置后，新建会话验证，历史“不支持”答复不能覆盖当前注册能力。

普通启动遇到当前源码不认识的团队迁移版本时，会只读检查现有运行所需的表和字段。结构完整才保留该版本、跳过 Agent 的旧迁移和种子；缺少结构仍拒绝启动，并列出缺项。它不会 stamp、回退或伪造其他模块的迁移记录，也不会自动补表或字段。团队新增迁移文件仍应由所属模块正常合并。

尾号 8801 示例要求当前演示账号的服务端 `bank_customer_id` 已绑定该卡持有人。其他客户必须拒绝访问，不能通过前端参数或模型临时更换客户。模拟初始密码只对首次初始化有效，不能用修改配置、seed 或重置数据库来恢复已修改的密码。


## 申请新卡时设置交易密码

输入“申请一张虚拟卡”，仍由现有 `card_apply` 准备操作。申请确认页提供“设置交易密码”“确认交易密码”两项独立密码输入，不要求旧密码。两项必须是相同的六位 ASCII 数字；前端和银行都会校验，缺失、格式错误或不一致不会发卡。沿用申请原有风险分级、明确确认及所需验证码，不把申请改成另一套工作流。

密码只随非聊天的 `pending-actions/{pending_id}/decide.transaction_password` 提交；其中只包含 `new_password`、`confirm_password`。服务端从登录身份绑定取得银行客户，再直接调用既有 `/cards/actions/{action_id}/password/authorize`，密码不会进入模型、工具参数、通用请求指纹或聊天账本。

银行复用 `CardAction.pending_password_hash` 私有字段暂存随机盐 scrypt 哈希，授权后执行申请时，将密码、卡片和申请订单在同一事务内写入。执行前没有暂存哈希会拒绝发卡；完成、取消或过期访问会清理暂存。同一申请并发或重复执行只创建一张卡和一个订单，已完成操作不能更换新卡密码。原 `/cards/apply` 直接申请入口及 `CardService.apply` 已拒绝发卡，防止绕过密码表单。

刷新恢复待办但清空密码；同一待办的后台重复更新保留正在输入的内容，切换会话、更换待办或提交后清空。演示种子卡仍可使用 `DEMO_CARD_PASSWORD` 初始化，已有卡密码保持不变；本功能复用现有私有字段，无新增迁移，不运行 seed/reset 或修改其他模块的迁移记录。

验收时选择一个不同于演示默认值的密码完成虚拟卡申请，再对新卡发起改密：演示默认密码应被拒绝，用户设置的初始密码应能通过旧密码验证。真实模型验收与 `ScriptedModel` 回归分别记录；测试密码与哈希不写入报告。
