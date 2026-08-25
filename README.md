# Agentic RAG Prototype

> 基于 Agentic RAG（智能体驱动的检索增强生成）的企业级文档问答系统原型。

## 简介

本项目旨在搭建一套**可演示、可迭代**的 Agentic RAG 智能问答系统原型。用户上传文档后，系统通过智能体规划 → 多源检索 → 答案生成与校验的链路，给出可溯源的回答。

详细需求见 [`Product_Design/企业级Agentic_RAG智能问答系统_PRD.docx`](./Product_Design/)。

## 目录速览

```
Agentic_RAG_Project/
├── Product_Design/    # 原始 PRD 与工作流文档
├── docs/              # 产品文档与决策记录
├── assets/            # 图片素材（设计、bug、参考）
├── notes/             # 学习笔记与技术调研
├── src/               # 源代码
├── tests/             # 测试脚本与结果
├── AGENTS.md          # 给开发 AI 的协作守则
└── README.md          # 本文件
```

各目录用途与边界请阅读 [AGENTS.md](./AGENTS.md)。

## 快速开始

> 本项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 依赖与虚拟环境，**不要**使用裸 `pip` 或手动维护 `requirements.txt`。

```bash
# 1. 安装 uv（如尚未安装）
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 同步依赖（首次运行会自动创建 .venv 并安装 pyproject.toml 中声明的依赖）
uv sync

# 3. 配置环境变量
cp .env.example .env   # 然后填入 API Key 等

# 4. 运行测试
uv run pytest tests/

# 5. 启动 Demo（待实现）
uv run python -m agentic_rag_project
```

## 技术栈（部分选型待 PRD 架构对齐任务确认）

- **语言**：Python 3.11+
- **包管理**：[uv](https://docs.astral.sh/uv/)（`pyproject.toml` + `uv.lock`）
- **LLM**：DeepSeek-V3（主力）/ DeepSeek-R1（推理）—— 详见 `docs/PRD架构对齐/CONSENSUS_PRD架构对齐.md`
- **向量库**：Qdrant（推荐，待最终确认）
- **Agent 框架**：LangGraph
- **文档解析**：unstructured（简单格式）+ RAGFlow 微服务（扫描件/复杂）
- **前端**：FastAPI（后端）+ Next.js / Streamlit（前端，Demo 阶段可选）

## 状态

- [x] 工作区目录结构搭建
- [x] 6A 工作流规则接入
- [ ] `pyproject.toml` 起草（用 `uv init` 初始化）
- [ ] PRD 架构对齐（当前任务，已完成 4 份文档，Approve 中）
- [ ] 最小可运行 Demo（文档加载 → 检索 → 生成）
- [ ] Agent 规划能力接入
- [ ] 评估与回测链路

---

最后更新：2026-08-21
