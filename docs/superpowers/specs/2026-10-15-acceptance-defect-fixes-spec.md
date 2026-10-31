# 验收缺陷修复 spec（C08 / C13 / 审计 / W06 / W01 口径）

- 状态：已评审（决定见 §7），未实现
- 日期：2026-10-15
- 基线：7886ded（dev）；工作树 D:\ai_project\wt-acceptance-fixes，分支 feat/acceptance-defect-fixes
- 上位文档：docs/superpowers/specs/2026-10-13-architecture-optimization-roadmap-spec.md
- 兄弟 spec：docs/superpowers/specs/2026-10-11-wealth-whole-turn-integration-spec.md、docs/superpowers/specs/2026-10-12-wealth-write-verification-spec.md
- 证据来源：D:/ai_project/agent/_accept/REPORT_ui_acceptance_card_wealth_local.md（42 用例：32 通过 / 6 偏离 / 3 失败 / 1 未触发）
- 评审：用户已确认（2026-10-15）——C08 采用选项 B、W01 认可新设计（只改文档）、W06 采用选项 a；决定与被否决的替代方案见 §7
- 复核：2026-10-15 第二轮（独立只读审计子代理 + 逐条回码核验）共 20 条 findings 已并入；本轮另定两件事——理财锁新增银行码 1009（R-13）、C13 根因按「schema 逼模型猜类型」定性并补「写参数落审计」（§0、§2.2、§4.2）
- 复核：2026-10-15 第三轮 spec↔plan 复核（两个独立只读审查子代理 + 逐条回码核验）共 33 条 findings 已并入，并定两件事——理财缺码拆分**只改 HTTP 层**（领域层 bank_backend/app/domain/investment_service.py:660-669 与 bank_backend/tests/domain/test_wealth_write_contract.py:96-97 不动）、1009 覆盖**全部**资金工具锁（含理财）

## §0 结论

先给判断，证据在 §2。

| 项 | 定性 | 一句话根因 |
|---|---|---|
| C08 红色操作连错 3 次验证码后对话侧不感知 | 缺陷（非架构） | 银行第 3 次失败响应已带 status="cancelled"，但 backend/app/agents/hitl.py:23-72 只读 verification_passed，把「已被银行取消」压成了「验证未通过」这个可重试状态 |
| C13 卡 active 时「解冻尾号 3878」答「没有找到符合条件的本人银行卡。」 | 缺陷（schema 逼模型猜类型）+ 一处设计脆弱 | 选择器把 card_type 变成 required 且 enum 只有 debit/credit/virtual、**没有 null**（backend/app/tools/schema.py:54-69 + backend/app/modules/card/tools.py:16-23），模型无法表达「不确定」、只能猜一个；backend/app/modules/card/tools.py:49-58 又把 card_suffix 与 card_type 做 AND，猜错即否决尾号唯一命中的那张卡。backend/app/modules/card/workflow.py:61-72 的 declared_arguments 已丢弃 null/空值、且丢弃发生在校验之前，所以「填 null」不是可用通道——修法必须是「type 不否决」。模型实际猜的是卡型还是尾号**无法证实**：写参数从不落审计（活动流只有 graph 自己的 card_cards {}） |
| W01 理财只读回合出现 tool_call | 口径漂移（实现已改，文档过期） | 提交 03fd141（2026-10-13）显式把只读工具改成经工具网关以原生 tool_call 回传；旧验收口径 docs/superpowers/specs/2026-10-11-wealth-whole-turn-integration-spec.md:411 已无测试支撑 |
| 审计缺口：417 行 llm_call_audit 无一行 tool_calls，agent_id 全 NULL | 缺陷（可观测性） | backend/app/audit/model_client.py:111-114 流式调用只累加 text_delta，:165 的 raw 恒为 None；backend/app/integration/llm_config.py:184-189 构造共享客户端时不传 agent_id |
| W06 错码文案是通用「该操作需要额外确认。请重新输入验证码后再确认。」 | 缺陷（与 C08 同源） | bank_backend/app/core/errors.py:52-57 VerificationFailedError 继承 ConfirmationRequiredError 却不覆盖 code → 线路仍是 1007 → 映射成 CONFIRMATION_REQUIRED 的通用文案 |
| 理财写路径连错 5 次验证码后锁工具 15 分钟，对话侧只见通用文案 | 缺陷（与 C08/W06 同源） | bank_backend/app/domain/security_service.py:24-32（LOCK_THRESHOLD=5 / LOCK_MINUTES=15，:97-114 累加）经 bank_backend/app/domain/investment_service.py:763-786 记账；第 6 次返回的 1005 锁文案被 backend/app/integration/bank_client.py:46 丢弃 → 通用「该业务当前状态不允许此操作。」；本 spec 给它独立码 1009（R-13），require_not_locked 的三个调用点（investment_service.py:199、:451、transfer_service.py:163）一起变 |

修复顺序：P0 C08 → P0 C13 → P1 审计 → P1 W06（含缺码/错码拆分与 1009 锁码）→ P1 W01（文档）。范围边界见 §6，与报告的差异见 §7。

## §1 需求

| # | 需求 | 验收 |
|---|---|---|
| R-1 | 红色操作验证码连错达到上限后，对话侧必须识别为「银行已取消该操作」，而不是「验证失败」 | 桩银行第 3 次失败响应 status="cancelled" 时，该回合不调用 card_execute，也不返回 VERIFICATION_FAILED |
| R-2 | 该回合以取消收尾：子任务行文案固定「本次银行卡操作已取消，未修改银行卡。」，确认卡永久消失，银行侧余额与限额不变 | 回合结果含该文案的子任务行；SSE 无 interrupt 帧；银行桩记录到 authorize(decision="cancel")，无 execute |
| R-2b | 自动取消回合必须留下确定性的取消陈述（子任务行），不依赖模型措辞 | 该回合的 subtask_end 与 domain_results 摘要恰为「本次银行卡操作已取消，未修改银行卡。」，且回合不以 interrupt 结束；最终 assistant 文本由模型生成、**不作逐字断言**（见 §8）；钉在 backend/tests/test_card_auto_cancel.py |
| R-3 | 自动取消不得改变请求指纹：同一提交的重放仍命中幂等记录 | 同 idempotency key + 同 body 二次 POST 返回首次回执（Receipt），不产生第二次模型调用与第二次 authorize |
| R-4 | 已取消 action 的再次点击不得落到通用 INVALID_STATE 文案 | 第 3 次失败那一回合必须走完 resume 并把 pending 消费掉（否则第 4 次点击仍会打到银行）；第 4 次点击以 INVALID_INTERRUPT_ANSWER（409，「确认没有指向当前待确认的操作」）拒绝且**不再调用 /cards/actions/{id}/authorize**；前端只在服务端确认该 question_id 已不再开放时才清卡（不得在 409 后无条件清）；不再出现「该业务当前状态不允许此操作。」 |
| R-5 | 尾号是身份：card_suffix 命中后，card_type 只能收窄歧义集合，永不否决 | 本人尾号 3878 是 virtual，请求 card_type=debit 时仍命中该卡；两卡同尾号不同卡型时才用 card_type 消歧 |
| R-6 | 「没有找到符合条件的本人银行卡。」只在尾号真的不匹配时出现 | 尾号 8801（无此卡）仍得该文案；尾号 3878 且卡型不符不得再得该文案 |
| R-7 | 选择器语义必须写进工具描述，且不得要求模型猜卡型：card_type 仅在尾号歧义时用于消歧 | 生成的 function schema 里**选择器副本**的 card_type 带该 description；card_apply 用的仍是原 SELECTOR（那里 card_type 是「要开的卡类型」，语义不得被改）；断言见 backend/tests/test_card_tools.py |
| R-8 | 流式模型调用必须把 assistant 的 tool_calls 落进 llm_call_audit.response_json | 一次返回工具调用的流式调用后，该行 response_json 含工具名、参数与 finish_reason（非 JSON null）；为此 backend/app/integration/model_client.py 的 StreamChunk 需能带出 tool_call_id / finish_reason |
| R-9 | 每次模型调用必须记录 agent_id：卡域回合记 "card"，调度器回合记 "supervisor" | 卡域回合审计行 agent_id == "card"，调度器回合 == "supervisor"，都不为 NULL |
| R-10 | 银行验证码错误必须有自己的错误码与文案，不得复用 1007 的「待确认」语义；同时 1007 只保留「缺码/需确认」 | 非空错码 → 1008（对话侧文案恰好是「验证码不正确。」）；缺码/空码 → 1007。卡路径在领域层拆（bank_backend/app/domain/card_action_service.py:134-142）；理财路径**只改 HTTP 层**（领域层 investment_service.py:660-669 保持不动，见 D7）；两条路径分别被 bank_backend/tests/api/test_card_actions.py:90（for code in (None, "654321")）与 bank_backend/tests/api/test_investment_routes.py 钉住（例外：同 Idempotency-Key 的重放不算缺码 —— 即使重试时漏了 mfa_code，仍返回原回执 `replayed: true`；见 §4.4 第 2 条与 commit 751e632） |
| R-11 | R-10 之后理财验证码纠错路径仍然有效 | backend/app/modules/wealth/infrastructure/bank_adapter.py:366 的 verification_failed 判定随新 kind 同步；理财错码仍进入纠错重问 |
| R-12 | 文档口径与实现一致：只读回合允许出现 tool_call/tool_result 帧；以 interrupt 结束的回合**也会**发出 subtask_end（turn_status=waiting_input、摘要即那句追问，事件层 status 仍是冻结词表里的 SUCCEEDED）；并写明处置口径——承载查询结果的 turn_status=completed 子任务行属于「用户答完追问后的完成轮」，追问轮以提问卡结束是方案要求的行为 | 过期段落被改写并标注提交依据（03fd141）；W01 的判定口径按完成轮表述；backend/tests/wealth/test_read_flow_has_no_pause.py 与 tests/test_wealth_graph.py:145-147 保持通过 |
| R-13 | 资金工具锁（含理财写路径）必须有自己的错误码与文案 | 银行第 6 次尝试返回 1009（security_service.require_not_locked 对所有资金工具一致，理财写路径同样命中），对话侧文案恰为「该资金工具已锁定，请稍后重试。」；理财纠错摘要不再出现「该业务当前状态不允许此操作。」；断言含一条理财锁用例 |

## §2 既有事实

### 2.1 C08 链（银行已取消 → 对话侧不感知）

| 事实 | 位置 |
|---|---|
| authorize() 三分支：cancel 分支对已 cancelled 的 action **幂等**返回 action_view；红色且码不符时 verification_failures += 1，达到 3 次置 status="cancelled" 并返回 {**action_view(action), "verification_passed": False} | bank_backend/app/domain/card_action_service.py:116-145（cancel 幂等 :123-130，错码 :134-142，_require_pending :133） |
| 第 3 次失败响应**带 status="cancelled"**；幂等重放（第 4 次点击）的响应是纯 action_view，**没有 verification_passed 键** | 同上，:134-142 与 :123-130 的差异 |
| hitl 只判断 data.get("verification_passed", True)，失败一律抛 VERIFICATION_FAILED（details.status 传了但无人消费） | backend/app/agents/hitl.py:23-72（「还可尝试」文案唯一出处 :65） |
| 路由在 resume 之前调 _authorize_card_action_if_needed；错码不落账本、暂停保持 | backend/app/api/routes.py:538（在 :539 _turn_response 之前） |
| 账本 message 是 domain-agnostic 的「确认本次操作」/「取消本次操作」 | backend/app/api/routes.py:495-496；context :500 |
| 请求指纹 = request_hash(conversation_id, message, canonical_context)，而 canonical context 只保留 mode 与 resume 的 question_id/value | backend/app/conversation/service.py:91-113（:96-102）；backend/app/conversation/turns.py:171-187（docstring :187 明说 verification_code 不属于指纹） |
| 「不落 state、不进指纹」的 turn 通道已有先例 | backend/app/agents/turn.py:125-145（TurnAction.verification_code，:143-145 校验只能随 confirm 传入）；backend/app/agents/runtime.py:63-74、:94-108；用法 backend/app/modules/transfer/workflow.py:435、backend/app/modules/wealth/workflow/graph.py:374-375 |
| 取消文案唯一出处 | backend/app/modules/card/tools.py:148-160（ToolData.summary） |
| card 模块的取消分支已存在：CANCEL → invoke("card_cancel", {"action_id": ...}) → from_result | backend/app/modules/card/workflow.py:450-467（:452-455） |
| 前端 submitAction：乐观撤卡 :925；收到 interrupt 回填 :941；catch 里 session.live=null + refreshHistory 后 rethrow :957-960 | frontend/src/App.vue:898-970（docstring :888-897 说明「被拒的验证码不是撤卡场景」） |
| 第 4 次提交得到通用文案「该业务当前状态不允许此操作。」= INVALID_STATE（银行 1005） | backend/app/integration/bank_errors.py:44-70（:64）；报告 req_e6ae5c39f030459da9151838bd18fce3 |
| 第 4 次点击的真实路径：decision=confirm → hitl POST /cards/actions/{id}/authorize → _require_pending 抛 1005 → hitl 转 AppError(INVALID_STATE, 409) → 通用文案；**decide 路由从不调 card_execute** | backend/app/agents/hitl.py:45-56（POST :46-52，转码 :53-56）；bank_backend/app/domain/card_action_service.py:133、:216-219 |
| hitl 的 verification_passed 默认 True 分支只在 authorized/completed 重放时可达；decision != "confirm" 时 :34 已提前 return，cancelled 走不到那里 | backend/app/agents/hitl.py:34-35、:57-58；bank_backend/app/domain/card_action_service.py:131-132 |

### 2.2 C13 链（尾号命中被卡型否决）

| 事实 | 位置 |
|---|---|
| matched_cards 对 card_suffix 与 card_type 做 AND 过滤，两者都可缺省 | backend/app/modules/card/tools.py:49-58 |
| 消费点：detail() 与 prepare()（len != 1 → VALIDATION/NOT_FOUND「请明确选择一张本人银行卡。」） | backend/app/modules/card/tools.py:86-91、:97-108 |
| 写工具的规格用 SELECTOR 且 required=() → 两个选择器都声明为可选 | backend/app/modules/card/tools.py:187-228（:220-228 列表推导生成 freeze/unfreeze/report_loss/release_loss） |
| 但 strict 模式下「可选」被强制变成 required+nullable：不在 required 的属性被改成 [T, "null"] 并 required.append(name) | backend/app/tools/schema.py:35-79（:60-68）；docstring :5-6 原文「strict mode has no notion of an optional property, so an optional one has to become a required nullable one」 |
| 模块侧先预读再过滤：非 card_cards/card_apply 的工具先 invoke("card_cards", {})，matches 空 → finish("没有找到符合条件的本人银行卡。", FAILED) | backend/app/modules/card/workflow.py:233-239（预读 :233-236，空 :238-239） |
| 单张命中才回填 card_suffix/card_type 并真正 invoke 写工具 → 参数不匹配时活动流不会出现写工具 TOOL_CALL | backend/app/modules/card/workflow.py:269-277 |
| card_type 的 SELECTOR 条目**没有 description**（card_suffix 有） | backend/app/modules/card/tools.py:16-23（:22） |
| 库证据：run_3bd9359a 只有一次 card_cards {} 的 TOOL_CALL，其后 card · FAILED · 没有找到符合条件的本人银行卡。；对照 C12 成功回合有 card_unfreeze {card_type: virtual, card_suffix: 3878} | 报告 C13/C12 活动流；llm_call_audit 对照（见 2.3） |
| 生成后的选择器 schema：card_type → {"type":["string","null"],"enum":["debit","credit","virtual"]}，required=["card_suffix","card_type"]，additionalProperties=false → enum 不含 null，模型无法表达「不确定」 | 实测（backend/.venv 跑 app.tools.schema.normalize_parameters）+ backend/app/tools/schema.py:54-69 |
| declared_arguments 丢弃 null 与 ""/"null"/"none"/"undefined"（_PLACEHOLDERS），且丢弃发生在 schema 校验之前 → 「填 null」会让 required 校验失败，不是可用通道 | backend/app/modules/card/workflow.py:61-72、:227-231 |
| SELECTOR 是共享对象：card_apply 也用它，那里 card_type 是「要开的卡类型」且必填 | backend/app/modules/card/tools.py:16-23、:191-197 |
| 现有测试钉子 | backend/tests/test_card_module.py:207、:221、:272、:320-332、:354、:386 |

### 2.3 审计缺口链

| 事实 | 位置 |
|---|---|
| 流式调用只累加 chunk.text_delta，ModelReply(text=...) 没有 raw → raw 恒为 None → response_json 写成 JSON null | backend/app/audit/model_client.py:91-120（:109-114、:118-120）、:155-182（:165-166） |
| 集成层投影了工具调用增量，但**没有 id、也没有 finish_reason**：StreamChunk 字段只有 text_delta / tool_call_delta / tool_name / tool_index / reasoning_delta / finish(bool) / truncated | backend/app/integration/model_client.py:186-205（字段 :199-205）、_stream_chunk :384-413 |
| 共享客户端构造时不传 agent_id → 417 行 agent_id 全 NULL | backend/app/integration/llm_config.py:167-189（:184-189 AuditedModelClient(client, audit, provider=…, model=…)，无 agent_id）、:191-207（:206） |
| agent_id 的两个绑定点（仓库里仅有的两处 AuditContext 构造） | backend/app/agents/runtime.py:530（AuditContext(user_id=user_id, source=SOURCE_SESSION_CHAT)）；backend/app/conversation/turns.py:281-286（AuditContext(conversation_id, run_id, user_id, source=SOURCE_CONVERSATION)）；SOURCE_* 常量在 backend/app/audit/context.py:27-32，AuditContext 字段 :48-52，merged 忽略 None :54-63 |
| AuditContext 是 ContextVar 传 conversation/run 的既有通道；merged 忽略 None | backend/app/audit/context.py:35-52、:54-63、:66-80 |
| agent 边界知道自己的 id：_handoff_for 用 spec.agent_id；domain_scope 是模块自建图的自然注入点 | backend/app/agents/sdk.py:163-189（:185）、:192-213（:211-213） |
| 库证据：llm_call_audit 417 行 result 全 SUCCEEDED、run_id 从不为 NULL（0 行）、148 个不同 run_id；**全表 0 行的 prompt 含 role=tool 的消息**；run_3bd9359a 的第三行 call_6616ddd2 与第一行 call_1eaee4c2 的 user 消息逐字相同（只有用户消息 + 调度指令，无卡结果注入）→ 该 run 里 card 只调用了一次模型 | 本机 PG test 库 llm_call_audit；对照 agent_run_event（CONTEXT_ASSEMBLED 数与审计行数不成 1:1，不可作缺口判据） |
| 测试钉子 | backend/tests/test_audited_model_client.py:105（`assert row.response_json == raw`）、:121-135（失败路径用例，**不要改**）、:211-269、:312-325；backend/tests/test_context_vocabulary.py:77-98（AuditContext 的既有单测）；test_audit_repository.py:74、:100；test_llm_settings.py:225、:296。**backend/tests/test_audit_context.py 不存在** |

### 2.4 W06 链（验证码错误复用 1007）

| 事实 | 位置 |
|---|---|
| VerificationFailedError 继承 ConfirmationRequiredError 但只有 docstring，不覆盖 code/message → 线路仍是 1007 pending_confirmation | bank_backend/app/core/errors.py:44-49、:52-57（文件头 :3-5 说错误码与 bank_backend/API接口文档.md 的 ErrorResponse 一致：1004/1005/1012） |
| 1007 → CONFIRMATION_REQUIRED | backend/app/integration/bank_client.py:26-35 |
| CONFIRMATION_REQUIRED → 「该操作需要额外确认。」（即报告 W06 的通用文案） | backend/app/integration/bank_errors.py:44-56、:58-70（:66） |
| ErrorCode 是稳定契约（注释：改动是 breaking change；One-to-one with BankErrorKind） | backend/app/tools/protocol.py:21-47（:24-27、:31、:35） |
| 模块侧错误文案表 | backend/app/modules/base.py:63-78（:68） |
| 理财把「验证码错误」等价于 CONFIRMATION_REQUIRED | backend/app/modules/wealth/infrastructure/bank_adapter.py:366 |
| 银行侧错码产生点 | bank_backend/app/domain/investment_service.py:668、:686 |
| **缺码在两个银行路径上已经是验证失败**（不是「需确认」）：card_action_service.py:134-136 用 (code or "") 与 mock_mfa_code 比较，None/空串计入失败；investment_service.py:667 `if not mfa_code or mfa_code != settings.mock_mfa_code` 同 | bank_backend/app/domain/card_action_service.py:134-136；bank_backend/app/domain/investment_service.py:660-669 |
| 理财锁：LOCK_THRESHOLD=5 / LOCK_MINUTES=15（按 tool_name 计），第 6 次 BusinessError 1005「资金工具 X 已锁定至 …，请先调用 /security/reset-lock」被 bank_client.py:46 换成通用文案 | bank_backend/app/domain/security_service.py:24-32、:76-94、:97-114；backend/app/integration/bank_client.py:46 |
| bank 侧现用码：1004/1005/1006/1007/1012 → 1008–1011 空闲（全仓 grep "1008" 只命中本 spec） | bank_backend/app/core/errors.py:14、:27、:33、:39、:47、:61 |
| CONFIRMATION_REQUIRED 在 backend/app 内的全部命中（9 行、11 处；新增码后需逐处复核） | 定义与文案：backend/app/tools/protocol.py:35、modules/base.py:68；映射与文案：integration/bank_client.py:33、integration/bank_errors.py:24/:48/:66；消费者：modules/card/workflow.py:461、modules/transfer/workflow.py:438、modules/transfer/writes.py:48、modules/wealth/infrastructure/bank_adapter.py:366（只有 bank_adapter 那处要改成 VERIFICATION_FAILED） |
| 理财侧摘要拼接：summary=f"{reason}请重新输入验证码后再确认。"，reason 取自 BANK_ERROR_MESSAGES | backend/app/modules/wealth/application/runtime.py:1224；backend/app/integration/bank_errors.py:83-86 |
| 需同步的钉子测试 | bank_backend/tests/api/test_card_actions.py:48、:81、:90-96、:109、:112-118；test_investment_routes.py:128-140；test_system_routes.py:210-231（锁用例；**:230 的 1005 断言改 1009**，:185 是另一处 1007）；test_transfer_routes.py:211（缺码语义，保持 1007）；backend/tests/test_error_codes.py:59、:61；backend/tests/test_bank_client.py:36-40、:74-79、:255-259；backend/tests/test_wealth_write_api.py:453。bank_backend/tests/domain/test_wealth_write_contract.py:21、:94、:96、:124、:149 按 D7 **保持不动**（领域层不改）；银行侧文档 bank_backend/API接口文档.md:319、:355 与 bank_backend/README.md:98、:125 需补 1008/1009 |

### 2.5 W01 口径

| 事实 | 位置 |
|---|---|
| 模块 README 明说只读工具注册进基座工具网关、每一次查询都是基座调用的原生工具（tool_call/tool_result 帧），模型自主选择工具 | backend/app/modules/wealth/README.md:30-38、:209-212 |
| 测试钉住「只读工具留下 tool_call/tool_result 帧」 | tests/test_wealth_graph.py:8、:145-147 |
| 测试钉住「只读流程不暂停」 | backend/tests/wealth/test_read_flow_has_no_pause.py:1-13、:64、:133-150；tests/wealth/test_read_flow.py:586、:591、:618；test_tool_selection.py:594、:623、:657 |
| 过期口径 | docs/superpowers/specs/2026-10-11-wealth-whole-turn-integration-spec.md:411；docs/wealth/理财子智能体联调整改编码交接.md:22；docs/wealth/联调整改验收记录.md:110 |
| **没有任何测试断言只读回合 tool_call 计数为 0** | 全仓测试检索结论 |

## §3 设计决定

- **D1（C08 通道）** 自动取消走 turn 通道，**不改写 message/context**。理由：请求指纹 = request_hash(conversation_id, message, canonical_context)，改写 context.resume.value.decision 会改变指纹，使重放落到 INVALID_INTERRUPT_ANSWER 而不是幂等回执（service.py:96-102）。通道复用 verification_code 的既有路径（turns.prepare → TurnContext），并且**必须由服务端构造，客户端 body 不得提供**。
- **D2（C08 判据）** 分流判据用 data.get("status") == "cancelled"，不用 verification_passed。理由：第 3 次失败响应带 status="cancelled"，而幂等重放响应只有 action_view（无 verification_passed 键）。第 4 次点击**不是**「默认 True 放行到 card_execute」——decide 路由从不调 card_execute（backend/app/api/routes.py:538 只调 hitl 的 authorize helper）：它是 hitl POST authorize → bank_backend/app/domain/card_action_service.py:133 _require_pending 抛 1005 → backend/app/agents/hitl.py:53-56 转 INVALID_STATE 409 → backend/app/integration/bank_errors.py:64 的通用文案；默认 True 分支只在 authorized/completed 的重放时可达（card_action_service.py:131-132）。
- **D3（C08 复用）** 复用 card_cancel，不新增工具。银行侧 authorize(cancel) 对已 cancelled 的 action 幂等，因此重放安全且不产生第二次资金动作。
- **D4（C08 账本）** 用户的点击仍如实记「确认本次操作」；取消由助手侧结果陈述（子任务行 + 最终答复）表达。伪造用户取消气泡会与 routes.py:496 的账本 message 及 C20 对照用例矛盾。差异记入 §7。**评审已确认采用本项（选项 B）**。
- **D5（C13 语义）** 选择器改为「身份 + 消歧」两段式：card_suffix 是身份，card_type 只能在歧义集合上收窄、永不否决。schema 生成器把声明为可选的属性变成 required+nullable（backend/app/tools/schema.py:54-69），而 card_type 的 enum 不含 null → 模型**无法表达不确定、只能猜一个**；backend/app/modules/card/workflow.py:61-72 的 declared_arguments 又把 null 丢掉、丢弃发生在校验之前，所以「填 null」也不是可用通道。⇒ 只能在消费侧容错（type 不否决），并给选择器补 description 让模型知道卡型只用于消歧。
- **D6（审计）** 不新增表/列：response_json 已存在且迁移已建，流式调用把它填上（tool_calls + finish_reason）；为此 backend/app/integration/model_client.py:199-205 的 StreamChunk 需新增 tool_call_id / finish_reason（id 取上游 call.id、上游不给时为 None，审计侧按 tool_index 归并；finish_reason 取上游 choice.finish_reason、缺失时回落 "tool_calls"）。agent_id 走 AuditContext 新增字段，由 domain_scope（backend/app/agents/sdk.py:192-213）设置，构造参数 agent_id 作为回退（优先级规则落在 audit/model_client.py 的两处）；调度器侧用 backend/app/audit/context.py:27-32 新增的 AGENT_ID_SUPERVISOR = "supervisor"，在 backend/app/agents/runtime.py:530 与 backend/app/conversation/turns.py:281-286 两处绑定。
- **D7（W06 + 缺码拆分）** 验证码错误拆出独立错误码（bank 侧 1008 + 后端 ErrorCode/BankErrorKind 新增），**同时必须把「缺码/空码」从验证失败里分出来**：今天 bank_backend/app/domain/card_action_service.py:134-136 与 investment_service.py:667 都把 None/空串算作验证失败，不拆的话改完后「缺码」也会变成 1008、1007 就空了。拆分的落点分两条路径：**卡路径在领域层拆**（码缺失时抛 ConfirmationRequiredError）；**理财路径只改 HTTP 层**（bank_backend/app/api 里把 VerificationFailedError 转成响应的地方，按「本次请求是否带了 mfa_code」分流成 1007），领域层 bank_backend/app/domain/investment_service.py:660-669 与 bank_backend/tests/domain/test_wealth_write_contract.py:96-97 保持不动——那里 mfa_code=None 断言 VerificationFailedError 是既有领域契约。资金工具锁（第 6 次）另加 1009（R-13），security_service.require_not_locked 对**所有**资金工具一致。这是线路契约的行为变更，必须在 PR 描述里显式声明。
- **D8（W01）** 只改文档，不改实现。

## §4 分项修复

### 4.1 C08（P0）

改动点：
1. backend/app/agents/hitl.py:23-72：_authorize_card_action_if_needed 返回结果对象 `CardAuthorizationOutcome`（frozen dataclass，定义在本文件）：`status: Literal["none","authorized","cancelled"]`、`remaining: int | None = None`，不再只返回 None（错码但未取消仍抛 VERIFICATION_FAILED，所以没有 rejected 返回值）。cancelled 判定 = data.get("status") == "cancelled"（D2），此时**不抛**；verification_passed 为 False 且未取消时保持现状抛 VERIFICATION_FAILED（details 保留 status，供后续消费）。
2. backend/app/api/routes.py:488-547：接收该结果；cancelled 时不抛，把自动取消信号经 _turn_response（:375-383、:403-413）沿 verification_code 的同一通道传给回合；message（:496）与 context（:500）保持原样（D1）。
3. 自动取消字段的全链路（缺一处都传不到，必须逐处改）：
   - **不动 backend/app/agents/turn.py**：TurnAction 在生产路径只当常量持有者（card/workflow.py:452 的 action_kind 返回字符串，TurnAction 只在 tests/support/handler_graph.py:107 被构造），加字段没有消费者
   - backend/app/conversation/turns.py:63-77（TurnPlan 数据类）、:180（prepare 签名）、:228-236（TurnPlan 构造）、:270-280（TurnContext 构造）
   - backend/app/agents/runtime.py:85-108（TurnContext 字段与 docstring：不进指纹、不进 AgentState）
   - backend/app/api/routes.py:405-413（_turn_response → turns.prepare）
   - 校验钉成「只能由服务端 resume 路径设置」
4. backend/app/modules/card/workflow.py:165-173：**不再 del turn**（现为 `del assembler, turn, client`），docstring 同步；:450-467 的 CONFIRMATION_KIND 分支里，若 `turn is not None and turn.auto_cancel` 为真，直接走已有的 CANCEL 分支（:453-455 invoke card_cancel → from_result），不读 answers、不调 card_execute。先例：backend/app/modules/transfer/workflow.py:435、backend/app/modules/wealth/workflow/graph.py:374-375（turn 用闭包、不进 state）；装配点 backend/app/agents/supervisor/graph.py:427-429 已把 turn 传进 graph_builder。
5. 第 3 次失败那一回合必须走完 resume 并把 pending 消费掉（清空），否则第 4 次点击仍会打到银行、R-4 不成立；用测试钉住（§5）。
6. frontend/src/App.vue：**不做**「catch 里无条件 setPending(null)」——:925 已在请求前乐观撤卡，:957-960 的 refreshHistory 会按服务端暂停重新 setPending（:537），无条件清卡会藏掉 details.question_id 指向的那张卡（:230 已有 INVALID_INTERRUPT_ANSWER 分支）。只保留「服务端确认该 question_id 已不再开放时才清」的语义；要改就改 refreshHistory 的判定，不改 catch。

验收：R-1..R-4、R-2b。

### 4.2 C13（P0）

改动点：
1. backend/app/modules/card/tools.py:49-58：matched_cards 改两段——先按 card_suffix 取 identity 集合（无 suffix 时为全量），identity 恰好 1 张即返回（忽略 card_type）；否则再按 card_type 收窄。空集合仍返回空，:86-91 的失败码选择不变。
2. backend/app/modules/card/workflow.py:269-270 的回填保持：单张命中时用查回的 card_type 覆盖 args，避免把猜错的卡型写进写工具调用。
3. backend/app/modules/card/tools.py:22：给**选择器副本**补 description「仅在尾号歧义时用于消歧」，例如 `SUFFIX_SELECTOR = {**SELECTOR, "card_type": {**SELECTOR["card_type"], "description": "仅在尾号歧义时用于消歧"}}`，只用在 :220-228 的写工具列表；card_apply 继续用原 SELECTOR（那里 card_type 是「要开的卡类型」，语义不得被改）。
4. 把模型原始写参数落日志（可诊断性）：backend/app/modules/card/workflow.py:227-239 一带，把 declared_arguments 后的 args 记入日志（今天写参数从不外发，C13 的「猜错卡型」与「猜错尾号」无法区分）。**backend/app/modules/card 下现无 logger，需要 `import logging` + 模块级 `logger = logging.getLogger(__name__)`**。

验收：R-5..R-7。风险见 §8（刻意的语义放宽）。

### 4.3 审计缺口（P1）

改动点：
1. backend/app/integration/model_client.py:186-205：StreamChunk 新增 `tool_call_id: str | None = None` 与 `finish_reason: str | None = None`；_stream_chunk :384-413 两个分支都填 finish_reason（取上游 choice.finish_reason，为 None 时保持 None），tool-call 分支再填 tool_call_id（取上游 call.id，缺失为 None）；既有的 `finish: bool`（:398 的 `getattr(choice, "finish_reason", None) is not None`）语义不变。
2. backend/app/audit/model_client.py:91-120：stream() 在累加 text_delta 之外，用 tool_index 归并 tool_call 增量（name / arguments / id），流结束时构造与 _response_json 同形的 response（choices[0].message.tool_calls + finish_reason）交给 `_record_success`（**调用点在 :118-120；:155-182 是该函数的定义，不必改**），使 response_json 非 null；**只在真的出现过工具调用时**才写，纯文本流式仍写 JSON null。
3. backend/app/audit/context.py:27-32：新增 `AGENT_ID_SUPERVISOR = "supervisor"`（与既有 SOURCE_* 并列）；:35-52 AuditContext 新增 agent_id（默认 None，merged 忽略 None）。
4. 两个绑定点传该常量：backend/app/agents/runtime.py:530、backend/app/conversation/turns.py:281-286。
5. backend/app/agents/sdk.py:192-213：domain_scope 内用 audit_context 合并 agent_id=spec.agent_id。
6. 优先级规则（ContextVar 优先、构造参数兜底）落在 backend/app/audit/model_client.py 里把 agent_id 交给 AuditContext 的两处（实现时定位：:155-182 与 :184-220 一带的 self._agent_id）；backend/app/integration/llm_config.py:170-189 保留构造参数（不传 agent_id）。

验收：R-8、R-9。

### 4.4 W06（P1）

改动点：
1. bank_backend/app/core/errors.py:52-57：VerificationFailedError 覆盖 code/message/http —— code=1008、http=409、message="验证码不正确"（1008 经全仓 grep 确认空闲：errors.py 现用 1004/1005/1006/1007/1012）；同步更新 bank_backend/API接口文档.md:319、:355 与 bank_backend/README.md:98、:125（都记作「验证失败返回 1007」）。
2. **缺码拆分**（否则 1007 会空），两条路径两种做法：**卡路径在领域层拆**——bank_backend/app/domain/card_action_service.py:134-136 码缺失/为空时抛 ConfirmationRequiredError(1007)，非空错码才抛 VerificationFailedError(1008)；**理财路径只改 HTTP 层**——bank_backend/app/api 里把 VerificationFailedError 转成响应的那一处按「本次请求是否带 mfa_code」分流（不带 → 1007），领域层 bank_backend/app/domain/investment_service.py:660-669 与 bank_backend/tests/domain/test_wealth_write_contract.py:96-97 不动。bank_backend/tests/api/test_card_actions.py:90 的 `for code in (None, "654321")` 钉卡路径，test_investment_routes.py 钉理财路径。**重放豁免（commit 751e632）**：HTTP 层的缺码守卫必须避开幂等重放 —— 先算一次 `key = resolve_idempotent_key(idempotency_key, body.idempotent_key)` 并同时传给服务，守卫再加 `and not _is_replay(database, user_id=body.user_id, operation_type=..., key=key)`；helper 见 bank_backend/app/api/routes.py:74-92，两处守卫在 :490-513（purchase_product）与 :527-549（redeem_product），`operation_type` 必须与服务内 `_replay` 一致，没有幂等键时不多查库。理由：幂等指纹不含 mfa_code/confirm，已成交的重试漏码仍必须返回原回执（`replayed: true`），否则是本分支引入的契约回归。
3. 资金工具锁新码（R-13）：bank_backend/app/domain/security_service.py:80-94 的锁 BusinessError 改用 1009（银行侧 message 保持英文稳定 token `"locked"`），bank 侧 errors.py 新增 LockedError；require_not_locked 的三个调用点（bank_backend/app/domain/investment_service.py:199、:451、transfer_service.py:163）一起变；对话侧 backend/app/integration/bank_client.py:26-35 新增 1009 → BankErrorKind.LOCKED，backend/app/integration/bank_errors.py 新增文案 `"该资金工具已锁定，请稍后重试。"`（与 modules/base.py 的 ERROR_MESSAGES 同句，不得再落到通用 INVALID_STATE）。
4. backend/app/integration/bank_client.py:26-35 新增 1008 → VERIFICATION_FAILED；backend/app/integration/bank_errors.py 新增 BankErrorKind.VERIFICATION_FAILED、KIND_TO_ERROR_CODE、`BANK_ERROR_MESSAGES[VERIFICATION_FAILED] = "验证码不正确。"`——**必须恰好是这一句**：backend/app/modules/wealth/application/runtime.py:1224 会拼成「{reason}请重新输入验证码后再确认。」，frontend/src/App.test.ts:1821 钉的就是「验证码不正确。请重新输入验证码后再确认。」；backend/tests/test_bank_client.py:36-40（集合相等）、:74-79（文案唯一性）与 :255-259（参数化表）会同时要求它。
5. backend/app/tools/protocol.py:21-47 新增 ErrorCode.VERIFICATION_FAILED（以及第 3 条需要的 LOCKED）；backend/app/modules/base.py:63-78 补对应文案（test_error_codes.py:59 的集合相等断言会同时要求两侧都加）。
6. backend/app/modules/wealth/infrastructure/bank_adapter.py:366：verification_failed 判定改为只看 BankErrorKind.VERIFICATION_FAILED（R-11）。
7. 逐条复核 §2.4 列出的 1007 消费者与钉子测试（13 处代码 + 测试）：属于「缺码/需确认」的保持 1007，属于「错码」的改 1008。

验收：R-10、R-11、R-13。

### 4.5 W01 口径（P1，仅文档）

1. docs/superpowers/specs/2026-10-11-wealth-whole-turn-integration-spec.md:411：改写过期段落，标注 2026-10-13 提交 03fd141 的改设计。
2. docs/wealth/理财子智能体联调整改编码交接.md:22、docs/wealth/联调整改验收记录.md:110：补上同一结论。
3. backend/app/modules/wealth/README.md:30-38（**核对即可**，那里已经是新口径）、:209-212：保留现有口径，在 :209-212 补一句「只读回合因此会出现 tool_call 帧；以 interrupt（提问卡）结束的回合也会发出 subtask_end（turn_status=waiting_input、摘要即那句追问，事件层 status 仍是 SUCCEEDED）；承载查询结果的 turn_status=completed 子任务行属于用户答完追问后的完成轮」。
4. 处置口径（W01-Q3）：文档写明承载查询结果的 turn_status=completed 子任务行属于「用户答完追问后的完成轮」；追问轮本身也会发出 subtask_end（turn_status=waiting_input、摘要即那句追问）。首轮按方案要求先追问三件事、以提问卡（interrupt）结束，因此首轮没有那条承载查询结果的子任务行不是缺陷。落点：docs/wealth/联调整改验收记录.md:174 附近（:110 那条只改口径）。

验收：R-12。

## §5 测试计划

新增：
- backend/tests/test_card_auto_cancel.py：桩银行第 3 次错码返回 status="cancelled" → 断言不调 card_execute、调用 card_cancel、subtask_end 与 domain_results 摘要恰为「本次银行卡操作已取消，未修改银行卡。」（R-2b；最终 assistant 文本由模型生成，**不作逐字断言**）、无 interrupt 帧；同 key 重放返回 Receipt 且不再次 authorize；客户端 body 携带 auto_cancel 被 schemas 拒为 422（提权面钉子）。桩银行沿用既有的 monkeypatch request 模式（backend/tests/test_card_authorization_api.py:29-42、backend/tests/test_card_decide_authorize.py:89-109），不给 CardBank 加新入参；「第 3 次才取消」这个阈值本身由 bank_backend/tests/api/test_card_actions.py:112-118 钉住。
- backend/tests/test_card_tools.py（**确认不存在，新建**）：matched_cards 三态——identity-only 命中、歧义时按 card_type 收窄、card_type 不符不否决尾号命中；以及**生成后的** function schema 里写工具副本的 card_type 带消歧 description、card_apply 的 card_type 描述未被改（R-7）。
- backend/tests/test_audited_model_client.py 新增流式用例（该文件现无流式用例）：流式返回工具调用时 response_json 含 tool_calls（name/arguments）与 finish_reason；纯文本流式仍为 null；agent_id 断言落在**落库行**上（"card" 与 "supervisor" 两态，不只断言 ContextVar）。
- backend/tests/test_card_decide_authorize.py 增用例：第 3 次失败回合结束后 pending 已被消费；第 4 次点击返回 409 且不调用 authorize（沿用 backend/tests/test_card_authorization_api.py:151-159 的 stale 点击形状）。
- frontend/src/App.test.ts 增用例：服务端仍报同一 question_id 的 409 时**不清卡**；服务端说该 question_id 已不再开放时才清卡。
- bank_backend 增用例：锁路径第 6 次 → 1009 + 文案含「已锁定」，**含一条理财锁**（investment_service 的 require_not_locked 路径）。

修改：
- backend/tests/test_card_decide_authorize.py：**新增**用例（:104 是桩数据、:120-121 是既有断言——错码仍抛 VERIFICATION_FAILED，**保持不变**）。
- backend/tests/test_audited_model_client.py：**新增**流式用例；:121-135 是失败路径用例（断言 LLM_TIMEOUT），**不要改**；:105、:312-325 一并复核。
- backend/tests/test_bank_client.py:36-40（集合相等）、:74-79（文案唯一性）、:255-259（参数化表）与 backend/tests/test_error_codes.py:59：新增成员后两侧同步。
- bank_backend/tests/api/test_card_actions.py:48、:81、:93、:109 按语义拆分 1007/1008；:90-96 补缺码用例（None/空串 → 1007）；:109 的 verification_failed 计数由 2 改 1（None 不再计一次失败）。
- bank_backend/tests/api/test_system_routes.py:230 的锁断言 1005 → 1009，并在 test_investment_routes.py 补一条理财锁用例。
- backend/tests/test_card_module.py：新增「卡型猜错仍命中尾号」用例；:272 的 8801 用例保持。
- backend/tests/test_wealth_write_api.py:453（活钉子：摘要含「请重新输入验证码」）复核文案仍是「验证码不正确。请重新输入验证码后再确认。」。
- bank_backend/tests/domain/test_wealth_write_contract.py 按 D7 **保持不动**（理财缺码拆分只做 HTTP 层）。

保持不变（作为回归钉子）：
- backend/tests/test_pending_action_api.py:184、:368（账本文案仍是「确认本次操作」）。
- tests/test_wealth_graph.py:145-147；backend/tests/wealth/test_read_flow_has_no_pause.py。

计划阶段第一步（环境前置；工作树未继承 gitignore 的运行环境）：
- backend：`uv sync --dev`（或从父仓 D:/ai_project/agent/backend/.venv 复制），补 .env、database.local.txt。
- bank_backend：`uv sync --dev`（无 requirements.txt；pyproject dev group 含 httpx/pytest/pyyaml/ruff）；补 .env、database.local.txt；设 TEST_DATABASE_URL（bank_backend/.env.example 明说不要指向 bank_db）；bank_backend/tests/conftest.py:28-56 会建 schema 并跑迁移（~7s、17 表）。
- frontend：`pnpm install`（node_modules）。
- 跑一次基线（backend pytest、bank_backend pytest、frontend vitest），记录失败基线，避免把既有失败当成新引入。

## §6 范围边界

范围内：C08、C13、W06（含缺码/错码拆分）、理财锁新码 1009、审计缺口、W01 文档口径。

范围外（本 spec 不处理，建议另开 spec 或后续验收项）：
- 报告 §6.2 的 6 条偏离中除 W06 之外的 5 条：C14（模型不调 report_loss，直接如实解释）、C18a（0.001 元走交互式纠错卡而非 FAILED 子任务）、W03（持仓只给产品编号 p_demo_cash，不给产品名）、W07（取消气泡文案「取消本次操作」而非「取消本次理财操作」）、W13（订单查询未接入，且「订单」被路由到 bill 域）。（注：W01 是 §6.1 的 3 个失败项之一，不是偏离。）
- W14：写锁是进程内存（backend/app/modules/wealth/application/runtime.py:377-392、:567、:577-586、:830），重启即解除。
- 授权卡 product_name 与 account_display 都等于卡标签（backend/app/modules/card/workflow.py:152-153）导致界面出现重复的两行。
- 按钮集合不统一；只读兜底文案。
- 验收方案文档自身缺陷 4 条：复位 SQL 使用不存在的 holdings.realized_income_minor（实际列为 id/user_id/product_id/source_account_id/principal_minor/current_value_minor/expected_return/purchased_at/maturity_date/status/created_at）；W12 假定 p_demo_stable 持仓不存在；W13 订单查询未接入；只改 accounts.balance_minor 会让 available_minor 不一致 → seed_service.health() 抛错 → GET /api/health 503 → start.ps1 健康门禁回滚。
- 环境问题：17:56 前后端进程无声消失；scripts/seed_wealth_demo 缺 DATABASE_FILE 会误打云端 RDS。

## §7 与验收报告的差异（评审已确认，2026-10-15）

### 7.1 C08 的「自动发出取消本次银行卡操作」——采用选项 B

- **决定**：不合成用户气泡。用户气泡保持如实的「确认本次操作」（routes.py:496 的账本文案），取消由助手侧子任务行「本次银行卡操作已取消，未修改银行卡。」与最终答复表达（D4）。
- **理由**：账本是不可改写的记录，替用户"发出"一句他没说过的话会与 routes.py:496 的 message 及 C20 对照用例矛盾；真走一次自动取消请求还会多出一个回合、第二次模型调用与第二个幂等键。
- **被否决**：
  - 选项 A（系统自动发出取消气泡）：账本失真 + 多一次请求/回合。
  - 选项 C（助手侧独立提示行，文案「取消本次银行卡操作」）：同样不是用户气泡；若后续 UI 确实需要这条字面文案，可作为选项 B 之上的 UI 增强，不改后端契约。

### 7.2 W01 只改文档，不回退实现——认可 03fd141 的新设计

- **决定**：只读工具经基座网关以原生 tool_call/tool_result 回传是现行设计，文档与判定口径向它对齐（§4.5）。
- **理由**：03fd141（2026-10-13）已显式改设计，模块 README 与 tests/test_wealth_graph.py:145-147 都按新口径钉住，旧口径（2026-10-11 spec:411）无任何测试支撑。
- **W01-Q3（处置口径）**：接受「首轮按方案要求先追问三件事、以提问卡结束；该轮的 subtask_end 带 turn_status=waiting_input、摘要就是那句追问（事件层 status 仍是 SUCCEEDED）；承载查询结果的 turn_status=completed 子任务行出现在用户答完三件事的完成轮」。报告里那句"没有 wealth · SUCCEEDED 子任务行"与事件流不符——该轮确实发出了 `subtask_end`（`status=SUCCEEDED`、`turn_status=waiting_input`）；第一轮真正缺的是**承载查询结果**的那条 `turn_status=completed` 子任务行，它属于完成轮。处置仍是**不是缺陷**：设计把查询结果放在完成轮，首轮只负责追问（§4.5 第 4 条）。
- **被否决**：回退到「模块自持、无 tool_call」——要废掉 tests/test_wealth_graph.py:145-147 与 README 口径，等于回退 03fd141。

### 7.3 W06 新增银行错误码——采用选项 a

- **决定**：VerificationFailedError 覆盖 code/message，新增「验证码错误」专用码 1008；**同时把「缺码/空码」拆回 1007**（今天两条路径都把 None 算作验证失败，不拆 1007 会空）；资金工具锁另加 1009，覆盖全部资金工具。两侧同步（§4.4）。
- **缺码拆分的落点（本轮决定，1(i)）**：卡路径在领域层拆（card_action_service）；理财路径**只改 HTTP 层**——领域层 investment_service.py:660-669 与 bank_backend/tests/domain/test_wealth_write_contract.py:96-97 保持不动，因为那条断言（mfa_code=None → VerificationFailedError）是既有的领域契约，而「本次是否带码」在 HTTP 层本来就是已知事实，判据一样可靠。
- **理财锁（本轮决定，2(a)）**：1009 覆盖所有资金工具（`security_service.require_not_locked` 一处改动同时覆盖它的三个实际调用点：`transfer_service.py:163`、`investment_service.py:199`、`investment_service.py:451`；卡片域不走这个函数），并补一条理财锁断言；银行 message 保持英文稳定 token，中文文案只在对话侧。
- **理由**：报告指出的根因就是这个真实契约缺陷——验证码错误与「还缺一个码」在线上是同一个码，对话侧无从分辨；拆开之后判据是契约而不是猜测。
- **被否决**：
  - 选项 b（纯对话侧按「本次是否带码」推断）：零银行改动，但判据是推断而非契约，且会把这个契约缺陷留在代码里（hitl 与 bank_adapter.py:280 的 mfa_code 只能事后猜）。
  - 选项 c（银行外传 details.reason）：要动 payload 形状，收益与选项 a 重叠。

## §8 风险与回滚

- **自动取消通道**：若客户端能构造该字段即为提权面 → 必须服务端构造，并用「客户端 body 带该字段不生效」的测试钉住；回滚 = 去掉该字段与 routes 的传递，恢复 VERIFICATION_FAILED（C08 退回现状）。
- **C13 语义放宽**：用户明确说错卡型时也会命中尾号，这是刻意选择（尾号是身份）；回滚 = 恢复 AND 过滤（单函数改动）。
- **W06 契约变更**：bank_client 与 bank_backend 必须同版本部署（本仓同进程/compose，风险低）；回滚 = 恢复 VerificationFailedError 继承 1007 并撤回新增 kind 与 ErrorCode。
- **审计行变宽**：仅在流式且有工具调用时写 response_json，形状与 _response_json 一致；消费端（backend/app/audit/service.py:187、:612；backend/app/storage/models.py:299）不解析内部结构，风险低。
- **未跑基线**：工作树缺运行环境，基线测试未执行 → 计划阶段第一步补齐并跑基线，避免把既有失败当成新引入。
- **理财锁码（1009）**：银行侧新增 LockedError 与对话侧新 kind 必须同版本部署；回滚 = 锁恢复 BusinessError 1005（文案退回通用）。
- **理财缺码拆分只在 HTTP 层**：领域层的 VerificationFailedError(mfa_code=None) 契约不变，分流依赖 HTTP 层能拿到本次请求的 mfa_code；将来若有第二个入口绕过 HTTP 层（脚本、内部调用），它仍会把缺码报成 1008。
- **R-2b 不作逐字断言**：最终答复由模型生成，测试只钉确定性产物（子任务摘要 + 无 interrupt 帧）；若产品要求逐字文案，需把该文案放进确定性路径（另开 spec）。
- **C13 根因未经证实**：写参数此前从不落审计，§4.2 第 4 条把它补上；若实现后发现模型猜错的是尾号而不是卡型，两段匹配仍能覆盖（尾号错则确实无卡）。
