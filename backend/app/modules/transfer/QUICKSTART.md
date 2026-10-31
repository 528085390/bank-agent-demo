# 快速启动

## 跑一次转账（本地）

1. 起后端与前端（见仓库根 README），登录后开一个会话。
2. 说：「从活期账户转 100 给老王」——小额，卡上**没有**验证码框。
3. 点「确认」→ 回复「转账已完成：100.00 元。」；银行侧恰好一次 `POST /transfers`。
4. 再说：「从活期账户转 5000 给老王」——大额，卡上**有**验证码框。
   - 不填码直接点「确认」→ 422「这笔操作需要验证码，请填写后重试」，银行零写调用；
   - 填任意数字码（模拟 MFA）再点 → 成功。
5. 点「取消」→ 「已取消，本次转账没有发起。」，银行零写调用。

## 跑测试

```powershell
# 仓库根；用本机 PostgreSQL（先 psql -U postgres -c "CREATE DATABASE test" 建一次库）
.\pytest-local.ps1 -q tests/transfer
.\pytest-local.ps1 -Parallel 8 -q          # 全量
```

## 看一次点击在服务端发生了什么

```powershell
# 会话详情里有待确认卡片
curl http://127.0.0.1:8000/api/conversations/{id}      # pending_interrupt.confirmation
# 点击走这条受控路由
POST /api/conversations/{id}/pending-actions/{pending_id}/decide
     { "question_id": "...", "decision": "confirm", "verification_code": "123456" }
```

## 排错

| 现象 | 原因 |
|---|---|
| 点击返回 422 `VERIFICATION_CODE_REQUIRED` | 卡上要码（金额 ≥ 5000 元）而请求没带 `verification_code` |
| 点击返回 409 `INVALID_INTERRUPT_ANSWER` | 点的是已被消费或已作废的卡片（换了金额/账户会换 `pending_id`） |
| 卡片又回来了且多了验证码框 | 银行要码而模块算的 `requires_code` 是 False：模块把卡升级后重挂，填码再点即可 |
| 转账没有网关审计行 | 写走模块自己的受控路径（网关没有验证码通道），这是设计 |
