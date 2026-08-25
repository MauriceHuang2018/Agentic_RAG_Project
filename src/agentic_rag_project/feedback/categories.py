"""Predefined attribution categories (T4.2).

The five system categories are seeded at first startup by
`seed_default_categories()`. Admins can add more via the
`POST /feedback/categories` endpoint; we never auto-remove
system rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategorySpec:
    """Definition of one built-in category row."""

    key: str
    name_zh: str
    description: str


DEFAULT_CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec(
        key="retrieval",
        name_zh="检索问题",
        description=(
            "召回率不足，正确答案未进入 Top-K。典型表现：用户问'年假"
            "天数'，检索结果全是'考勤制度'，没检索到《员工手册》。"
        ),
    ),
    CategorySpec(
        key="chunking",
        name_zh="切片问题",
        description=(
            "检索到了文档，但关键信息被切碎了。典型表现：检索到《员工"
            "手册》的 chunk，但'年假 15 天'这句话被切成了'年假'和"
            "'15 天'两个不相关的 chunk。"
        ),
    ),
    CategorySpec(
        key="generation",
        name_zh="生成问题",
        description=(
            "检索内容正确，但模型幻觉或过度推理。典型表现：检索到"
            "'年假 15 天'，模型回答'年假 10 天'（幻觉）；或模型擅自"
            "添加了原文没有的'但需提前一个月申请'。"
        ),
    ),
    CategorySpec(
        key="knowledge",
        name_zh="知识库问题",
        description=(
            "文档本身过期、错误、矛盾。典型表现：检索到《员工手册 "
            "2024 版》说年假 10 天，但 2026 版已改为 15 天；或两份"
            "文档给出矛盾答案。"
        ),
    ),
    CategorySpec(
        key="user_query",
        name_zh="用户问题",
        description=(
            "问题模糊、超出知识库范围、恶意测试。典型表现：用户问"
            "'明天彩票号码是多少'；或'用一句话总结我们公司所有产品'"
            "（过于宽泛）。"
        ),
    ),
)


def find_by_key(key: str) -> CategorySpec | None:
    """Return the spec for `key`, or None if it's not a built-in."""
    for spec in DEFAULT_CATEGORIES:
        if spec.key == key:
            return spec
    return None


__all__ = [
    "DEFAULT_CATEGORIES",
    "CategorySpec",
    "find_by_key",
]
