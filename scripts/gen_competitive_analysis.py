"""Generate competitive analysis docx based on Mantou Business School template.

Output: Product_Design/竞品分析.doc
"""

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from copy import deepcopy

OUTPUT_PATH = r"d:\DataBack\AI-Assistant-Py-Prj\DocGPT_RAG_Prototype\Agentic_RAG_Project\Product_Design\竞品分析.doc"
TEMPLATE_PATH = r"d:\DataBack\AI-Assistant-Py-Prj\DocGPT_RAG_Prototype\Agentic_RAG_Project\Product_Design\竞品分析报告模板（馒头商学院）.docx"


def set_cn_font(run, font_name="微软雅黑", size=None, bold=None):
    """Apply CJK font + size + bold to a run."""
    run.font.name = font_name
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)
    rFonts.set(qn("w:hint"), "eastAsia")
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold


def add_para(doc, text, *, size=10.5, bold=False, align=None, font="微软雅黑", style=None):
    """Add a paragraph with consistent CJK styling."""
    p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    if align is not None:
        p.alignment = align
    run = p.add_run(text)
    set_cn_font(run, font_name=font, size=size, bold=bold)
    return p


def set_cell_shading(cell, hex_color):
    """Set a cell background color (hex without #)."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def set_table_borders(table):
    """Apply a thin single-line border to all sides + inside of a table."""
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    tblBorders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = OxmlElement(f"w:{edge}")
        b.set(qn("w:val"), "single")
        b.set(qn("w:sz"), "4")
        b.set(qn("w:space"), "0")
        b.set(qn("w:color"), "808080")
        tblBorders.append(b)
    tblPr.append(tblBorders)


def fill_cell(cell, text, *, size=10.5, bold=False, align=None, fill=None):
    """Replace cell content with a single styled paragraph."""
    cell.text = ""
    p = cell.paragraphs[0]
    if align is not None:
        p.alignment = align
    run = p.add_run(text)
    set_cn_font(run, size=size, bold=bold)
    if fill is not None:
        set_cell_shading(cell, fill)


def make_table(doc, rows, cols, header_rows=1, header_fill="D9E1F2"):
    """Create a basic bordered table; return the table."""
    table = doc.add_table(rows=rows, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    set_table_borders(table)
    # Apply header shading + bold
    for r in range(header_rows):
        for c in range(cols):
            cell = table.cell(r, c)
            set_cell_shading(cell, header_fill)
            for p in cell.paragraphs:
                for run in p.runs:
                    set_cn_font(run, size=10.5, bold=True)
    return table


def build_doc():
    # Start from the template to inherit styles
    doc = Document(TEMPLATE_PATH)

    # Remove all default body content from template
    body = doc.element.body
    sectPr = body.find(qn("w:sectPr"))
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)

    # ===== Title =====
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title_p.add_run("企业级 Agentic RAG 智能问答系统竞品分析")
    set_cn_font(run, size=18, bold=True)

    # Subtitle
    sub_p = doc.add_paragraph()
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub_p.add_run("——基于馒头商学院竞品分析报告模板")
    set_cn_font(run, size=10.5, bold=False)

    # Document meta
    meta_p = doc.add_paragraph()
    meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta_p.add_run("编制日期：2026-09-06    适用范围：产品立项 / 技术选型 / 战略规划")
    set_cn_font(run, size=9, bold=False)

    # ============================================================
    # 一、确定竞品
    # ============================================================
    add_para(doc, "一、确定竞品", size=14, bold=True)

    add_para(
        doc,
        "选择依据：参照馒头商学院模板的通用原则（市场前列 + 阶段性强于本产品的目标），结合本项目 "
        "「企业级通用 Agentic RAG 智能问答系统」的定位，从 2026 年企业级知识库 / AI 搜索赛道中选定 3 "
        "款代表性产品作为本次竞品分析对象。其中 Glean 代表商业闭源赛道的标杆（行业 No.1），Onyx "
        "代表开源自托管赛道的标杆（被誉为「开源版 Glean」），RAGFlow 代表深度文档理解 + Agent "
        "工作流的开源技术派标杆。三者共同覆盖了「商业 vs 开源」「连接器型 vs 文档型」「纯搜索型 vs "
        "深度解析型」三个关键坐标轴，能够完整映射本产品的差异化空间。",
        size=10.5,
    )

    # 1.x 竞品列表小节
    add_para(doc, "1.1  候选竞品清单与筛选理由", size=12, bold=True)

    candidates_table = make_table(doc, rows=5, cols=4, header_rows=1)
    candidates_table.columns[0].width = Cm(2.5)
    candidates_table.columns[1].width = Cm(5.0)
    candidates_table.columns[2].width = Cm(4.5)
    candidates_table.columns[3].width = Cm(4.5)
    fill_cell(candidates_table.cell(0, 0), "候选竞品", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(candidates_table.cell(0, 1), "类型 / 定位", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(candidates_table.cell(0, 2), "入选理由", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(candidates_table.cell(0, 3), "在本项目中的角色", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    rows_data = [
        (
            "Glean",
            "商业闭源 · 企业级 AI 平台",
            "估值 72 亿美元，企业 AI 搜索赛道全球第一；Gartner 2025 Generative AI Knowledge Management 创新指南 Emerging Leader；连接器覆盖 275+ 应用。",
            "对标行业天花板，决定本产品在企业级能力上的目标基准。",
        ),
        (
            "Onyx",
            "开源 · 自托管 AI 平台",
            "原 Danswer（Y Combinator W23），GitHub 19.7k+ stars，定位「Open Source AI Platform - AI Chat with advanced features that works with every LLM」，40+ 连接器，气隙部署能力。",
            "对标「开源第一阵营」，决定本产品在自托管 + 模型无关维度的差异点。",
        ),
        (
            "RAGFlow",
            "开源 · 端到端 RAG 引擎",
            "Apache 2.0，GitHub 76k+ stars；DeepDoc 深度文档理解 + 内置 GraphRAG + Agent 工作流；当前开源 RAG 解析能力第一梯队。",
            "对标「文档解析 + Agent 工作流」技术派，决定本产品底层引擎的能力天花板。",
        ),
        (
            "其他备选",
            "Microsoft GraphRAG / LightRAG / LangSmith / Yuxi-Know 等",
            "本次报告以 3 个核心竞品为主，其他方案在文末「附：参考产品」中简述，避免正面对标 Glean 的连接器数量（详见 2025-05-19 内部纪要）。",
            "不进入主竞品矩阵，仅作为技术组件参考。",
        ),
    ]
    for i, (a, b, c, d) in enumerate(rows_data, start=1):
        fill_cell(candidates_table.cell(i, 0), a, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        fill_cell(candidates_table.cell(i, 1), b)
        fill_cell(candidates_table.cell(i, 2), c)
        fill_cell(candidates_table.cell(i, 3), d)

    # 1.2 确定结果
    add_para(doc, "1.2  本次竞品分析确定对象", size=12, bold=True)
    add_para(
        doc,
        "经筛选，最终确定本报告的 3 个核心竞品为：Glean（竞品 1）、Onyx（竞品 2）、RAGFlow（竞品 3）。"
        "三者形成「商业闭环标杆 + 开源连接器标杆 + 开源文档解析标杆」三角矩阵，可全面覆盖本产品在 "
        "Agentic RAG 路线上的差异化决策。",
        size=10.5,
    )

    # ============================================================
    # 二、产品分析
    # ============================================================
    add_para(doc, "二、产品分析", size=14, bold=True)

    # 2.1 战略层
    add_para(doc, "2.1  战略层", size=12, bold=True)

    strategy_table = make_table(doc, rows=4, cols=3, header_rows=1)
    fill_cell(strategy_table.cell(0, 0), "竞品", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(strategy_table.cell(0, 1), "口号 / 品牌主张", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(strategy_table.cell(0, 2), "定位", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    strategy_rows = [
        (
            "Glean",
            "Work AI that Works\nEnterprise AI that Works",
            "面向全球大型企业的「Work AI」平台，定位为「企业受信任的情报层（Trusted Intelligence Layer）」，以「Know what your company knows」为核心信息，"
            "通过 275+ 连接器 + Enterprise Graph + Agentic Engine 2 提供统一的搜索 / 助理 / 代理能力，强调零知识安全、权限感知与合规。",
        ),
        (
            "Onyx",
            "Open Source AI Platform\nAI Chat with advanced features that works with every LLM",
            "定位「开源 ChatGPT Enterprise 替代品」与「Glean 的开源对应物」，MIT 协议可自托管、可气隙部署；"
            "通过 40+ Indexed Connectors 持续后台索引，支持任意 LLM（OpenAI / Anthropic / Gemini / 本地 Ollama / vLLM / LiteLLM），强调模型无关与数据自主。",
        ),
        (
            "RAGFlow",
            "RAGFlow is a leading open-source RAG engine that fuses cutting-edge RAG with Agent capabilities to create a superior context layer for LLMs",
            "定位「Agentic Context Engine / 下一代 AI 代理的关键基础设施」，"
            "以 DeepDoc 深度文档理解为核心卖点，强调复杂 PDF / 扫描件 / 表格 / 图文混排的解析质量，"
            "将自身从「RAG 系统」升级为「Agent 的上下文引擎」，愿景是「Quality in, quality out」。",
        ),
    ]
    for i, (name, slogan, pos) in enumerate(strategy_rows, start=1):
        fill_cell(strategy_table.cell(i, 0), name, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        fill_cell(strategy_table.cell(i, 1), slogan)
        fill_cell(strategy_table.cell(i, 2), pos)

    # 2.2 范围层
    add_para(doc, "2.2  范围层", size=12, bold=True)

    # 2.2.1 功能对比表
    add_para(
        doc,
        "2.2.1  功能对比（★ 特色 / 差异化功能    √ 支持    × 不支持）",
        size=11,
        bold=True,
    )

    feature_table = make_table(doc, rows=16, cols=4, header_rows=1)
    fill_cell(feature_table.cell(0, 0), "功能维度", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(feature_table.cell(0, 1), "Glean", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(feature_table.cell(0, 2), "Onyx", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(feature_table.cell(0, 3), "RAGFlow", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    feature_rows = [
        ("企业 SaaS 连接器",       "★ 275+ 应用原生连接器",          "√ 40+ Indexed Connectors",        "√ 6 类数据源（Confluence/S3/Notion/Discord/Google Drive 等）"),
        ("本地复杂文档深度解析",   "× 依赖源应用 API",                "√ 支持但解析深度有限",            "★ DeepDoc + OCR 15+ 语言 + 表格结构识别"),
        ("实时增量索引 / CDC",     "√ Continuous sync（轮询）",        "√ Continuous sync（轮询）",        "× 批量 / 手动为主，增量能力有限"),
        ("权限感知检索 (ACL)",     "★ 实时权限同步 + Enterprise Graph","√ 权限继承 + 40+ 源 ACL",         "× 无内置权限感知"),
        ("GraphRAG（图增强 RAG）", "★ 商用级 GraphRAG",                "√ 可选 StructRAG / Agent Search", "√ 内置 GraphRAG（Light/General 双模式）"),
        ("Agent 规划-执行-反思",   "★ Agentic Engine 2 + 预构建 Agents","√ Agent 框架（深度弱于 Glean）", "√ Agent 工作流编排（缺反思-重试机制）"),
        ("多跳推理 / 工具调用",     "★ 成熟多跳 + 工具生态",            "√ Deep Research 多步检索",        "√ Agent + MCP + 代码沙箱"),
        ("引用溯源 / 答案可验证",  "√ 每条答案含引用",                 "√ Hybrid + Rerank + 引用",        "★ 引用 + 页码 + 包围盒 + 置信度分数"),
        ("多轮对话 / 追问引导",    "√ Cmd-J 相关内容推荐",             "√ 多轮对话",                      "√ 多轮对话 + 引用可视化"),
        ("模型无关（Any LLM）",    "√ Model Hub 接入 15+ 模型",        "★ Any LLM（含本地 Ollama/vLLM）", "√ OpenAI/DeepSeek/Claude/Gemini/本地"),
        ("全链路评估体系",          "× 内置评估非常有限",               "× 内置评估非常有限",               "× 内置评估非常有限"),
        ("数据治理 / 责任人 / 过期","√ 知识集合（治理有限）",            "× 几乎无内置治理",                "× 几乎无内置治理"),
        ("合规认证",      "★ SOC 2 + GDPR + Dell On-Prem",        "★ SOC 2 Type II + 气隙部署 + ISO 27001 进行中", "× 自建合规（DIY）"),
        ("私有化 / 数据驻留",      "√ Dell On-Prem 私有化",            "★ 完全自托管 / 气隙部署",         "√ Docker 一键私有化"),
        ("成本路由（问题复杂度分类）","× 无内置路由",                    "× 无内置路由",                    "× 无内置路由"),
    ]
    for i, (dim, g, o, r) in enumerate(feature_rows, start=1):
        fill_cell(feature_table.cell(i, 0), dim, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        fill_cell(feature_table.cell(i, 1), g, align=WD_ALIGN_PARAGRAPH.LEFT)
        fill_cell(feature_table.cell(i, 2), o, align=WD_ALIGN_PARAGRAPH.LEFT)
        fill_cell(feature_table.cell(i, 3), r, align=WD_ALIGN_PARAGRAPH.LEFT)

    # 2.2.2 详细对比（按核心能力维度展开）
    add_para(doc, "2.2.2  详细对比", size=11, bold=True)

    # --- 详细对比 1) 文档解析与索引 ---
    add_para(doc, "1) 文档解析与索引", size=10.5, bold=True)

    add_para(doc, "Glean：", size=10.5, bold=True)
    add_para(
        doc,
        "Glean 主要依靠 275+ 企业 SaaS 连接器（Google Drive / SharePoint / Confluence / Slack / "
        "Salesforce / Jira / Zendesk / ServiceNow / GitHub / Box / OneDrive / Outlook / Gmail 等），"
        "对源应用的元数据 / 内容 / 权限做轻量解析并持续后台同步。深度解析能力依赖源应用 API 返回的字段，"
        "对本地复杂文档（扫描件 PDF、复杂 Excel 表格、图文混排 PPT）解析能力有限。本地文件上传不是其主战场。",
        size=10.5,
    )

    add_para(doc, "Onyx：", size=10.5, bold=True)
    add_para(
        doc,
        "Onyx 通过 40+ Indexed Connectors 把 Slack、Google Drive、Confluence、GitHub、Jira、Salesforce、"
        "Notion、Gmail、SharePoint、Linear、Zendesk 等数据源持续后台索引；连接器策略明确区别于 MCP / "
        "live-query，强调「continuous sync + 后台索引」。同样侧重连接器，对本地扫描件 / 复杂表格的解析能力"
        "弱于 RAGFlow。在 220K workplace documents 基准中宣称 64% 胜率击败 ChatGPT、76% 胜率击败 Notion AI。",
        size=10.5,
    )

    add_para(doc, "RAGFlow：", size=10.5, bold=True)
    add_para(
        doc,
        "RAGFlow 的核心壁垒是 DeepDoc 引擎：基于计算机视觉 + 布局分析（而非朴素文本切分），可识别 10 类"
        "版面元素（text / title / figure / figure caption / table / table caption / header / footer / "
        "reference / equation），并保留空间关系（图注归属于上方图、表头归属于下方表）；Table Structure "
        "Recognition 支持层级表头、跨单元格、投影行表头；OCR 支持 15+ 语言，处理扫描件 / 中英混排。格式覆盖"
        "16+（PDF 扫描 / PDF 数字 / DOCX / XLSX / PPTX / 图片 / TXT / MD / HTML / EPUB / Email 等）。切片"
        "基于模板（Book / Paper / Resume / Legal / Table 等），可人工干预修正。",
        size=10.5,
    )

    add_para(doc, "[截图占位] 截图：Glean 数据源管理页 / Onyx Connectors 列表 / RAGFlow 文档解析预览", size=10.5)

    # --- 详细对比 2) Agent / 多跳推理 / 工具调用 ---
    add_para(doc, "2) Agent 规划-执行-反思 + 多跳推理", size=10.5, bold=True)

    add_para(doc, "Glean：", size=10.5, bold=True)
    add_para(
        doc,
        "Agentic Engine 2 提供 Fast 与 Thinking 双模式；Agent Builder 支持无代码 / 对话式构建、"
        "支持分支、循环、版本化；Agent Library 沉淀预构建 Agents（IT/HR/Sales/Support 通用模板）。"
        "Agent 可跨系统编排完成 ticket triage、deal briefs、onboarding、PR risk summaries、runbook "
        "search 等场景；Agent Governance 提供运行时护栏，是行业最成熟的商业化方案。",
        size=10.5,
    )

    add_para(doc, "Onyx：", size=10.5, bold=True)
    add_para(
        doc,
        "Onyx 提供 Agent 框架 + Deep Research 多步检索，能完成跨数据源的链式查询；其闭环能力弱于 Glean，"
        "「自我修正」机制主要靠查询改写 + 多轮检索回退，但缺统一的反思-重试编排层。",
        size=10.5,
    )

    add_para(doc, "RAGFlow：", size=10.5, bold=True)
    add_para(
        doc,
        "RAGFlow 提供可视化 Agent Workflow Builder（拖拽式画布）、MCP 工具调用、Python / JavaScript "
        "代码沙箱（2026 年 4 月发布）、Chart generation、Persistent Memory、Pre-built Templates、"
        "Published Agent Apps（2026 年 4 月发布）。底层走「工作流编排」路线，缺真正的「反思-重试」"
        "机制，自主规划能力仍弱于 Glean / Onyx。",
        size=10.5,
    )

    add_para(doc, "[截图占位] 截图：Glean Agent Builder / Onyx Deep Research / RAGFlow Agent Canvas", size=10.5)

    # --- 详细对比 3) 检索 / 答案体验 ---
    add_para(doc, "3) 检索 / 答案体验（引用 + 追问 + 评估）", size=10.5, bold=True)

    add_para(doc, "Glean：", size=10.5, bold=True)
    add_para(
        doc,
        "Hybrid Retrieval + Rerank；每条答案带引用；Deep Research agent 输出带引用 + 表格 + "
        "可视化的研究报告；Cmd-J 提供追问引导；Assistant 支持 Fast / Thinking 双模式。"
        "全链路评估（Golden Set / Shadow Replay / Drift Alarm）几乎全部依赖外部工具（LangSmith 等）。",
        size=10.5,
    )

    add_para(doc, "Onyx：", size=10.5, bold=True)
    add_para(
        doc,
        "Hybrid Retrieval（semantic + keyword）+ Rerank + Citations；多轮对话；追问引导有限；"
        "无内置全链路评估，需自接 RAGAS / LangSmith。",
        size=10.5,
    )

    add_para(doc, "RAGFlow：", size=10.5, bold=True)
    add_para(
        doc,
        "Hybrid Retrieval（dense + BM25）+ 多阶段 Rerank；支持 Elasticsearch / Infinity / "
        "OpenSearch / OceanBase；引用溯源到「文档 - 页码 - 包围盒」并展示置信度分数；"
        "支持迭代式检索精炼（context 不足时自动改写 query 重试）；Chunk 可视化允许人工干预。"
        "追问 / 转人工 / 纠错等差异化体验未内置。",
        size=10.5,
    )

    add_para(doc, "[截图占位] 截图：Glean 答案页 + Deep Research 报告 / Onyx Chat / RAGFlow Chunk 可视化", size=10.5)

    # --- 详细对比 4) 权限 / 合规 ---
    add_para(doc, "4) 权限感知检索 + 安全合规", size=10.5, bold=True)

    add_para(doc, "Glean：", size=10.5, bold=True)
    add_para(
        doc,
        "Glean 的核心护城河之一：通过 Enterprise Graph + Personal Graph 实现「实时权限同步 + "
        "检索阶段过滤」，支持跨应用统一权限模型；租户级隔离、可配置数据驻留；SOC 2、GDPR、"
        "DLP 报告、加密；Dell On-Prem 私有化部署。",
        size=10.5,
    )

    add_para(doc, "Onyx：", size=10.5, bold=True)
    add_para(
        doc,
        "Onyx 通过权限继承在检索时执行 ACL 过滤，支持 40+ 源系统的 ACL；MIT 协议完全自托管，"
        "支持气隙（air-gapped）部署；SOC 2 Type II 已获、ISO 27001 进行中，符合金融 / 国防客户"
        "对「数据完全自主」的要求。",
        size=10.5,
    )

    add_para(doc, "RAGFlow：", size=10.5, bold=True)
    add_para(
        doc,
        "RAGFlow 无内置权限感知检索；Docker 一键私有化部署简单，但合规认证（DLP / SOC 2 / GDPR）"
        "需自行建设。适合在受信内部环境使用，对外商用需自建合规链路。",
        size=10.5,
    )

    add_para(doc, "[截图占位] 截图：Glean Permissions Console / Onyx ACL 配置 / RAGFlow 用户管理", size=10.5)

    # --- 详细对比 5) 成本 / 部署 / 模型无关 ---
    add_para(doc, "5) 成本路由 + 部署模式 + 模型无关", size=10.5, bold=True)

    add_para(doc, "Glean：", size=10.5, bold=True)
    add_para(
        doc,
        "商业订阅（未公开定价），按席位收费；Model Hub 接入 15+ 模型（含 Amazon Bedrock / Azure "
        "OpenAI / Google Vertex AI / OpenAI 等）；提供 Usage Controls + AI Gateway 用于成本治理；"
        "缺基于问题复杂度的内置成本路由（仍以 Agent 自主调度为主）。",
        size=10.5,
    )

    add_para(doc, "Onyx：", size=10.5, bold=True)
    add_para(
        doc,
        "约 $20 / 用户 / 月（vs ChatGPT Enterprise 约 $60 / 用户 / 月，便宜 67%）；完全模型无关，"
        "支持 OpenAI / Anthropic Claude / Gemini + 自托管 Ollama / vLLM / LiteLLM；缺内置路由，"
        "部署方式最灵活（云 / 自托管 / 气隙）。",
        size=10.5,
    )

    add_para(doc, "RAGFlow：", size=10.5, bold=True)
    add_para(
        doc,
        "Apache 2.0 协议 + 约 78k GitHub stars，社区版免费；支持 OpenAI / DeepSeek / Anthropic "
        "Claude / Google Gemini / 本地 Ollama / vLLM / llama.cpp；嵌入模型支持 BGE / E5 / Jina / "
        "Voyage / OpenAI / 本地 sentence transformers；缺内置成本路由。",
        size=10.5,
    )

    add_para(doc, "[截图占位] 截图：Glean Model Hub / Onyx LLM 配置 / RAGFlow 模型管理", size=10.5)

    # 2.2.3 功能总结
    add_para(doc, "2.2.3  功能总结", size=11, bold=True)

    summary_table = make_table(doc, rows=4, cols=2, header_rows=1)
    fill_cell(summary_table.cell(0, 0), "竞品", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(summary_table.cell(0, 1), "核心优势与典型短板总结", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(summary_table.cell(1, 0), "Glean", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(
        summary_table.cell(1, 1),
        "优势：商业闭环最完整（连接器 + Agent + 权限 + 合规），行业护城河在「实时权限感知 + Enterprise Graph」；"
        "Agentic Engine 2 与预构建 Agents / Workflows 是行业最成熟的方案。\n"
        "短板：商业闭源、本地复杂文档解析能力弱、价格昂贵、缺内置全链路评估。",
    )
    fill_cell(summary_table.cell(2, 0), "Onyx", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(
        summary_table.cell(2, 1),
        "优势：MIT 自托管 + Any LLM + 气隙部署，价格仅为 ChatGPT Enterprise 1/3，"
        "「Indexed Connectors」vs MCP 的差异化策略清晰；220K 文档基准胜率强。\n"
        "短板：本地复杂文档解析弱于 RAGFlow，Agent 闭环能力弱于 Glean，无内置全链路评估与数据治理。",
    )
    fill_cell(summary_table.cell(3, 0), "RAGFlow", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(
        summary_table.cell(3, 1),
        "优势：DeepDoc + TSR + 16+ 格式解析是开源最强；内置 GraphRAG 双模式 + Agent Canvas + "
        "MCP + 代码沙箱 + 持久化记忆；引用溯源到页码 + 包围盒 + 置信度分数。\n"
        "短板：无内置权限感知 / 合规认证 / 全链路评估 / 数据治理；连接器生态仅 6 类；"
        "Agent 自主规划能力弱于 Glean / Onyx。",
    )

    # 2.3 结构层
    add_para(doc, "2.3  结构层", size=12, bold=True)

    add_para(doc, "2.3.1  各竞品首页 / 主入口截图对比", size=11, bold=True)
    add_para(
        doc,
        "[截图占位] 建议补充以下三张截图以完成对比：\n"
        "  · Glean 首页 Assistant 主对话界面（Cmd-J 入口 + 推荐问题 + 引用卡片）；\n"
        "  · Onyx Community Edition 默认 Chat 主页（左侧连接器状态栏 + 中间对话流 + 右侧引用面板）；\n"
        "  · RAGFlow Knowledge Base + Dataset 主入口（数据集列表 + 解析状态 + Agent 入口）。",
        size=10.5,
    )

    add_para(doc, "2.3.2  其他核心页面截图对比", size=11, bold=True)
    add_para(
        doc,
        "[截图占位] 建议补充以下对比：\n"
        "  · Glean：Agent Builder / Connectors 管理 / Permissions Console / Canvas (Deep Research)；\n"
        "  · Onyx：Connectors 列表 / Deep Research / 用户与权限 / 模型配置；\n"
        "  · RAGFlow：Dataset 详情 / Chunk 可视化 / Agent Workflow Canvas / 模型 + 嵌入模型配置。",
        size=10.5,
    )

    add_para(doc, "2.3.3  主操作流程对比", size=11, bold=True)

    flow_table = make_table(doc, rows=5, cols=2, header_rows=1)
    fill_cell(flow_table.cell(0, 0), "流程节点", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    fill_cell(flow_table.cell(0, 1), "三竞品实现差异", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    flow_rows = [
        (
            "数据接入",
            "Glean：管理员后台按应用启用连接器，授权 OAuth 后台自动索引。\n"
            "Onyx：同等流程，但提供 Connector Health 监控。\n"
            "RAGFlow：上传本地文件 / 配置 6 类数据源（Confluence/S3/Notion/Discord/Google Drive/Email）后触发解析。",
        ),
        (
            "问答交互",
            "Glean：Cmd-J / Assistant 单条 + 多轮，强项是 Cmd-J 的「相关内容推荐」。\n"
            "Onyx：单条 + 多轮，支持 Deep Research 多步检索。\n"
            "RAGFlow：单条 + 多轮 + 引用可视化 + Chunk 跳转；Agent 入口可走多步工作流。",
        ),
        (
            "权限治理",
            "Glean：实时同步源系统权限 + Enterprise Graph 权限映射。\n"
            "Onyx：索引阶段同步 ACL，检索阶段过滤。\n"
            "RAGFlow：无内置权限感知，需在企业版中自行叠加 ACL 层。",
        ),
        (
            "运营 / 治理",
            "Glean：Knowledge 集合 + 自定义描述 + 部分审计。\n"
            "Onyx：连接器健康监控 + 引用追踪。\n"
            "RAGFlow：Dataset 状态 + 解析日志 + Chunk 干预；缺数据治理后台。",
        ),
    ]
    for i, (node, diff) in enumerate(flow_rows, start=1):
        fill_cell(flow_table.cell(i, 0), node, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        fill_cell(flow_table.cell(i, 1), diff)

    add_para(doc, "[截图占位] 截图：主操作流程图（用户问 → 检索 → 生成 → 引用）三竞品对比", size=10.5)

    # ============================================================
    # 三、总结
    # ============================================================
    add_para(doc, "三、总结", size=14, bold=True)

    add_para(doc, "3.1  对本产品的启发", size=12, bold=True)
    add_para(
        doc,
        "（1）三竞品共同证明「企业级 AI 搜索 / 问答」已从单点能力升级为「连接器生态 + 深度文档解析 + "
        "Agent 编排 + 权限合规」的复合战场，单点能力无法满足企业采购标准。\n"
        "（2）Glean 的护城河（实时权限感知 + 预构建 Agents）证明：通用 SaaS AI 的天花板是「企业内部"
        "数据自主权」而非「模型能力本身」，对自托管 + 私有化路径是利好。\n"
        "（3）Onyx 的「Indexed Connectors vs MCP」策略证明：后台持续索引 > 即时拉取，是企业知识库"
        "的正确同步范式；这与 2025-05-19 内部纪要中「CDC + Kafka + 增量 Upsert」的判断一致。\n"
        "（4）RAGFlow 的 DeepDoc 证明：复杂文档解析能力是「把知识库问答做出差异化」的最直接抓手；"
        "对中型企业用户，解析质量 > 连接器数量。\n"
        "（5）三竞品在「全链路评估 / 数据治理 / 成本路由」上普遍是短板，是本产品做差异化的最大窗口。",
        size=10.5,
    )

    add_para(doc, "3.2  可借鉴的具体动作", size=12, bold=True)
    add_para(
        doc,
        "（1）借鉴 Glean：在产品形态上明确「Assistant + Search + Agent」三大入口，对应不同使用深度。\n"
        "（2）借鉴 Onyx：连接器后台需提供 Connector Health / 最近同步时间 / 失败重试可视化。\n"
        "（3）借鉴 RAGFlow：Chunk 可视化 + 人工干预入口必须保留，这是大文件知识库必需的「信任建立」手段。\n"
        "（4）借鉴 Glean 的 Enterprise Graph 思路：构建企业内部的「人 / 文档 / 项目」知识图谱作为未来"
        "Agent 编排的底座。\n"
        "（5）借鉴 RAGFlow 的引用包围盒 + 置信度：作为降低幻觉、提升答案可验证性的标配。",
        size=10.5,
    )

    add_para(doc, "3.3  差异化定位（如何打败竞品）", size=12, bold=True)
    add_para(
        doc,
        "本产品的核心差异化建议遵循 2025-05-19 内部纪要结论：\n"
        "· 不在「连接器数量」上正面追赶 Glean（5 年积累无法短期超越），聚焦「本地文件 + 钉钉 / 飞书 / "
        "企业微信 / Confluence 核心应用」+ 开放 API 让用户自研连接器。\n"
        "· 把「CDC + Kafka + 增量 Upsert 实时同步」做成招牌功能，主打分钟级新鲜度（竞品多为轮询级）。\n"
        "· 把「数据治理（文档责任人 + 权威来源 + 过期清理 + 反馈闭环）」做成核心卖点——这是所有竞品的盲区。\n"
        "· Agent 层深度自研规划-执行-反思闭环（基于 LangGraph 或自研状态机），解决 RAGFlow / Onyx "
        "缺反思-重试机制的痛点。\n"
        "· 权限感知检索必须自研，参考 Onyx 的「检索阶段过滤」架构 + Glean 的 Enterprise Graph 思路，"
        "设计统一 ACL 模型。\n"
        "· 成本路由层（问题复杂度分类器 + Token 预算熔断）作为架构壁垒，竞品均未提供。",
        size=10.5,
    )

    add_para(doc, "3.4  产品走向与核心点", size=12, bold=True)
    add_para(
        doc,
        "走向：以「实时性 + 数据治理 + 全链路评估 + 成本路由」四个维度作为长期护城河，配合「深度文档"
        "解析 + 权限感知 + Agent 闭环」三大工程能力，构建「企业可自托管、可合规、可量化、可治理」的"
        "Agentic RAG 智能问答底座。\n"
        "核心点：\n"
        "  ① 文档解析深度对标 RAGFlow（DeepDoc / TSR / OCR 15+ 语言）；\n"
        "  ② 实时同步对标 Onyx（Indexed Connectors + CDC）；\n"
        "  ③ Agent 闭环对标 Glean（规划-执行-反思 + 工具生态）；\n"
        "  ④ 数据治理 + 全链路评估 + 成本路由 = 竞品盲区，本产品的最大差异化点。",
        size=10.5,
    )

    # 附：参考产品（不在本次主竞品矩阵中）
    add_para(doc, "附：参考产品（不进入主竞品矩阵）", size=11, bold=True)
    add_para(
        doc,
        "· Microsoft GraphRAG（MIT）：微软研究院 2024 年中开源的参考实现，强项是全局主题总结 + "
        "多跳 / 全局检索，弱项是索引成本高、查询慢；适合作为静态核心知识的「全局视图引擎」与 LightRAG "
        "增量更新能力互补（LazyGraphRAG 可将索引成本降至原版 0.1%）。\n"
        "· LangSmith：Anthropic 的追踪 + 评估 + A/B 测试框架，作为外部可观测性组件接入。\n"
        "· RAGAS / DeepEval：开源评估库，用于评估召回率 / 忠实度 / 幻觉率。\n"
        "· Yuxi-Know（MIT）：基于 LightRAG 的集成化 GUI 平台，提供权限管理 + 可视化，可作为参考 UI。\n"
        "· KAG（Apache 2.0，OpenSPG）：专业领域 KG 推理，Schema 约束 + 逻辑形式推理，适合医疗 / 法律。",
        size=10.5,
    )

    # Save
    doc.save(OUTPUT_PATH)
    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    build_doc()