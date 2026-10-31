# 转账模块交付总结

## 交付物

- `tools.py`：五个平台工具（`transfer_accounts`／`transfer_contacts`／`transfer_scheduled`／`transfer_prepare`／`transfer_cancel`）与处理器。
- `writes.py`：模块自己的受控写路径 `submit_transfer`／`cancel_scheduled`。
- `workflow.py`：`build_transfer_graph`（`decide → wait → process`）。
- `__init__.py`：`TRANSFER` 声明 + `MODULE`（`graph_builder` 指向 `build_transfer_graph`）。
- 测试：`backend/tests/transfer/` 四个文件（工具层／写路径／图／HTTP 全链路）。

## 验收标准（逐条对应 spec §7）

| 场景 | 期望 |
|---|---|
| 小额（单笔 ≤1000 元且当日累计不超限） | 卡上无验证码框；点确认 → 一次 `POST /transfers`（`confirm=true`、无 `mfa_code`）→ 成功；点取消 → 银行零写调用 |
| 红标（单笔 + 当日累计 > 1000 元） | 卡上有验证码框；不填直接点 → 422 且未进图；填数字码 → 成功（原「大额 ≥5000」并入本档；银行仍是权威，1007 兜底） |
| 码不是数字 | 写路径判 `CONFIRMATION_REQUIRED`，卡片升级为重挂，不产生银行写调用 |
| 验证码 | 不进 SSE、不进账本、不进审计、不进检查点 |
| 模块形态 | 模块只有「工具 + 可选 `graph_builder`」一种形状；`transfer` / `wealth` / `card` 均由 `graph_builder` 挂到父图 |
| 前端 | 一行不改，卡片与验证码框正常渲染（人工冒烟，见 `tests/transfer/MANUAL_SMOKE.md`） |

## 已知边界

1. **写不经工具网关**：`POST /transfers` 不产生网关审计行（设计取舍见 spec §9）。
2. **不支持定时转账的创建**：只有查询与取消；创建登记为待开发（spec §12 D-1）。
3. **取消定时转账必须点击确认**：`transfer_cancel` 可见，但图先挂卡；模型不能直接执行这笔写。
4. **预约意图不会降级**：模型被要求如实说明不支持预约；银行写端点零调用（有回归）。
