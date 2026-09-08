"""Guardrail detection rules (M4.3.2 / TASK §三.4).

Four classes per Decision B:
  1. `sensitive_word`  — trie-backed, mirrors the existing
     `post_processor.SensitiveWordFilter`. Hot-reloadable from Redis.
  2. `prompt_injection` — 5 regex patterns covering the common
     jailbreak / instruction-override phrasings.
  3. `PII`             — reuses the phone / id-card / email regex
     patterns from `post_processor.mask` so we don't ship two
     divergent PII detectors.
  4. `out_of_scope`    — keyword set covering the topics the RAG
     corpus does not cover (medical / legal / financial / political
     etc.). Hits return a soft-decline + helpful redirect.

Keeping the lists in one module makes it trivial to audit
("what is blocked today?") and easy for ops to extend without
touching the checker logic.
"""
from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# 1. sensitive_word — handled by `SensitiveWordFilter` (re-used).
#    Listed here for visibility only; the checker delegates to the filter
#    so the trie + Redis hot-reload stay in one place.
# ---------------------------------------------------------------------------

# Mirrors `post_processor.filter.DEFAULT_SENSITIVE_WORDS`. Listed
# verbatim so a static reviewer can audit the default scope without
# cross-referencing another module.
DEFAULT_SENSITIVE_WORDS: Final[tuple[str, ...]] = (
    # 政治 / 反动
    "反动", "邪教", "颠覆国家",
    # 暴力 / 恐怖
    "暴力恐怖", "枪支", "毒品",
    # 经济犯罪
    "非法集资", "洗钱", "赌博", "诈骗",
    # 黄色 / 不雅
    "色情", "裸聊", "fuck", "shit", "asshole",
    # 其它
    "自杀方法", "炸弹制作",
)

# ---------------------------------------------------------------------------
# 2. prompt_injection — 5 regex patterns (DESIGN §4.6.3).
# ---------------------------------------------------------------------------

PROMPT_INJECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # "ignore previous instructions" / 忽略之前的指示
    re.compile(
        r"(?i)\b(ignore|forget|disregard|override)\s+"
        r"(all\s+)?(previous|prior|above|earlier)\s+"
        r"(instructions?|prompts?|rules?)\b"
    ),
    # "you are now X" / 角色重写
    re.compile(
        r"(?i)\byou\s+are\s+now\s+(a|an|the)\s+"
        r"(developer|admin|root|jailbreak|evil)\b"
    ),
    # system-prompt extraction attempts
    re.compile(
        r"(?i)\b(show|reveal|print|output)\s+"
        r"(your|the)\s+(system|initial|original)\s+"
        r"(prompt|instructions?|message)\b"
    ),
    # "do anything now" / DAN-style
    re.compile(
        r"(?i)\b(do\s+anything\s+now|DAN\s+mode|"
        r"developer\s+mode\s+enabled|jailbroken)\b"
    ),
    # 中文：忽略之前的指令 / 你是另一个AI
    re.compile(
        r"(忽略|无视)\s*(之前|以上|前面)\s*"
        r"(的|所有)?\s*(指示|指令|规则|提示)"
    ),
)

# ---------------------------------------------------------------------------
# 3. PII — reused from `post_processor.mask`.
# ---------------------------------------------------------------------------

# Importing the compiled patterns keeps a single source of truth.
from agentic_rag_project.post_processor.mask import (  # noqa: E402
    DEFAULT_EMAIL_PATTERN,
    DEFAULT_ID_PATTERN,
    DEFAULT_PHONE_PATTERN,
)

PII_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("phone", DEFAULT_PHONE_PATTERN),
    ("id_card", DEFAULT_ID_PATTERN),
    ("email", DEFAULT_EMAIL_PATTERN),
)

# ---------------------------------------------------------------------------
# 4. out_of_scope — 30+ topics the RAG corpus deliberately does not cover.
# ---------------------------------------------------------------------------

OUT_OF_SCOPE_KEYWORDS: Final[tuple[str, ...]] = (
    # 医疗
    "诊断", "处方", "用药剂量", "手术方案", "病情", "重症", "癌症治疗",
    # 法律
    "诉讼策略", "判决预测", "罪名认定", "起诉书",
    # 金融投资
    "股票推荐", "买入建议", "涨跌预测", "内幕消息",
    # 政治 / 外交
    "领导人评价", "政治敏感", "外交立场",
    # 色情 / 暴力
    "色情", "裸聊", "自残",
    # 其它（与产品定位无关）
    "算命", "星座运势", "博彩下注",
)
