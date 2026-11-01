# 守门人 · 银行 AI 智能体

> 一个能查、能算、能办事，但**不替用户拍板**的银行智能体。模型负责把话听懂、把方案说清楚；金额、权限、确认和审计由代码守死。

![工作台](docs/images/workbench.png)

## 这个项目解决什么问题

银行 App 的菜单越叠越深，用户真正想说的往往只有一句：「给李四转 1500」「这个月钱花哪了」「帮我把下个月的视频会员停了」。直接接一个大模型，能听懂，却不敢让它碰钱；只做传统表单，安全，但不够自然。

这个项目的做法是把两者拆开：

- **模型只做两件事**：理解意图、组织语言。
- **代码管住三件事**：能不能做、要不要确认、做完有没有留痕。
- **每一笔写操作**都先出结构化确认卡；该验密码的验密码，该验验证码的验验证码；确认之前零写入。

## 能做什么

| 场景 | 能力 |
| --- | --- |
| 智能转账 | 账户/余额查询、即时转账、**定时转账创建**、**AA 收款发起/查进度/付份额/取消**、转账记录核实；大额或日累计超限自动升级红标 MFA |
| 账单分析 | 交易查询、分类统计、月度/年度对比、邮件报告 |
| 理财 | 风险测评、产品推荐与对比、申购、持仓收益、赎回 |
| 卡片管理 | 查询、申请、日/月限额、商户黑名单、冻结/解冻、挂失/解挂、交易密码修改 |
| 订阅代扣 | 订阅识别、**续费前提醒**、取消/暂停/恢复、手动扣款 |
| 生活事件 | 生日识别、按预算挑礼物、预订单、到期执行与撤销 |
| 跨场景联动 | Supervisor 把多个领域串成任务图；后一个领域可读取前一个领域的结论继续办 |
| 飞书 IM | 官方长连、扫码绑定、文字/卡片办理业务；无需公网 IP 或入站端口 |

### 安全分级

- 🟢 **绿**：只读查询，直接执行。
- 🟡 **黄**：有副作用但可控（如小额转账、订阅取消），先出确认卡，点击确认才写。
- 🔴 **红**：金额超过日累计 1,000 元、修改交易密码、挂失、理财申购等，必须确认卡 + 当场输入验证码/密码。

![红标 MFA 确认卡](docs/images/red-card.png)

## 安全设计

1. **确认与执行分离**：模型只能生成 `pending_action`，真正的写接口必须由用户在卡片上点击确认后触发；聊天里说「确认」不等于授权。
2. **程序化 grounding**：模型自由文本里出现的金额、交易号、订单号，必须能在工具结果或会话事实中找到出处，否则整段替换为安全提示，并记 `GROUNDING_BLOCKED` 审计事件。
3. **提示注入防御**：外部文本按数据对待，不执行其中的指令；高风险指令进入确认流程而不是直接放行。
4. **三道审计**：`agent_run_event`（运行事件）、`business_audit_event`（业务写）、`llm_call_audit`（模型调用）三张表，所有关键动作可回放。
5. **异常熔断与沙箱**：连续失败/超时触发熔断；代码执行走隔离网关，限制文件大小、进程数和执行时间。
6. **真实账号与多租户隔离**：登录后所有会话、内存、待确认操作都绑定当前用户；跨用户访问返回 404。

详细口径见 [安全自评报告](docs/security-self-assessment.md)。

## 架构

```text
Vue 3 前端 / 飞书 IM
        │  SSE + REST
        ▼
FastAPI 对话底座 ── Supervisor（LangGraph）
        │                │
        │                ├─ transfer   智能转账
        │                ├─ bill       账单分析
        │                ├─ wealth     理财
        │                ├─ card       卡片
        │                ├─ subscription 订阅代扣
        │                └─ life_event 生活事件
        ▼
工具网关 ── 确认卡 / grounding / 审计 / 熔断 / 沙箱
        │  HTTP + X-Service-Token
        ▼
模拟银行服务（FastAPI + PostgreSQL）
```

- [架构与运行说明](docs/ARCHITECTURE.md)
- [可视化架构图](docs/architecture-map/runtime-architecture.html)
- [模块接入契约](docs/superpowers/specs/2026-09-22-module-integration-seam-contract.md)

## 技术栈

- **后端**：Python 3.12、FastAPI、LangGraph、SQLAlchemy、Alembic、PostgreSQL
- **前端**：Vue 3、TypeScript、Vite
- **模型**：任意 OpenAI Chat Completions 兼容接口（在页面设置面板或环境变量中配置）
- **渠道**：飞书开放平台 WSS 长连
- **部署**：Docker Compose + Nginx；GitHub Actions 可推送测试环境

## 快速开始（Windows）

### 1. 前置条件

- Python 3.12+、[uv](https://docs.astral.sh/uv/)
- Node.js 22+
- Docker Desktop（本机 PostgreSQL 用）
- 一个 OpenAI Chat Completions 兼容的模型 Key（不填也能启动，但聊天会返回 `MODEL_NOT_CONFIGURED`）

### 2. 启动本机 PostgreSQL

```powershell
docker run -d --name bank-agent-db-1 `
  -e POSTGRES_USER=bank -e POSTGRES_PASSWORD=bank_local `
  -p 55432:5432 -v bank-agent-pgdata:/var/lib/postgresql/data `
  postgres:16-alpine
```

### 3. 准备配置

```powershell
Copy-Item database.txt.example database.txt
Copy-Item database.local.txt.example database.local.txt
Copy-Item backend\.env.example backend\.env
Copy-Item bank_backend\.env.example bank_backend\.env
```

- `database.txt` / `database.local.txt`：按你本机的 PostgreSQL 填写；本地容器默认就是 `127.0.0.1:55432`、用户 `bank`、密码 `bank_local`。
- 模型 Key：首次启动后在页面右上角「设置」里填，或写进 `backend/.env.local` 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。
- `BANK_SERVICE_TOKEN` 由 `start.ps1` 首次启动时自动生成并写入两个 `.env`，不需要手填。

### 4. 一键启动

```powershell
./start.ps1 -Database local
```

脚本会依次检查环境、补依赖、建库/迁移/种子、拉起三个进程并打开浏览器：

- 前端 <http://127.0.0.1:5173/>
- Agent 后端 <http://127.0.0.1:8000/api/health>
- 模拟银行 <http://127.0.0.1:8001/api/health>

停止：`./stop.ps1`。

> 不想用脚本时，三个服务也可以分别启动；命令见 [架构与运行说明](docs/ARCHITECTURE.md)。

## 测试

```powershell
# 后端（需要一个独立的 test 库）
cd backend
$env:TEST_DATABASE_URL = "postgresql+psycopg://bank:bank_local@127.0.0.1:55432/test"
.venv\Scripts\python.exe -m pytest -q

# 前端
cd frontend
npm test
npm run build
```

当前基线：后端 `2,621 passed / 51 skipped`，模拟银行 `385 passed`，前端 `473 passed / 1 skipped`；48 项真实端到端场景全过。测试数据与模型调用均不依赖线上环境。

## 项目结构

```text
backend/          Agent 底座：Supervisor、六个领域模块、工具网关、审计、渠道
bank_backend/     模拟银行：账户/转账/账单/卡/订阅/理财/生活事件 API
frontend/         Vue 3 演示前端
deploy/           Dockerfile、Compose、Nginx、部署脚本
docs/             架构、安全自评、模块说明、测试矩阵、需求材料
```

## 文档

- [架构与运行说明](docs/ARCHITECTURE.md) —— 服务边界、对话流程、SSE、配置、迁移
- [技术文档](docs/技术文档.md) —— 系统架构图、核心算法、安全设计、部署与验收
- [安全自评报告](docs/security-self-assessment.md) —— 权限分级、五层纵深、审计、沙箱、已知风险
- [测试矩阵](docs/test-matrix.md) —— 核心用例与覆盖范围
- [智能转账](backend/app/modules/transfer/README.md)
- [账单分析](docs/bill-agent.md)
- [理财](backend/app/modules/wealth/README.md)
- [卡片管理](docs/card-management.md)
- [订阅代扣](backend/app/modules/subscription/README.md)
- [生活事件](backend/app/modules/life_event/README.md)
- [飞书渠道设计](docs/superpowers/specs/2026-10-27-im-channel-bridge-design.md)
- [部署与 CI/CD](docs/deploy-cicd.md)

## 已知边界

- 这是赛题演示项目，模拟银行不是真实银行系统；金额与客户均为演示数据。
- 任务图目前以**顺序执行**为主，不宣称并行调度。
- 普通推理轮可以点「停止」中断；确认卡一旦提交银行，不能中途撤销。
- 飞书渠道默认关闭，需要自己的飞书应用凭据。
- 部署脚本以单机 Docker Compose 为目标，未做多副本与高可用。

## 交付物

- [200 字作品简介](docs/deliverables/intro-200.md)
- [技术文档 PDF](docs/deliverables/bank-agent-tech-doc.pdf) / [DOCX](docs/deliverables/bank-agent-tech-doc.docx)
- [安全自评报告 PDF](docs/deliverables/bank-agent-security-assessment.pdf) / [DOCX](docs/deliverables/bank-agent-security-assessment.docx)
- [答辩 PPT](docs/deliverables/bank-agent-deck.pptx)（10 页，含讲稿）
- [演示视频](docs/deliverables/bank-agent-demo.mp4)（3 分 02 秒，真实 UI 录屏）
- 本仓库是**公开源码镜像**：已移除真实数据库凭据、SMTP 授权码、服务器地址与运维口令；启动所需的示例配置均在 `*.example` 文件中。代码、文档与作品资料与本项目 2026-11-01 最终验收版同步。

---

如果这个项目对你有帮助，欢迎 Star。
