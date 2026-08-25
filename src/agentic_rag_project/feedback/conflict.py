"""Document conflict / expiry detection (T4.2).

Used by `AutoAttributor` to fire the `knowledge` category. Both
checks are regex-based; we deliberately avoid LLM calls so the
attribution pipeline stays fast and reproducible.

`has_document_conflict` looks for numeric contradictions between
chunks — e.g. two chunks both contain "年假" with different
day-counts ("年假 10 天" vs "年假 15 天"). When the same
pattern shows up with two distinct numbers across chunks, we
flag it.

`is_document_expired` scans for staleness markers — explicit
"已废弃", "旧版", "obsolete", or trailing-year markers older
than a threshold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentCheckResult:
    """One document-side check's verdict."""

    matched: bool
    reason: str = ""


# Common staleness markers (zh + en).
_STALENESS_MARKERS: tuple[str, ...] = (
    "已废弃",
    "已失效",
    "旧版",
    "废弃版本",
    "作废",
    "deprecated",
    "obsolete",
    "no longer in effect",
    "outdated",
)

# Year / version patterns:
#   - "2024 版", "2026 版"        (zh)
#   - "v1.0", "v2.3"             (en)
#   - "2024-01", "2024.1"        (en date)
_VERSION_PATTERN = re.compile(
    r"(?:"
    r"(?P<zh>\d{4})\s*版"           # 2024 版
    r"|v(?P<v>\d+(?:\.\d+)*)"      # v1.0
    r"|(?P<date>\d{4})[-./](0?[1-9]|1[0-2])"  # 2024-01
    r")",
    flags=re.UNICODE,
)


def has_document_conflict(retrieved_chunks: list[str]) -> DocumentCheckResult:
    """Detect contradictions between chunks.

    We look for chunks that share a numeric "headcount" pattern but
    disagree on the number. A cheap proxy: any phrase like "N 天",
    "N%", "N 元", "N 次" with two distinct N values across chunks.
    The proxy is intentionally broad — false positives still
    classify correctly via the fallback `generation` rule.
    """
    if len(retrieved_chunks) < 2:
        return DocumentCheckResult(matched=False, reason="not_enough_chunks")

    # Pattern: "N (天|%|元|次|个|人|岁)" or zh prefix like "年假 N 天"
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(天|%|元|次|个|人|岁|小时|分|条)")
    values_per_key: dict[str, set[str]] = {}
    for chunk in retrieved_chunks:
        if not chunk:
            continue
        for m in pattern.finditer(chunk):
            number = m.group(1)
            unit = m.group(2)
            # Build the key from the CJK noun phrase right before
            # the number — usually 2-3 chars like "年假", "折扣",
            # "税率". Strip whitespace and punctuation so the
            # "key" doesn't drift based on the preceding punctuation.
            noun = _extract_cjk_noun(chunk, m.start())
            key = f"{noun}::{unit}" if noun else f"raw::{unit}"
            values_per_key.setdefault(key, set()).add(number)

    for key, values in values_per_key.items():
        if len(values) >= 2:
            return DocumentCheckResult(
                matched=True,
                reason=f"conflicting_values_in_{key}",
            )
    return DocumentCheckResult(matched=False)


def _extract_cjk_noun(text: str, end_pos: int) -> str:
    """Walk backwards from `end_pos` collecting consecutive CJK chars.

    Returns the longest contiguous CJK run that ends at `end_pos`
    (up to 4 chars). Empty if no CJK chars precede `end_pos`.
    """
    out: list[str] = []
    i = end_pos - 1
    while i >= 0 and len(out) < 4:
        ch = text[i]
        if "一" <= ch <= "鿿":
            out.append(ch)
            i -= 1
        else:
            break
    return "".join(reversed(out))


def is_document_expired(
    retrieved_chunks: list[str],
    *,
    max_year_age: int = 2,
    reference_year: int | None = None,
) -> DocumentCheckResult:
    """Detect staleness via markers or old year markers.

    Two flavours:
      * Marker-based: any chunk text contains a STALENESS keyword.
      * Year-based: chunks carry a year/version marker more than
        `max_year_age` years before `reference_year`.

    We take the FIRST hit — the attributor only needs a yes/no.
    """
    import datetime as _dt

    ref_year = reference_year or _dt.datetime.now().year

    for chunk in retrieved_chunks:
        if not chunk:
            continue
        for marker in _STALENESS_MARKERS:
            if marker in chunk:
                return DocumentCheckResult(
                    matched=True, reason=f"staleness_marker:{marker}"
                )

    oldest_year = None
    for chunk in retrieved_chunks:
        if not chunk:
            continue
        for m in _VERSION_PATTERN.finditer(chunk):
            year_str = m.group("zh") or m.group("date") or ""
            if year_str and year_str.isdigit():
                year = int(year_str)
                if 1990 <= year <= ref_year:
                    if oldest_year is None or year < oldest_year:
                        oldest_year = year
    if oldest_year is not None and (ref_year - oldest_year) > max_year_age:
        return DocumentCheckResult(
            matched=True,
            reason=f"oldest_marker_year={oldest_year}_ref={ref_year}",
        )
    return DocumentCheckResult(matched=False)


__all__ = [
    "DocumentCheckResult",
    "has_document_conflict",
    "is_document_expired",
]
