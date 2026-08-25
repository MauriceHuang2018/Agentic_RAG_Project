"""PII masker for outgoing answers (T3.4).

DESIGN 4.2 / TASK T3.4: phone numbers, mainland-China ID numbers,
and email addresses detected in the answer are replaced with a
mask character before the response leaves the API. The patterns
live here (not in config) so a single import gets you the default
behavior; `Masker` accepts custom patterns so the admin can tighten
or loosen detection without code changes.

Patterns are conservative: false positives are preferred over leaks,
so this is one of the few places where we err on the side of
masking too aggressively. Each match becomes the same number of `*`
characters as the original so position-dependent downstream rendering
(footnotes, line lengths) is preserved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


DEFAULT_MASK_CHAR = "*"

# Mainland-China mobile (11-digit, starts with 1[3-9]).
DEFAULT_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# Mainland-China resident ID (18 chars: 17 digits + 1 digit/X).
DEFAULT_ID_PATTERN = re.compile(r"(?<!\d)[1-9]\d{16}[\dXx](?!\d)")

# Email (RFC-pragmatic; full RFC 5322 is much uglier than needed here).
DEFAULT_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)

DEFAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("phone", DEFAULT_PHONE_PATTERN),
    ("id_card", DEFAULT_ID_PATTERN),
    ("email", DEFAULT_EMAIL_PATTERN),
)


def _mask_match(match: re.Match[str], char: str) -> str:
    return char * len(match.group(0))


@dataclass
class Masker:
    """Apply a sequence of redaction patterns to a string.

    Each pattern is tagged with a label (`phone`, `id_card`, `email`)
    so audit logs can record *which* kind of PII was redacted without
    recording the value itself. Use `summarize` to get the per-label
    hit counts after a `mask` call.
    """

    patterns: tuple[tuple[str, re.Pattern[str]], ...] = DEFAULT_PATTERNS
    mask_char: str = DEFAULT_MASK_CHAR
    _last_summary: dict[str, int] = field(init=False, repr=False)
    _last_replaced_count: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.mask_char:
            raise ValueError("mask_char must be a single character")
        self._last_summary = {}
        self._last_replaced_count = 0

    # ------------------------------------------------------------------
    # main API
    # ------------------------------------------------------------------

    def mask(self, text: str) -> str:
        """Apply every pattern in order; return the redacted text.

        Idempotent — running mask on already-masked text yields the same
        output (the patterns are designed not to match the mask char)."""
        if not text:
            self._last_summary = {}
            self._last_replaced_count = 0
            return text
        summary: dict[str, int] = {}
        out = text
        for label, pattern in self.patterns:
            out, count = pattern.subn(
                lambda m: _mask_match(m, self.mask_char), out
            )
            if count:
                summary[label] = summary.get(label, 0) + count
        self._last_summary = summary
        self._last_replaced_count = sum(summary.values())
        return out

    @property
    def last_summary(self) -> dict[str, int]:
        """Per-label hit counts from the most recent `mask()` call."""
        return dict(self._last_summary)

    @property
    def last_replaced_count(self) -> int:
        """Total redactions from the most recent `mask()` call."""
        return self._last_replaced_count

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def detect(self, text: str) -> dict[str, list[str]]:
        """Return `{label: [matched_strings, ...]}` without mutating the input.

        Useful when the caller wants to decide policy *before* masking
        (e.g. escalate to admin instead of mask, in T5.5)."""
        out: dict[str, list[str]] = {}
        if not text:
            return out
        for label, pattern in self.patterns:
            matches = pattern.findall(text)
            if matches:
                out[label] = matches
        return out


def build_default_masker() -> Masker:
    """Convenience factory for tests and the chat endpoint."""
    return Masker()


def all_default_labels() -> tuple[str, ...]:
    return tuple(label for label, _ in DEFAULT_PATTERNS)


__all__ = [
    "DEFAULT_EMAIL_PATTERN",
    "DEFAULT_ID_PATTERN",
    "DEFAULT_MASK_CHAR",
    "DEFAULT_PATTERNS",
    "DEFAULT_PHONE_PATTERN",
    "Masker",
    "all_default_labels",
    "build_default_masker",
]
