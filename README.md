# Agentic RAG Prototype

> **企业级 Agentic RAG（智能体驱动的检索增强生成）文档问答系统原型**
>
> 可演示、可迭代、可上生产：从文档入库到智能问答，再到治理与观测，全链路自洽。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Vue](https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?logo=vue.js&logoColor=white)](https://vuejs.org/)
[![Tests](https://img.shields.io/badge/Tests-768%20passed-success)](#测试)
[![License](https://img.shields.io/badge/Status-MVP--Production--Ready-blue)]()

---

## 一、项目简介

本项目搭建了一套**企业级 Agentic RAG 智能问答系统原型**：用户上传文档后，系统通过「智能体规划 → 多源检索 → 答案生成 → 引用溯源 → 合规过滤」的完整链路，给出**可溯源、可审计、可度量**的回答。

核心目标是验证「把 Agentic RAG 推到生产环境」所需的全部工程要素——权限、审计、观测、治理、安全、性能——是否都能落到代码与基础设施层面。

完整需求见 [`Product_Design/企业级Agentic_RAG智能问答系统_PRD.docx`](./Product_Design/)；架构决策见 [`docs/phase1-mvp/`](./docs/phase1-mvp/)。

---

## 二、项目优势

### 2.1 Agentic 能力：智能路由 + 智能体编排

- **智能路由（Router）**：基于置信度的用户意图分类器(LLM,关键词)，把问题分发到不同通道(简单/复杂/全局分析/精确事实)，优化使用场景和成本。
- **LangGraph 编排**：复杂问题走 LangGraph 状态机，智能体可多轮调用检索工具、改写查询、合成答案。
- **直搜通道（Direct）**：简单问题走"向量 + BM25 + Rerank"混合检索直出，平均时延显著低于 Agent 路径。
- **两阶段检索（Two-Stage）**：先粗排父节点（Top-K=3），再细排子节点（Top-K=10），解决长文档跨段语义断裂。

### 2.2 企业级权限与安全

- **RBAC + 工作区**：细粒度角色权限 + 多工作区隔离，权限校验在网关、检索、合规三层生效。
- **ACL 感知检索**：`build_user_filter` 在 Qdrant 检索前注入 workspace / doc_id 过滤条件，**不可能**越权返回未授权文档。
- **查询守门（Query Guardrail）**：4 类规则（prompt injection / 越权指令 / 敏感词 / 长度）在 LLM 之前拦截，输入侧零漏洞。
- **审计 WORM**：审计日志走 PostgreSQL 触发器，**不可改写、不可删除**，满足金融/医疗合规要求。
- **M5 全量安全加固**：8 项安全审计 finding 全部关闭（密钥 / Redis / Docker / 注入 / SSRF / 越权），CI 拒绝带 `__FROM_SECRET__` 默认值的镜像出库。

### 2.3 多模态文档解析

- **5 个文本抽取器**：PyMuPDF（PDF）/ python-docx（DOCX）/ python-pptx（PPTX）/ openpyxl（XLSX）/ 原生（MD/TXT）。
- **视觉路由（Visual Router）**：扫描件/复杂版式 PDF 自动分流到 DeepDoc OCR 服务。
- **RapidOCR 兜底**：DeepDoc 不可用时,本地 RapidOCR 兜底，无单点依赖。
- **解析幂等**：通过 UUID5 + `ON CONFLICT` 实现 upsert 幂等，重复上传不产生脏数据。

### 2.4 可观测与治理

- **Prometheus + Grafana**：自带仪表盘（`infra/grafana/`），覆盖 LLM 延迟/失败率、检索召回率、CSAT 满意度等关键 SLO。
- **审计 + 反馈闭环**：CSAT 看板 + 满意度突降告警 → 自动触发告警 → 管理员介入排查。
- **漂移检测（Drift Detector）**：每 7 天扫一次质量指标，发现漂移自动报警。
- **结构化日志 + Trace ID**：所有请求贯穿 `trace_id`，跨服务可串联。

### 2.5 工程化交付

- **端到端类型安全**：后端 Pydantic / 前端 OpenAPI 自动生成 TS 类型，编译期及时发现并处理字段不一致。
- **完整测试金字塔**：768 个测试通过（pytest + vitest），含单元 / 集成 / 契约测试。
- **Docker Compose 一键起**：10 个服务编排（PostgreSQL / Redis / Qdrant / LiteLLM / DeepDoc / API / Celery / Prometheus / Grafana / Alertmanager）。
- **前端 Vue 3 单页应用**：14 路由 + 3 守卫 + 7 Pinia store + RBAC 内建，无需手写权限。

---

## 三、技术栈

### 后端

| 类别 | 选型 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | 类型注解 + `from __future__ import annotations` |
| Agent 框架 | LangGraph 0.2+ | 状态机编排 + 工具调用 |
| 向量库 | Qdrant | 混合检索（dense + sparse BM25）|
| 关系库 | PostgreSQL 16 | 17 张表，Alembic 迁移管理 |
| 缓存 / 队列 | Redis 7 / Celery | 缓存 + 异步任务 |
| LLM 网关 | LiteLLM 1.52+ | 模型无关，支持 DeepSeek-V3 / R1 / Qwen 系列 |
| Web 框架 | FastAPI 0.115+ | 异步、自动 OpenAPI |
| 数据校验 | Pydantic 2.9+ | 全链路类型契约 |
| 包管理 | uv | `pyproject.toml` + `uv.lock`，**禁止**手动 `pip install` |

### 前端

| 类别 | 选型 |
|------|------|
| 框架 | Vue 3.5 + Vite 5 |
| 语言 | TypeScript 5.6 |
| UI 库 | Element Plus 2.8 |
| 状态管理 | Pinia 2.2（7 个 store）|
| 路由 | vue-router 4 + 3 守卫（auth / rbac / workspace）|
| HTTP | axios 1.7（拦截器统一处理 5xx/timeout）|
| 图表 | vue-echarts + ECharts 5.5 |
| 国际化 | vue-i18n（zh-CN / en-US）|
| 包管理 | pnpm ≥ 8 |

### 基础设施

- **PostgreSQL 16-alpine**：业务库 `rag_business` + LiteLLM 元数据库 `rag_litellm`
- **Redis 7-alpine**：缓存（DB 0）+ 对话历史（DB 4）+ Celery broker/backend（DB 1/2）
- **Qdrant latest**：hybrid dense + sparse，端口 6333（HTTP）/ 6334（gRPC）
- **LiteLLM proxy**：模型网关，端口 4000
- **RAGFlow DeepDoc**：LitServe :9390，OCR / 表格结构识别 / 版面分析
- **Prometheus + Grafana + Alertmanager**：观测三件套，端口 9090 / 3000 / 9093

---

## 四、系统架构

```
┌──────────────────────────────────────────────────────────┐
│  L5 用户交互层  Vue 3 SPA · FastAPI REST · 管理后台       │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│  L4 智能路由层  Query Guardrail → Router → LLM Classifier │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│  L3 检索引擎层                                            │
│  ├── Direct Path   Hybrid Searcher (Dense + BM25 + Rerank)│
│  └── Agentic Path  LangGraph Runner (Plan → Tool → Answer)│
│  └── ACL Filter    Workspace + Doc-level 过滤            │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│  L2 生成输出层  LiteLLM Gateway → Synthesizer → Citations │
│                   → Sensitive Filter → Masker            │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│  L1 基础设施层  Qdrant · PostgreSQL · Redis · Celery       │
│                   · DeepDoc · RapidOCR · 5× Extractors   │
└──────────────────────────────────────────────────────────┘
        ▲
        │ 审计 WORM · Prometheus · CSAT · Drift
        │
  L0 横切关注点
```

完整架构定义见 [`docs/phase1-mvp/DESIGN_phase1-mvp.md`](./docs/phase1-mvp/DESIGN_phase1-mvp.md)。

---

## 五、快速开始

### 5.1 环境要求

- Python 3.11+
- Node.js ≥ 18 + pnpm ≥ 8
- Docker + Docker Compose v2
- [uv](https://docs.astral.sh/uv/)（Python 依赖管理）
- 至少 8GB 可用内存（Qdrant + DeepDoc + 模型推理）

#### 菜单启动（推荐）

完成 5.2 配置后，在项目根目录执行：

```bat
agenticRAG.bat
```

按菜单提示选择：

- **[1] 首次安装**：自动创建 `.env`（如缺失）+ `uv sync` + `cd src\web && pnpm install`（已存在的步骤会自动跳过）
- **[2] 一键启动**：`docker compose up -d` 并弹出 Backend & Frontend 两个窗口
- **[3] 停止所有**：`docker compose down` + 关闭弹出的窗口

> 5.3–5.5 三个小节是 [2] 一键启动 所执行步骤的等价手动命令，方便在调试或自定义场景下使用。

### 5.2 克隆与配置

```bash
git clone https://github.com/MauriceHuang2018/Agentic_RAG_Project.git   # 需要先下载代码
cd Agentic_RAG_Project

# 后端依赖
cp .env.example .env
# 编辑 .env，至少填写：
#   POSTGRES_PASSWORD / REDIS_PASSWORD / JWT_SECRET / LITELLM_MASTER_KEY /
#   DASHSCOPE_API_KEY / GRAFANA_ADMIN_PASSWORD / QDRANT_API_KEY
```

### 5.3 启动基础设施

`agenticRAG.bat` 已自动执行。手动启动命令：

```bash
docker compose up -d        # 起 10 个服务
docker compose ps           # 健康检查
```

服务启动后访问：
- API：<http://localhost:8000>（Swagger UI 在 `/docs`）
- Grafana：<http://localhost:3000>
- Prometheus：<http://localhost:9090>
- Qdrant Dashboard：<http://localhost:6333/dashboard>

### 5.4 启动后端

`agenticRAG.bat` 已在弹出的 Backend 窗口中自动启动。手动启动命令：

```bash
uv sync                                     # 安装依赖（首次 ~3 分钟）
uv run python -m agentic_rag_project        # 或 uv run uvicorn agentic_rag_project.main:app --reload
```

启动日志会打印 `Application startup complete`，监听 `0.0.0.0:8000`。

### 5.5 启动前端

`agenticRAG.bat` 已在弹出的 Frontend 窗口中自动启动。手动启动命令：

```bash
cd src/web
pnpm install                                # 安装依赖
pnpm gen:openapi                            # 从后端 OpenAPI 生成 TS 类型
pnpm dev                                   # http://localhost:5173
```

默认登录账号（来自 `seed_admin_user` / `seed_demo_data`）：

| 用户名 | 密码 | 角色 | 用途 |
|--------|------|------|------|
| `admin` | `.env` `DEMO_ADMIN_PASSWORD`（默认见 `.env.example`） | `system_admin` + super_admin | 跨工作空间操作员（M6+ 运维/审计） |
| `alice` | `.env` `DEMO_ALICE_PASSWORD` | `kb_user` | 普通聊天用户 |
| `bob` | `.env` `DEMO_BOB_PASSWORD` | `kb_user` | 普通聊天用户 |

---

## 六、使用指南

### 6.1 命令行：发起一次对话

```bash
# 1. 登录获取 JWT
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"chat_user","password":"<seed-password>"}' \
  | jq -r '.access_token')

# 2. 提问（走 Direct 通道：向量 + BM25 + Rerank）
curl -X POST http://localhost:8000/api/v1/chat/query \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "我们的产品保修期是多久？",
    "workspace_id": "<your-workspace-uuid>",
    "route_hint": "auto"
  }'
```

返回结构示例：

```json
{
  "answer": "标准保修期为购买后 12 个月……",
  "route": "direct",
  "fallback_triggered": false,
  "citations": [
    {"doc_id": "...", "title": "用户手册.pdf", "page": 7, "score": 0.87}
  ],
  "session_id": "...",
  "trace_id": "..."
}
```

### 6.2 Web UI：完整功能

浏览器访问 <http://localhost:5173>，登录后可使用：

- **Chat**（`/chat`）：多轮对话、流式响应、引用溯源、重新生成、点赞/点踩
- **Documents**（`/documents`）：拖拽上传、解析进度、列表/搜索/删除
- **Feedback**（`/feedback`）：提交工单、查看历史、客服对话窗口
- **Admin**（`/admin/*`）：用户管理、角色配置、审计日志、CSAT 看板、告警配置
- **Profile**（`/profile`）：个人信息、密码修改、API Token 管理

所有受限页面在路由层由 `rbac.guard.ts` 拦截，**不依赖**视图内手写检查。

### 6.3 上传一份文档

```bash
DOC_ID=$(curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./your-doc.pdf" \
  -F "workspace_id=<your-workspace-uuid>" \
  | jq -r '.document_id')

# 轮询状态
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/documents/$DOC_ID/status" | jq '.status'
# → "ready" 表示解析 + 索引完成
```

支持格式：PDF / DOCX / PPTX / XLSX / MD / TXT；扫描件自动走 DeepDoc OCR。


---

## 七、部署到生产

### 7.1 关键加固（已完成）

- ✅ 所有密钥由 docker-compose `:?` 守卫强制注入，缺失即拒绝启动
- ✅ Grafana admin 密码必填（无默认）
- ✅ 审计日志表 WORM 触发器（不可改写 / 不可删除）
- ✅ Prometheus `/metrics` 仅允许 loopback + CIDR 白名单
- ✅ Query Guardrail 在 LLM 之前 4 层过滤

### 7.2 部署前自检

详见 [`docs/m4_3_governance/PROD_DEPLOYMENT_CHECKLIST.md`](./docs/m4_3_governance/PROD_DEPLOYMENT_CHECKLIST.md)。要点：

1. `.env` 中所有 `__FROM_SECRET__` 全部替换为真实密钥
2. `JWT_SECRET` 与 `LITELLM_MASTER_KEY` 用 `openssl rand -hex 32` 生成
3. `METRICS_BEARER_TOKEN` 必须设置（公网部署）
4. `PROMETHEUS_MULTIPROC_DIR` 已挂载（多 worker 场景）
5. PostgreSQL 数据卷、Qdrant snapshot 已纳入备份策略

### 7.3 关闭与清理

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 同时删除数据卷（**危险操作**，会清空 PG/Qdrant/Redis）
```

---

## 八、项目结构

```
Agentic_RAG_Project/
├── Product_Design/                # 原始 PRD 与工作流文档（只读）
├── docs/                          # 产品文档、ADR、6A 工作流产物
│   ├── phase1-mvp/                # 一期 MVP：ALIGNMENT → CONSENSUS → DESIGN → TASK → ACCEPTANCE
│   ├── m3_agentic_rag_intent/     # M3: Agentic RAG 智能体路径
│   ├── m4_3_governance/            # M4.3: 审计 WORM + Query Guardrail + Drift
│   ├── m4_4_csat_dashboard/       # M4.4: CSAT 看板 + 告警
│   ├── m5_security/                # M5: 全量安全加固
│   ├── 前端架构/                   # 前端路由 / 目录 / 权限设计
│   └── 原型设计/                   # 字段映射 v2.0
├── src/
│   ├── agentic_rag_project/       # 后端 Python 包（FastAPI）
│   │   ├── api_gateway/           # 路由层（chat/documents/feedback/admin/me）
│   │   ├── agent_core/            # LangGraph Runner + State + Tools
│   │   ├── chat/                  # ChatService + Synthesizer
│   │   ├── retrieval_direct/      # Hybrid Searcher + Two-Stage
│   │   ├── router/                # Classifier（LLM/Heuristic/Keyword/Default）
│   │   ├── doc_processor/         # 5 × Extractors + Visual Router
│   │   ├── rbac/                  # 角色 / 权限 / 工作区 / 种子数据
│   │   ├── acl_filter/            # Qdrant Payload ACL 注入
│   │   ├── audit/                 # AuditService + WORM 触发器
│   │   ├── query_guardrail/       # 4 类输入过滤规则
│   │   ├── observability/         # Prometheus + LLM 指标 + CSAT 查询
│   │   ├── feedback/              # 工单 / 评分 / 归因 / 状态机
│   │   ├── services/              # 业务服务层
│   │   └── db/                    # SQLAlchemy 模型 + Alembic 迁移
│   └── web/                       # 前端 Vue 3 SPA（详见 src/web/README.md）
├── tests/                         # pytest 后端测试（768 通过）
├── infra/                         # docker-compose、Prometheus、Grafana、DeepDoc 配置
├── scripts/                       # 辅助脚本（种子数据、OpenAPI 生成等）
├── AGENTS.md                      # AI 协作守则（红线、命名规范、提交粒度）
├── docker-compose.yml             # 10 服务编排
├── pyproject.toml                 # uv 依赖声明（**唯一**来源）
└── README.md                      # 本文件
```

各目录边界与禁止事项见 [`AGENTS.md`](./AGENTS.md)。

---

## 十、版本里程碑

| Milestone | 说明 | 测试基线 |
|-----------|------|----------|
| phase1-mvp | 一期 MVP：文档解析 → 检索 → 问答 | 633/0/7 |
| M3 Agentic RAG | LangGraph 智能体路径首跑通 | 641/0/7 |
| M4.3 Governance | 审计 WORM + Query Guardrail + Drift | 661/1/7 |
| M4.4 CSAT | CSAT 看板 + 告警 | 675/1/7 |
| M5 Security | 全量安全加固（8 项 finding） | 729/1/7 |
| M6 Frontend | Vue 3 SPA 完整版（14 路由） | 755/2/7 |
| 工作区 ACL | ACL 数据管线修复 | 764/1/7 |
| 解析路由修复 | parser_router + indexer FK 修复 | 768/1/7 |
| 当前 | Synthesizer 容错 + Chat 重试 | **768 / 1 / 7** |

完整进度见 [`docs/`](./docs/) 目录下每个子项目的 `*ACCEPTANCE*.md`。

---

## 十一、贡献指南

1. 动手前先读 [`AGENTS.md`](./AGENTS.md)：红线、命名规范、提交粒度
2. 新需求走 **6A 工作流**（[`docs/.claude/rules/6a-workflow.md`](./.claude/rules/6a-workflow.md)）：ALIGNMENT → CONSENSUS → DESIGN → TASK → ACCEPTANCE
3. 遵循「先 spec → 再 test → 再实现」，单元测试失败不许注释
4. **已完成且正确的功能尽量不要修改**；改一处提交一次，便于回滚
5. 提交信息用英文，说明动机而非罗列改动

---

## 十二、许可证与声明

- 本项目为**原型**，生产部署前需完成第三方安全审计
- 默认模型为 DashScope（Qwen/DeepSeek 系列），需自备 API Key
- 部分代码参考 DeepDoc / LangGraph / LiteLLM 等开源项目，遵循各自许可证

---

**最后更新**：2026-08-20