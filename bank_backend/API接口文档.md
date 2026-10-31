openapi: 3.1.0
info:
  title: 模拟银行系统 API
  version: 1.3.0
  description: |
    面向 AI Agent 演示的完整功能 Mock 银行后端。首版使用 PostgreSQL 16，
    不做终端用户登录、JWT 或真实 MFA。
    金额统一为 CNY 整数分。所有写接口在数据库事务中更新状态，并支持幂等键。

    访问控制有两层。**服务凭证**：除 /health 之外的接口都要求请求头 X-Service-Token
    （/health 是容器探针，不含任何客户数据），它证明请求来自本系统的后端，不代表任何用户。
    **运维口令**：/seed/* 额外要求 X-Ops-Token，因为它会清空全库并重建演示数据。
    缺凭证返回 1001（401），缺运维口令返回 1002（403）。
    /clock* 不要运维口令：App 后端的演示控制面要代理它推进模拟时钟；推时钟可逆，清库不可逆。

    **user_id 必须由调用方明确给出**，缺失返回 1013（422）。它表示「调用方为该请求选择
    的银行客户」，不是登录态：模型不得自行指定身份；服务端会校验资源归属，不属于本人的
    资源按 1004 处理，与「不存在」不可区分。
    理财（申购／赎回）不按金额分级：固定需要 confirm=true 且 mfa_code 等于配置的演示
    验证码，申购还要求本人有效测评且产品风险不高于测评等级；测评、赎回规则和订单查询
    见 Wealth 标签下的接口。产品收益率均为模拟参数，不构成真实收益或保证收益。
    user_id 表示“可信调用方为该请求选择的银行客户”，由 Agent 运行时按已绑定身份注入，
    不是登录态：模型不得自行指定身份，服务端仍会校验资源归属（不属于本人按 1004 拒绝）。
servers:
  - url: http://localhost:8000/api
    description: 本地开发
tags:
  - name: Accounts
  - name: Contacts
  - name: Transfers
  - name: Bills
  - name: Products
  - name: Holdings
  - name: Wealth
  - name: Cards
  - name: Subscriptions
  - name: Birthdays
  - name: Clock
  - name: Audit
  - name: Security
  - name: System
paths:
  /cards/actions:
    post:
      tags: [Cards]
      summary: 准备不可变的卡片操作，等待用户授权
      description: |
        只接受本人资源。参数按 operation 校验，准备阶段不修改银行卡。
        操作有效期为真实时间十分钟；同一用户和幂等键重放同一记录，换参数拒绝。
        apply 参数为 card_type 和可选 account_id；其他操作需要 card_id。
        update_limit 可提供 daily_limit_minor 或 monthly_limit_minor，未提供项保留原值。
        update_restrictions 提供 mode(add/remove/replace) 与 merchants 字符串数组。
      operationId: prepareCardAction
      parameters: [{name: Idempotency-Key, in: header, required: true, schema: {type: string, minLength: 1, maxLength: 128}}]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              additionalProperties: false
              required: [user_id, operation, parameters]
              properties:
                user_id: {type: string, minLength: 1}
                operation: {type: string, enum: [apply, update_limit, update_restrictions, freeze, unfreeze, report_loss, release_loss, change_password]}
                parameters: {type: object}
      responses:
        '200': {description: 待授权操作（action_id、before、parameters、permission_level、expires_at、status、verification_failures）, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
        '404': {$ref: '#/components/responses/NotFound'}
        '409': {$ref: '#/components/responses/BusinessError'}
        '422': {$ref: '#/components/responses/ValidationError'}
  /cards/actions/{action_id}/password/authorize:
    post:
      tags: [Cards]
      summary: 独立银行卡交易密码验证接口（模拟银行，非模型工具）
      description: |
        用于 apply 的初始密码设置和 change_password 的交易密码修改，均须显式确认。
        apply 不接收 old_password，要求 new_password、confirm_password 为相同的六位 ASCII 数字；验证码按现有分级要求，提供的验证码也必须正确。
        change_password 必须提交正确旧交易密码和模拟验证码。
        密码为六位 ASCII 数字，新密码与确认密码一致且不同于旧密码。
        验证通过后只在银行私有字段暂存带随机盐的 scrypt 哈希，不立即修改当前密码；随后调用 execute 原子更新。申请时密码、卡片与申请订单必须在同一事务中创建，没有已验证的私有哈希不得发卡；不会回退到演示默认密码。
        旧密码和验证码失败按卡片分别累计，不能通过新建操作清零；任一累计三次锁定该卡改密验证十五分钟并取消当前操作。
        错误凭据返回 200 与 verification_passed=false、credential_error(old_password/verification_code/locked)、remaining_attempts；格式错误返回 422。
        取消使用通用 authorize，不需要密码。公开响应、参数快照和审计不包含密码或哈希。user_id 由可信 Agent 服务取得；本服务仍为内部 Mock。
      operationId: authorizeCardPassword
      parameters: [{name: action_id, in: path, required: true, schema: {type: string}}]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              additionalProperties: false
              required: [user_id, decision, new_password, confirm_password]
              properties:
                user_id: {type: string, minLength: 1}
                decision: {type: string, enum: [confirm, cancel]}
                verification_code: {type: string, maxLength: 32, writeOnly: true}
                old_password: {type: string, pattern: '^[0-9]{6}$', writeOnly: true}
                new_password: {type: string, pattern: '^[0-9]{6}$', writeOnly: true}
                confirm_password: {type: string, pattern: '^[0-9]{6}$', writeOnly: true}
      responses:
        '200': {description: 不含密码及哈希的授权状态或验证失败结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
        '404': {$ref: '#/components/responses/NotFound'}
        '409': {$ref: '#/components/responses/BusinessError'}
        '422': {$ref: '#/components/responses/ValidationError'}
  /cards/actions/{action_id}/authorize:
    post:
      tags: [Cards]
      summary: 独立用户交互接口：确认、取消或模拟验证
      description: |
        本接口不作为模型工具开放。黄色操作需 confirm；红色操作还需正确的 MOCK_MFA_CODE（默认 123456）。
        验证错误仍返回 200，但 verification_passed=false，并累计失败次数，三次后取消。
        取消不需要验证码。银行服务仍是无登录的内部 Mock，调用方须在 Agent 服务校验会话身份。
      operationId: authorizeCardAction
      parameters: [{name: action_id, in: path, required: true, schema: {type: string}}]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              additionalProperties: false
              required: [user_id, decision]
              properties:
                user_id: {type: string, minLength: 1}
                decision: {type: string, enum: [confirm, cancel]}
                verification_code: {type: [string, 'null'], maxLength: 32}
      responses:
        '200': {description: 授权状态及验证结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
        '404': {$ref: '#/components/responses/NotFound'}
        '409': {$ref: '#/components/responses/BusinessError'}
        '422': {$ref: '#/components/responses/ValidationError'}
  /cards/actions/{action_id}/execute:
    post:
      tags: [Cards]
      summary: 执行已授权的不可变卡片操作
      description: |
        按用户归属、授权、有效期和卡片快照校验后执行。参数来自准备记录，不接受确认标记或新参数。
        卡片变化、订单、成功审计与完成状态在同一事务提交。重复调用返回保存的结果，replayed=true。
      operationId: executeCardAction
      parameters: [{name: action_id, in: path, required: true, schema: {type: string}}]
      requestBody:
        required: true
        content:
          application/json:
            schema: {type: object, additionalProperties: false, required: [user_id], properties: {user_id: {type: string, minLength: 1}}}
      responses:
        '200': {description: 实际卡片结果、action_id 与 replayed, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
        '404': {$ref: '#/components/responses/NotFound'}
        '409': {$ref: '#/components/responses/BusinessError'}
        '422': {$ref: '#/components/responses/ValidationError'}
  /accounts:
    get:
      tags: [Accounts]
      summary: 查询用户账户列表
      operationId: listAccounts
      parameters: [$ref: '#/components/parameters/UserId']
      responses:
        '200': {$ref: '#/components/responses/AccountsResponse'}
        '404': {$ref: '#/components/responses/NotFound'}
  /accounts/{account_id}:
    get:
      tags: [Accounts]
      summary: 查询账户详情
      operationId: getAccount
      parameters: [$ref: '#/components/parameters/AccountId', $ref: '#/components/parameters/UserId']
      responses:
        '200': {$ref: '#/components/responses/AccountResponse'}
        '404': {$ref: '#/components/responses/NotFound'}
  /accounts/{account_id}/balance:
    get:
      tags: [Accounts]
      summary: 查询账户余额
      operationId: getAccountBalance
      parameters: [$ref: '#/components/parameters/AccountId', $ref: '#/components/parameters/UserId']
      responses:
        '200':
          description: 余额
          content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}, example: {code: 0, message: success, data: {account_id: a_zhang_current, balance_minor: 1250050, available_minor: 1250050, currency: CNY}}}}
        '404': {$ref: '#/components/responses/NotFound'}
  /accounts/balance:
    get:
      tags: [Accounts]
      summary: 查询所有账户余额
      operationId: listBalances
      parameters: [$ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/AccountsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /accounts/{account_id}/transactions:
    get:
      tags: [Accounts]
      summary: 查询账户流水
      operationId: listAccountTransactions
      parameters: [$ref: '#/components/parameters/AccountId', $ref: '#/components/parameters/UserId', $ref: '#/components/parameters/StartDate', $ref: '#/components/parameters/EndDate', $ref: '#/components/parameters/Page', $ref: '#/components/parameters/PageSize', {name: transaction_type, in: query, schema: {type: string}}, {name: category, in: query, schema: {type: string}}]
      responses: {'200': {$ref: '#/components/responses/TransactionsResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /accounts/transactions:
    get:
      tags: [Accounts]
      summary: 查询用户全部流水
      operationId: listTransactions
      parameters: [$ref: '#/components/parameters/UserId', $ref: '#/components/parameters/StartDate', $ref: '#/components/parameters/EndDate', $ref: '#/components/parameters/Page', $ref: '#/components/parameters/PageSize', {name: transaction_type, in: query, schema: {type: string}}, {name: category, in: query, schema: {type: string}}]
      responses: {'200': {$ref: '#/components/responses/TransactionsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /contacts:
    get:
      tags: [Contacts]
      summary: 搜索联系人
      operationId: searchContacts
      parameters: [$ref: '#/components/parameters/UserId', {name: q, in: query, description: 姓名、手机号或备注，普通子串匹配；不传则返回全部, schema: {type: string, maxLength: 100}}]
      responses: {'200': {$ref: '#/components/responses/ContactsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /mock-products:
    get:
      tags: [Birthdays]
      summary: 查询生日 mock 商品
      operationId: listMockProducts
      responses:
        '409': {$ref: '#/components/responses/BusinessError'}
        '200':
          description: 固定鲜花和蛋糕商品
          content:
            application/json:
              schema: {$ref: '#/components/schemas/ApiResponse'}
              example:
                code: 0
                message: success
                data:
                  - product_code: flowers
                    name: 模拟鲜花
                    price_minor: 19900
                    currency: CNY
                  - product_code: cake
                    name: 模拟蛋糕
                    price_minor: 12900
                    currency: CNY
  /transfers:
    post:
      tags: [Transfers]
      summary: 发起即时转账
      operationId: createTransfer
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/TransferRequest'}, example: {user_id: u_zhang, from_account_id: a_zhang_current, to_account_id: a_partner_current, amount_minor: 20000, remark: 生活费, confirm: true, mfa_code: '123456', idempotent_key: transfer-001}}}}
      responses: {'200': {$ref: '#/components/responses/TransferResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /transfers/scheduled:
    get:
      tags: [Transfers]
      summary: 查询定时转账任务
      operationId: listScheduledTransfers
      parameters: [$ref: '#/components/parameters/UserId', {name: status, in: query, schema: {type: string, enum: [scheduled, running, completed, failed, cancelled, expired]}}]
      responses: {'200': {$ref: '#/components/responses/TasksResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
    post:
      tags: [Transfers]
      summary: 创建定时转账
      operationId: createScheduledTransfer
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/ScheduledTransferRequest'}}}}
      responses: {'201': {$ref: '#/components/responses/TaskResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /transfers/scheduled/{task_id}:
    get:
      tags: [Transfers]
      summary: 查询定时转账详情
      operationId: getScheduledTransfer
      parameters: [$ref: '#/components/parameters/TaskId', $ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/TaskResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /transfers/scheduled/{task_id}/cancel:
    post:
      tags: [Transfers]
      summary: 取消定时转账
      operationId: cancelScheduledTransfer
      parameters: [$ref: '#/components/parameters/TaskId', $ref: '#/components/parameters/IdempotencyKey']
      requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/ConfirmRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/TaskResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /transfers/aa:
    post:
      tags: [Transfers]
      summary: 创建 AA 收款请求
      operationId: createAaRequest
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/AaCreateRequest'}}}}
      responses: {'201': {$ref: '#/components/responses/AaResponse'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /transfers/aa/{aa_request_id}:
    get:
      tags: [Transfers]
      summary: 查询 AA 请求
      operationId: getAaRequest
      parameters: [$ref: '#/components/parameters/AaId', $ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/AaResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /transfers/aa/{aa_request_id}/pay:
    post:
      tags: [Transfers]
      summary: 支付 AA 分摊项
      operationId: payAaParticipant
      parameters: [$ref: '#/components/parameters/AaId', $ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/AaPayRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/TransactionResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /transfers/aa/{aa_request_id}/cancel:
    post:
      tags: [Transfers]
      summary: 取消 AA 请求
      operationId: cancelAaRequest
      parameters: [$ref: '#/components/parameters/AaId']
      responses: {'200': {$ref: '#/components/responses/AaResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /bills/month:
    get:
      tags: [Bills]
      summary: 查询月度账单
      operationId: getMonthlyBill
      parameters: [$ref: '#/components/parameters/UserId', {name: month, in: query, required: true, schema: {type: string, pattern: '^\\d{4}-\\d{2}$'}, example: '2026-09'}]
      responses: {'200': {$ref: '#/components/responses/BillResponse'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /bills/year:
    get:
      tags: [Bills]
      summary: 查询年度账单
      operationId: getYearlyBill
      parameters: [$ref: '#/components/parameters/UserId', {name: year, in: query, required: true, schema: {type: integer, minimum: 2000, maximum: 2100}, example: 2026}]
      responses: {'200': {$ref: '#/components/responses/BillResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /bills/categories:
    get:
      tags: [Bills]
      summary: 查询消费分类统计
      operationId: getBillCategories
      parameters: [$ref: '#/components/parameters/UserId', $ref: '#/components/parameters/StartDate', $ref: '#/components/parameters/EndDate']
      responses: {'200': {$ref: '#/components/responses/BillResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /bills/analyze:
    post:
      tags: [Bills]
      summary: 分析账单异常
      operationId: analyzeBills
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/BillAnalyzeRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/AnalysisResponse'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /products:
    get:
      tags: [Products]
      summary: 查询理财产品
      operationId: listProducts
      parameters: [{name: risk_level, in: query, schema: {type: string, enum: [low, medium, high]}}, {name: product_type, in: query, schema: {type: string}}]
      responses: {'200': {$ref: '#/components/responses/ProductsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /products/{product_id}:
    get:
      tags: [Products]
      summary: 查询理财产品详情
      operationId: getProduct
      parameters: [$ref: '#/components/parameters/ProductId']
      responses: {'200': {$ref: '#/components/responses/ProductResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /products/recommend:
    post:
      tags: [Products]
      summary: 推荐理财产品
      operationId: recommendProducts
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/ProductRecommendRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/ProductsResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /products/compare:
    post:
      tags: [Products]
      summary: 对比理财产品
      operationId: compareProducts
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/ProductCompareRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/ProductsResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /products/purchase:
    post:
      tags: [Products]
      summary: 申购理财产品
      operationId: purchaseProduct
      description: |
        理财写固定需要 confirm=true 且 mfa_code 等于配置的演示验证码（mock_mfa_code），
        不按金额分级，小额也不能免验证，验证失败返回 1008；连续 5 次错码后该资金工具被锁定，锁定期间返回 1009（银行侧 message 固定为英文 token locked）。此外必须有本人有效测评且产品
        风险不高于测评等级；term_months 缺省取产品期限，显式传入必须与产品期限一致，
        不允许自选缩短。幂等键取自 Idempotency-Key 请求头或请求体 idempotent_key；
        同键同业务参数返回原回执，同键不同参数拒绝（1005）。
        部署顺序：先部署对话侧 backend，再部署 bank_backend；反向部署时旧的对话侧会把未知的
        1008/1009 按 409 归为通用客户端错误。
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/PurchaseRequest'}}}}
      responses: {'201': {$ref: '#/components/responses/PurchaseResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /holdings:
    get:
      tags: [Holdings]
      summary: 查询持仓
      operationId: listHoldings
      parameters: [$ref: '#/components/parameters/UserId', {name: status, in: query, schema: {type: string, enum: [holding, partial, redeemed]}}]
      responses: {'200': {$ref: '#/components/responses/HoldingsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /holdings/{holding_id}:
    get:
      tags: [Holdings]
      summary: 查询持仓详情
      operationId: getHolding
      parameters: [$ref: '#/components/parameters/HoldingId', $ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/HoldingResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /holdings/{holding_id}/simulate-interest:
    post:
      tags: [Holdings]
      summary: 模拟收益
      operationId: simulateInterest
      parameters: [$ref: '#/components/parameters/HoldingId']
      requestBody: {content: {application/json: {schema: {type: object, properties: {as_of: {type: string, format: date}}}}}}
      responses: {'200': {$ref: '#/components/responses/HoldingResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /products/redeem:
    post:
      tags: [Holdings]
      summary: 赎回持仓
      operationId: redeemProduct
      description: |
        赎回本人持仓，不要求重新测评。固定需要 confirm=true 且 mfa_code 等于配置的
        演示验证码（mock_mfa_code），验证失败返回 1008；连续 5 次错码后该资金工具被锁定，锁定期间返回 1009。
        部署顺序：先部署对话侧 backend，再部署 bank_backend；反向部署时旧的对话侧会把未知的
        1008/1009 按 409 归为通用客户端错误。金额不得超过可赎回额：
        flexible 产品按当前价值，maturity_only 产品在到期日之前为 0。部分赎回按
        floor(本金 × 赎回额 ÷ 当前价值) 释放本金，其余为实现收益并写入订单 details。
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/RedeemRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/RedeemResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /wealth/risk-assessments:
    post:
      tags: [Wealth]
      summary: 保存风险测评
      operationId: submitRiskAssessment
      description: |
        服务端按 wealth-risk-v1 规则评分并追加一条测评记录，不覆盖历史。
        answers 是五题编号 q1..q5 到选项 0/1/2 的对象：五题必须齐全，选项只能是 0/1/2，
        bool 与字符串都不算数值。总分 0-3 为 low、4-7 为 medium、8-10 为 high，
        q1 为 0 时等级上限为 low；有效期 30 天，以银行模拟时钟为准。
        最终等级、分数和有效期由银行计算，请求里出现 risk_level、score 等字段直接拒绝（1012）。
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/RiskAssessmentRequest'}}}}
      responses: {'201': {$ref: '#/components/responses/RiskAssessmentResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /wealth/risk-assessments/latest:
    get:
      tags: [Wealth]
      summary: 查询最近一次风险测评
      operationId: getLatestRiskAssessment
      description: |
        返回本人最近一条测评（含已过期记录），valid 由银行模拟时钟派生：到达 expires_at
        即为过期，过期不删除记录。没有测评时 data 为 null。
      parameters: [$ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/RiskAssessmentResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /wealth/orders/by-idempotency-key:
    get:
      tags: [Wealth]
      summary: 按幂等键核实理财订单
      operationId: findWealthOrderByKey
      description: |
        按 (user_id, operation_type, idempotent_key) 三元组查询原回执并核实关联理财订单。
        operation_type 只允许 purchase_product／redeem_product，不借该接口读其他域的幂等结果。
        查不到返回 found=false 且不编造订单——它不等于在途请求一定没有提交。
        该静态路径必须先于 /wealth/orders/{order_id} 注册。
      parameters: [$ref: '#/components/parameters/UserId', {name: operation_type, in: query, required: true, schema: {type: string, enum: [purchase_product, redeem_product]}}, {name: idempotent_key, in: query, required: true, schema: {type: string, minLength: 1, maxLength: 128}}]
      responses: {'200': {$ref: '#/components/responses/WealthOrderLookupResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /wealth/orders/{order_id}:
    get:
      tags: [Wealth]
      summary: 查询本人理财订单
      operationId: getWealthOrder
      description: |
        只返回本人（user_id）的 product_purchase／product_redeem 订单，包含账户、金额、
        持仓、状态、资金流水号和执行结果。订单不属于本人或不是理财订单时统一按 1004 拒绝，
        不通过猜 ID 泄露他人订单。order_id 是业务订单号，transaction_id 是资金流水号，两者不混用。
      parameters: [$ref: '#/components/parameters/UserId', $ref: '#/components/parameters/OrderId']
      responses: {'200': {$ref: '#/components/responses/WealthOrderResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /cards:
    get:
      tags: [Cards]
      summary: 查询卡片列表
      operationId: listCards
      parameters: [$ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/CardsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /cards/{card_id}:
    get:
      tags: [Cards]
      summary: 查询卡片详情
      operationId: getCard
      parameters: [$ref: '#/components/parameters/CardId', $ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /cards/apply:
    post:
      tags: [Cards]
      summary: 旧直接申请入口（已停用，不再发卡；请使用准备、独立密码授权、执行流程）
      deprecated: true
      operationId: applyCard
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/CardApplyRequest'}}}}
      responses: {'422': {$ref: '#/components/responses/ValidationError'}}
  /cards/{card_id}/limit:
    post:
      tags: [Cards]
      summary: 修改卡片限额
      operationId: updateCardLimit
      parameters: [$ref: '#/components/parameters/CardId']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/CardLimitRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /cards/{card_id}/freeze:
    post:
      tags: [Cards]
      summary: 挂失或冻结卡片
      operationId: freezeCard
      parameters: [$ref: '#/components/parameters/CardId']
      requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/ConfirmRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /cards/{card_id}/unfreeze:
    post:
      tags: [Cards]
      summary: 解挂或解冻卡片
      operationId: unfreezeCard
      parameters: [$ref: '#/components/parameters/CardId']
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /cards/{card_id}/block:
    post:
      tags: [Cards]
      summary: 永久阻断卡片
      operationId: blockCard
      parameters: [$ref: '#/components/parameters/CardId']
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /cards/{card_id}/restrictions:
    get:
      tags: [Cards]
      summary: 查询卡片限额和黑名单
      operationId: getCardRestrictions
      parameters: [$ref: '#/components/parameters/CardId']
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
    post:
      tags: [Cards]
      summary: 设置卡片限额和黑名单
      operationId: setCardRestrictions
      parameters: [$ref: '#/components/parameters/CardId']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/CardRestrictionRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/CardResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /subscriptions:
    get:
      tags: [Subscriptions]
      summary: 查询订阅列表
      operationId: listSubscriptions
      parameters: [$ref: '#/components/parameters/UserId', {name: status, in: query, schema: {type: string, enum: [active, paused, cancelled]}}]
      responses: {'200': {$ref: '#/components/responses/SubscriptionsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
    post:
      tags: [Subscriptions]
      summary: 创建订阅
      operationId: createSubscription
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/SubscriptionRequest'}}}}
      responses: {'201': {$ref: '#/components/responses/SubscriptionResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /subscriptions/{subscription_id}:
    get:
      tags: [Subscriptions]
      summary: 查询订阅详情
      operationId: getSubscription
      parameters: [$ref: '#/components/parameters/SubscriptionId', $ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/SubscriptionResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /subscriptions/{subscription_id}/cancel:
    post:
      tags: [Subscriptions]
      summary: 取消订阅
      operationId: cancelSubscription
      parameters: [$ref: '#/components/parameters/SubscriptionId']
      responses: {'200': {$ref: '#/components/responses/SubscriptionResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /subscriptions/{subscription_id}/pause:
    post:
      tags: [Subscriptions]
      summary: 暂停订阅
      operationId: pauseSubscription
      parameters: [$ref: '#/components/parameters/SubscriptionId']
      responses: {'200': {$ref: '#/components/responses/SubscriptionResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /subscriptions/{subscription_id}/resume:
    post:
      tags: [Subscriptions]
      summary: 恢复订阅
      operationId: resumeSubscription
      parameters: [$ref: '#/components/parameters/SubscriptionId']
      responses: {'200': {$ref: '#/components/responses/SubscriptionResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /subscriptions/{subscription_id}/charge:
    post:
      tags: [Subscriptions]
      summary: 模拟本期订阅扣款
      operationId: chargeSubscription
      parameters: [$ref: '#/components/parameters/SubscriptionId', $ref: '#/components/parameters/IdempotencyKey']
      responses: {'200': {$ref: '#/components/responses/ChargeResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /subscriptions/run-due:
    post:
      tags: [Subscriptions]
      summary: 批量运行到期订阅
      description: 与 /clock/run-tasks 使用同一任务执行逻辑，只处理订阅任务。
      operationId: runDueSubscriptions
      responses: {'200': {$ref: '#/components/responses/TaskRunResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /birthdays:
    get:
      tags: [Birthdays]
      summary: 查询生日联系人
      operationId: listBirthdays
      parameters: [$ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/ContactsResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /birthdays/orders:
    post:
      tags: [Birthdays]
      summary: 创建生日礼物订单
      operationId: createBirthdayOrder
      parameters: [$ref: '#/components/parameters/IdempotencyKey']
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/BirthdayOrderRequest'}}}}
      responses: {'201': {$ref: '#/components/responses/OrderResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /birthdays/orders/{order_id}:
    get:
      tags: [Birthdays]
      summary: 查询生日订单
      operationId: getBirthdayOrder
      parameters: [$ref: '#/components/parameters/OrderId', $ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/OrderResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /birthdays/orders/{order_id}/cancel:
    post:
      tags: [Birthdays]
      summary: 取消生日订单
      operationId: cancelBirthdayOrder
      parameters: [$ref: '#/components/parameters/OrderId']
      responses: {'200': {$ref: '#/components/responses/OrderResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /clock:
    get:
      tags: [Clock]
      summary: 查询模拟时钟
      operationId: getClock
      responses: {'200': {$ref: '#/components/responses/ClockResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
    post:
      tags: [Clock]
      summary: 设置或向前拨模拟时钟
      operationId: setClock
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/ClockRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/ClockResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /clock/run-tasks:
    post:
      tags: [Clock]
      summary: 运行到期任务
      operationId: runDueTasks
      requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/TaskRunRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/TaskRunResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /audit:
    get:
      tags: [Audit]
      summary: 查询审计日志
      operationId: listAuditLogs
      parameters: [$ref: '#/components/parameters/UserId', {name: action, in: query, schema: {type: string}}, {name: resource_type, in: query, schema: {type: string}}, $ref: '#/components/parameters/Page', $ref: '#/components/parameters/PageSize']
      responses: {'200': {$ref: '#/components/responses/AuditResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /audit/{audit_id}:
    get:
      tags: [Audit]
      summary: 查询审计详情
      operationId: getAuditLog
      parameters: [{name: audit_id, in: path, required: true, schema: {type: string}}]
      responses: {'200': {$ref: '#/components/responses/AuditDetailResponse'}, '404': {$ref: '#/components/responses/NotFound'}}
  /security/status:
    get:
      tags: [Security]
      summary: 查询模拟安全状态
      operationId: getSecurityStatus
      parameters: [$ref: '#/components/parameters/UserId']
      responses: {'200': {$ref: '#/components/responses/SecurityResponse'}, '404': {$ref: '#/components/responses/NotFound'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /security/reset-lock:
    post:
      tags: [Security]
      summary: 清除模拟安全锁定
      operationId: resetSecurityLock
      requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/UserRequest'}}}}
      responses: {'200': {$ref: '#/components/responses/SecurityResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /seed/reset:
    post:
      tags: [System]
      summary: 重置固定种子
      operationId: resetSeed
      requestBody:
        required: false
        content: {application/json: {schema: {$ref: '#/components/schemas/SeedResetRequest'}}}
      responses: {'200': {$ref: '#/components/responses/HealthResponse'}, '409': {$ref: '#/components/responses/BusinessError'}, '422': {$ref: '#/components/responses/ValidationError'}}
  /seed/demo:
    post:
      tags: [System]
      summary: 初始化完整演示数据
      description: 清空业务数据后初始化所有业务域的固定假数据，包括持仓、订单、定时任务、AA、订阅、审计和幂等记录。
      operationId: buildDemoData
      responses: {'200': {$ref: '#/components/responses/DemoDataResponse'}, '409': {$ref: '#/components/responses/BusinessError'}}
  /health:
    get:
      tags: [System]
      summary: 最小就绪检查（容器探针用）
      description: 只确认库可达、核心表可读、模拟时钟是否已初始化，不校验任何余额，因此正常交易或新客户开户不会让它返回 503。无需服务凭证。
      operationId: getHealth
      responses: {'200': {$ref: '#/components/responses/ReadyResponse'}, '503': {$ref: '#/components/responses/BusinessError'}}
  /health/deep:
    get:
      tags: [System]
      summary: 完整健康检查（含账户对账）
      description: 要求种子已写入，并逐账户校验 available_minor 等于 balance_minor。排障用，不适合当容器探针。
      operationId: getHealthDeep
      responses: {'200': {$ref: '#/components/responses/HealthResponse'}, '503': {$ref: '#/components/responses/BusinessError'}}
  /pool/provision:
    post:
      tags: [System]
      summary: 补一个注册客户池名额
      description: 注册领走一个客户之后由后端调用，让客户池长回一格；号码按当前最大池号递增，已存在的号跳过。返回 customer_id 为 null 表示本次没补上（并发抢号用尽重试），调用方应忽略。不需要运维口令。
      operationId: provisionPoolCustomer
      responses: {'201': {$ref: '#/components/responses/PoolProvisionResponse'}, '401': {$ref: '#/components/responses/ServiceUnauthorized'}}
components:
  parameters:
    UserId: {name: user_id, in: query, required: true, description: '银行客户 ID，由可信调用方（Agent 运行时）按已绑定身份注入。必填：缺失返回 1013，不再回落到任何默认客户。服务端校验资源归属，不属于本人按 1004 处理。', schema: {type: string}}
    AccountId: {name: account_id, in: path, required: true, schema: {type: string}}
    TaskId: {name: task_id, in: path, required: true, schema: {type: string}}
    AaId: {name: aa_request_id, in: path, required: true, schema: {type: string}}
    ProductId: {name: product_id, in: path, required: true, schema: {type: string}}
    HoldingId: {name: holding_id, in: path, required: true, schema: {type: string}}
    CardId: {name: card_id, in: path, required: true, schema: {type: string}}
    SubscriptionId: {name: subscription_id, in: path, required: true, schema: {type: string}}
    OrderId: {name: order_id, in: path, required: true, schema: {type: string}}
    IdempotencyKey: {name: Idempotency-Key, in: header, required: false, schema: {type: string, minLength: 8, maxLength: 128}}
    StartDate: {name: start_date, in: query, schema: {type: string, format: date}}
    EndDate: {name: end_date, in: query, schema: {type: string, format: date}}
    Page: {name: page, in: query, schema: {type: integer, minimum: 1, default: 1}}
    PageSize: {name: page_size, in: query, schema: {type: integer, minimum: 1, maximum: 100, default: 20}}
  schemas:
    ApiResponse: {type: object, required: [code, message, data], properties: {code: {type: integer}, message: {type: string}, data: {}}}
    ErrorResponse: {type: object, required: [code, message, data], properties: {code: {type: integer}, message: {type: string}, data: {type: 'null'}}}
    DemoData: {type: object, required: [run_id, seed_version, current_time, counts, balances, examples], properties: {run_id: {type: string, format: uuid}, seed_version: {type: string}, current_time: {type: string, format: date-time}, counts: {type: object, additionalProperties: {type: integer, minimum: 1}}, balances: {type: object, additionalProperties: {type: integer, minimum: 0}}, examples: {type: object, additionalProperties: {type: string}}}}
    Account: {type: object, required: [account_id, user_id, account_type, balance_minor, available_minor, currency, status], properties: {account_id: {type: string}, user_id: {type: string}, account_type: {type: string, enum: [current, fixed]}, balance_minor: {type: integer, minimum: 0}, available_minor: {type: integer, minimum: 0}, currency: {type: string, const: CNY}, interest_rate: {type: number}, status: {type: string, enum: [active, closed]}}}
    Transaction: {type: object, required: [transaction_id, account_id, amount_minor, direction, transaction_type, status, created_at], properties: {transaction_id: {type: string}, account_id: {type: string}, counterparty_account_id: {type: string, nullable: true}, amount_minor: {type: integer, minimum: 1}, direction: {type: string, enum: [debit, credit]}, transaction_type: {type: string}, category: {type: string, nullable: true}, status: {type: string, enum: [pending, completed, failed, cancelled]}, remark: {type: string}, reference_id: {type: string, nullable: true}, created_at: {type: string, format: date-time}}}
    Contact: {type: object, properties: {contact_id: {type: string}, user_id: {type: string}, name: {type: string}, phone: {type: string}, remark: {type: string}, recipient_user_id: {type: string}, recipient_account_id: {type: string}, birthday: {type: string, format: date, nullable: true}}}
    TransferRequest: {type: object, required: [from_account_id, to_account_id, amount_minor], properties: {user_id: {type: string, description: 必填，缺失返回 1013}, from_account_id: {type: string}, to_account_id: {type: string}, amount_minor: {type: integer, minimum: 1, maximum: 100000000}, remark: {type: string, maxLength: 200}, confirm: {type: boolean, default: false}, mfa_code: {type: string, nullable: true}, idempotent_key: {type: string}}}
    ScheduledTransferRequest: {allOf: [{$ref: '#/components/schemas/TransferRequest'}, {type: object, required: [execute_at], properties: {execute_at: {type: string, format: date-time}}}]}
    ConfirmRequest: {type: object, properties: {confirm: {type: boolean, default: true}, mfa_code: {type: string, nullable: true}}}
    Task: {type: object, properties: {task_id: {type: string}, user_id: {type: string}, task_type: {type: string}, execute_at: {type: string, format: date-time}, status: {type: string, enum: [scheduled, running, completed, failed, cancelled, expired]}, amount_minor: {type: integer}, result: {}, error_code: {type: string, nullable: true}}}
    AaParticipant: {type: object, properties: {id: {type: string}, name: {type: string}, phone: {type: string}, account_id: {type: string, nullable: true}, amount_minor: {type: integer}, status: {type: string, enum: [pending, paid, refused]}, transaction_id: {type: string, nullable: true}}}
    AaCreateRequest: {type: object, required: [total_amount_minor, participants], properties: {user_id: {type: string, default: u_zhang}, from_account_id: {type: string}, total_amount_minor: {type: integer, minimum: 1}, participants: {type: array, minItems: 2, items: {type: object, required: [name], properties: {name: {type: string}, phone: {type: string}, account_id: {type: string}}}}, remark: {type: string}, idempotent_key: {type: string}}}
    AaPayRequest: {type: object, required: [participant_index, confirm], properties: {participant_index: {type: integer, minimum: 0}, confirm: {type: boolean, const: true}}}
    AaRequest: {type: object, properties: {aa_request_id: {type: string}, total_amount_minor: {type: integer}, status: {type: string}, participants: {type: array, items: {$ref: '#/components/schemas/AaParticipant'}}}}
    Bill: {type: object, properties: {period: {type: string}, income_minor: {type: integer}, expense_minor: {type: integer}, consumption_minor: {type: integer}, categories: {type: object, additionalProperties: {type: integer}}}}
    BillAnalyzeRequest: {type: object, required: [start_date, end_date], properties: {user_id: {type: string}, start_date: {type: string, format: date}, end_date: {type: string, format: date}, category: {type: string}, min_amount_minor: {type: integer}, keywords: {type: array, items: {type: string}}}}
    Finding: {type: object, properties: {rule_id: {type: string}, transaction_ids: {type: array, items: {type: string}}, amount_minor: {type: integer}, explanation: {type: string}}}
    Product: {type: object, properties: {product_id: {type: string}, name: {type: string}, product_type: {type: string}, min_amount_minor: {type: integer}, max_amount_minor: {type: integer}, expected_return: {type: number, description: 模拟年化参数，不代表真实收益或保证收益}, risk_level: {type: string, enum: [low, medium, high]}, duration_months: {type: integer}, redeem_rule: {type: string, enum: [flexible, maturity_only], description: flexible 随时可赎回；maturity_only 只在到期后可赎回}, status: {type: string, enum: [available, sold_out]}}}
    ProductRecommendRequest: {type: object, properties: {user_id: {type: string}, risk_level: {type: string}, amount_minor: {type: integer}}}
    ProductCompareRequest: {type: object, required: [product_ids], properties: {product_ids: {type: array, minItems: 2, items: {type: string}}}}
    PurchaseRequest: {type: object, required: [from_account_id, product_id, amount_minor], properties: {user_id: {type: string}, from_account_id: {type: string, description: 必须是本人活期账户}, product_id: {type: string}, amount_minor: {type: integer, minimum: 1}, term_months: {type: integer, minimum: 0, description: 缺省取产品期限；显式传入必须与产品期限一致}, confirm: {type: boolean, description: 理财写固定要求 true}, mfa_code: {type: string, description: 必须等于配置的演示验证码}, idempotent_key: {type: string}}}
    Holding: {type: object, properties: {holding_id: {type: string}, user_id: {type: string}, product_id: {type: string}, product_name: {type: string, nullable: true}, source_account_id: {type: string}, principal_minor: {type: integer}, current_value_minor: {type: integer}, accrued_interest_minor: {type: integer, description: current_value_minor − principal_minor，即持有收益}, realized_income_minor: {type: integer, description: 汇总本人该持仓已完成赎回订单的 interest_redeemed_minor}, redeemable_minor: {type: integer, description: 可赎回额；flexible 取当前价值，maturity_only 到期前为 0，已赎回为 0}, expected_return: {type: number}, status: {type: string, enum: [holding, partial, redeemed]}, purchased_at: {type: string, format: date-time}, maturity_date: {type: string, format: date, nullable: true}}}
    RedeemRequest: {type: object, required: [holding_id, amount_minor], properties: {user_id: {type: string}, holding_id: {type: string}, amount_minor: {type: integer, minimum: 1}, to_account_id: {type: string, description: 必须是本人账户，省略时使用持仓来源账户}, confirm: {type: boolean, description: 理财写固定要求 true}, mfa_code: {type: string, description: 必须等于配置的演示验证码}, idempotent_key: {type: string}}}
    RiskAnswers: {type: object, additionalProperties: false, required: [q1, q2, q3, q4, q5], description: '五题编号到选项的映射：q1 本金损失接受度、q2 投资经验、q3 收入稳定性、q4 计划投资期限、q5 短期资金需求', properties: {q1: {type: integer, enum: [0, 1, 2]}, q2: {type: integer, enum: [0, 1, 2]}, q3: {type: integer, enum: [0, 1, 2]}, q4: {type: integer, enum: [0, 1, 2]}, q5: {type: integer, enum: [0, 1, 2]}}}
    RiskAssessmentRequest: {type: object, additionalProperties: false, required: [answers], properties: {user_id: {type: string, default: u_zhang}, answers: {$ref: '#/components/schemas/RiskAnswers'}}}
    RiskAssessment: {type: object, properties: {assessment_id: {type: string}, user_id: {type: string}, answers: {$ref: '#/components/schemas/RiskAnswers'}, score: {type: integer, minimum: 0, maximum: 10}, risk_level: {type: string, enum: [low, medium, high]}, rule_version: {type: string, const: wealth-risk-v1}, assessed_at: {type: string, format: date-time}, expires_at: {type: string, format: date-time}, valid: {type: boolean, description: 由银行模拟时钟派生，过期记录仍返回但为 false}}}
    WealthOrder: {type: object, properties: {order_id: {type: string}, order_type: {type: string, enum: [product_purchase, product_redeem]}, user_id: {type: string}, product_id: {type: string, nullable: true}, product_name: {type: string, nullable: true}, holding_id: {type: string, nullable: true}, from_account_id: {type: string, nullable: true}, to_account_id: {type: string, nullable: true}, amount_minor: {type: integer}, status: {type: string, enum: [pending, completed, failed, cancelled]}, transaction_id: {type: string, nullable: true}, details: {type: object}, created_at: {type: string, format: date-time}, updated_at: {type: string, format: date-time}}}
    WealthOrderLookup: {type: object, properties: {found: {type: boolean}, user_id: {type: string}, operation_type: {type: string}, idempotent_key: {type: string}, result: {type: object, nullable: true, description: 首次执行保存的原回执}, order: {allOf: [{$ref: '#/components/schemas/WealthOrder'}], nullable: true}, created_at: {type: string, format: date-time, nullable: true}}}
    Card: {type: object, properties: {card_id: {type: string}, user_id: {type: string}, account_id: {type: string}, card_type: {type: string}, card_number: {type: string}, status: {type: string}, daily_limit_minor: {type: integer}, monthly_limit_minor: {type: integer}, merchant_blacklist: {type: array, items: {type: string}}}}
    CardApplyRequest: {type: object, required: [card_type], properties: {user_id: {type: string}, account_id: {type: string}, card_type: {type: string, enum: [debit, credit, virtual]}, idempotent_key: {type: string}}}
    CardLimitRequest: {type: object, required: [daily_limit_minor, monthly_limit_minor], properties: {daily_limit_minor: {type: integer, minimum: 0}, monthly_limit_minor: {type: integer, minimum: 0}}}
    CardRestrictionRequest: {type: object, properties: {daily_limit_minor: {type: integer, minimum: 0}, monthly_limit_minor: {type: integer, minimum: 0}, merchant_blacklist: {type: array, items: {type: string}}}}
    SubscriptionRequest: {type: object, required: [name, merchant_name, amount_minor, frequency, from_account_id, start_date], properties: {user_id: {type: string}, name: {type: string}, merchant_name: {type: string}, amount_minor: {type: integer, minimum: 1}, frequency: {type: string, enum: [daily, weekly, monthly, yearly]}, from_account_id: {type: string}, start_date: {type: string, format: date}, auto_renew: {type: boolean, default: true}, idempotent_key: {type: string}}}
    Subscription: {type: object, properties: {subscription_id: {type: string}, name: {type: string}, merchant_name: {type: string}, amount_minor: {type: integer}, frequency: {type: string}, next_due_date: {type: string, format: date}, status: {type: string}, auto_renew: {type: boolean}, last_charge_id: {type: string, nullable: true}}}
    BirthdayOrderRequest: {type: object, required: [contact_id, product_code, from_account_id, birthday_date], properties: {user_id: {type: string}, contact_id: {type: string}, product_code: {type: string, enum: [flowers, cake]}, from_account_id: {type: string}, birthday_date: {type: string, format: date}, confirm: {type: boolean}, idempotent_key: {type: string}}}
    Order: {type: object, properties: {order_id: {type: string}, order_type: {type: string}, amount_minor: {type: integer}, status: {type: string}, task_id: {type: string, nullable: true}, transaction_id: {type: string, nullable: true}, details: {type: object}}}
    Clock: {type: object, properties: {current_time: {type: string, format: date-time}, run_id: {type: string, format: uuid}, due_task_count: {type: integer}}}
    ClockRequest: {type: object, required: [current_time], properties: {current_time: {type: string, format: date-time}}}
    TaskRunRequest: {type: object, properties: {birthday_outcome: {type: string, enum: [success, fail]}}}
    Audit: {type: object, properties: {audit_id: {type: string}, request_id: {type: string}, user_id: {type: string}, action: {type: string}, resource_type: {type: string}, resource_id: {type: string}, permission_level: {type: string}, confirm_required: {type: boolean}, mfa_required: {type: boolean}, result: {type: string}, reason: {type: string}, created_at: {type: string, format: date-time}}}
    Security: {type: object, properties: {user_id: {type: string}, tool_name: {type: string}, failure_count: {type: integer}, locked_until: {type: string, format: date-time, nullable: true}, last_reason: {type: string}}}
    UserRequest: {type: object, properties: {user_id: {type: string}}}
    SeedResetRequest: {type: object, properties: {expected_run_id: {type: string, nullable: true, description: 传入时仅当当前 run_id 匹配才重置，否则 409}}}
    TaskRunResult: {type: object, properties: {processed_count: {type: integer}, completed_count: {type: integer}, failed_count: {type: integer}, items: {type: array, items: {$ref: '#/components/schemas/Task'}}}}
    Health: {type: object, properties: {status: {type: string}, database: {type: string}, seed_version: {type: string}, run_id: {type: string}}}
  responses:
    '422': {$ref: '#/components/responses/ValidationError'}
    AccountsResponse: {description: 账户列表, content: {application/json: {schema: {allOf: [{$ref: '#/components/schemas/ApiResponse'}, {type: object, properties: {data: {type: array, items: {$ref: '#/components/schemas/Account'}}}}]}}}}
    AccountResponse: {description: 账户, content: {application/json: {schema: {allOf: [{$ref: '#/components/schemas/ApiResponse'}, {type: object, properties: {data: {$ref: '#/components/schemas/Account'}}}]}}}}
    TransactionsResponse: {description: 流水分页, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ContactsResponse: {description: 联系人, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    TransferResponse: {description: 转账结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    TransactionResponse: {description: 交易结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    TasksResponse: {description: 任务列表, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    TaskResponse: {description: 任务详情, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    AaResponse: {description: AA 请求, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    BillResponse: {description: 账单, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    AnalysisResponse: {description: 分析结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ProductsResponse: {description: 产品列表, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ProductResponse: {description: 产品详情, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    PurchaseResponse: {description: 申购结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    HoldingsResponse: {description: 持仓列表, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    HoldingResponse: {description: 持仓详情, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    RedeemResponse: {description: 赎回结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    RiskAssessmentResponse: {description: 风险测评；没有记录时 data 为 null, content: {application/json: {schema: {allOf: [{$ref: '#/components/schemas/ApiResponse'}, {type: object, properties: {data: {allOf: [{$ref: '#/components/schemas/RiskAssessment'}], nullable: true}}}]}}}}
    WealthOrderResponse: {description: 理财订单, content: {application/json: {schema: {allOf: [{$ref: '#/components/schemas/ApiResponse'}, {type: object, properties: {data: {$ref: '#/components/schemas/WealthOrder'}}}]}}}}
    WealthOrderLookupResponse: {description: 按幂等键核实结果, content: {application/json: {schema: {allOf: [{$ref: '#/components/schemas/ApiResponse'}, {type: object, properties: {data: {$ref: '#/components/schemas/WealthOrderLookup'}}}]}}}}
    CardsResponse: {description: 卡片列表, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    CardResponse: {description: 卡片, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    SubscriptionsResponse: {description: 订阅列表, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    SubscriptionResponse: {description: 订阅, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ChargeResponse: {description: 扣款, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    OrderResponse: {description: 订单, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ClockResponse: {description: 时钟, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    TaskRunResponse: {description: 任务运行结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    AuditResponse: {description: 审计列表, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    AuditDetailResponse: {description: 审计详情, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    SecurityResponse: {description: 安全状态, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    HealthResponse: {description: 健康状态, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ReadyResponse: {description: 最小就绪状态（含 seeded 字段，表示种子是否已写入）, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    PoolProvisionResponse: {description: '客户池补位结果：customer_id 为新建客户号，null 表示本次没补上', content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    ServiceUnauthorized: {description: 缺少服务凭证或凭证不正确, content: {application/json: {schema: {$ref: '#/components/schemas/ErrorResponse'}, example: {code: 1001, message: service_token_required, data: null}}}}
    OpsUnauthorized: {description: 缺少运维口令或口令不正确（仅 /seed/* 与 /clock*）, content: {application/json: {schema: {$ref: '#/components/schemas/ErrorResponse'}, example: {code: 1002, message: ops_token_required, data: null}}}}
    DemoDataResponse: {description: 完整演示数据初始化结果, content: {application/json: {schema: {$ref: '#/components/schemas/ApiResponse'}}}}
    NotFound: {description: 资源不存在, content: {application/json: {schema: {$ref: '#/components/schemas/ErrorResponse'}, example: {code: 1004, message: resource_not_found, data: null}}}}
    ValidationError: {description: 参数校验失败, content: {application/json: {schema: {$ref: '#/components/schemas/ErrorResponse'}, example: {code: 1012, message: validation_error, data: null}}}}
    BusinessError: {description: 业务执行失败, content: {application/json: {schema: {$ref: '#/components/schemas/ErrorResponse'}, example: {code: 1005, message: invalid_state, data: null}}}}
