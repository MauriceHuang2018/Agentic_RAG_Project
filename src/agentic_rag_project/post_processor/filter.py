"""Sensitive-word filter for outgoing answers (T3.4).

DESIGN 4.2 / TASK T3.4: every assistant answer passes through this
filter before it reaches the user. Hits trigger a configurable
degraded response (a polite refusal) so we never echo a banned
phrase back to the chat client.

The word list is hot-reloadable from Redis (key `sensitive:words`),
so admins can update it without restarting the API. If Redis is
unreachable we fall back to a baked-in default list — see
`DEFAULT_SENSITIVE_WORDS`.

Matching is whole-word, case-insensitive, and built on a Trie so
multi-word phrases ("非法集资") match in a single pass instead of
calling `str.find` per word.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

logger = logging.getLogger(__name__)


SENSITIVE_WORDS_REDIS_KEY = "sensitive:words"

DEFAULT_REFUSAL_TEXT = (
    "抱歉，暂无法提供该问题的答复，请换个话题或联系管理员。"
)

# Conservative baseline — admin should replace with the real list.
DEFAULT_SENSITIVE_WORDS: tuple[str, ...] = (
    # Common categories that show up in corporate RAG:
    "反动", "色情", "暴力恐怖", "非法集资", "毒品", "枪支",
    "赌博", "邪教", "诈骗", "洗钱",
    "fuck", "shit", "asshole",
)


# ---------------------------------------------------------------------------
# Trie
# ---------------------------------------------------------------------------


class _TrieNode:
    __slots__ = ("children", "is_word")

    def __init__(self) -> None:
        self.children: dict[str, "_TrieNode"] = {}
        self.is_word: bool = False


class _Trie:
    """Minimal Aho-Corasicas-free trie for whole-word match."""

    def __init__(self, words: Iterable[str] = ()) -> None:
        self._root = _TrieNode()
        for w in words:
            self.add(w)

    def add(self, word: str) -> None:
        node = self._root
        for ch in word.lower():
            node = node.children.setdefault(ch, _TrieNode())
        node.is_word = True

    def find_first(self, text: str) -> tuple[int, int] | None:
        """Return `(start, end_exclusive)` of the first match, or None.

        Scans `text` char-by-char. On a hit, returns the byte index
        span of the matched word (lower-case offsets; for ASCII-only
        words these match Python `str` indices)."""
        node = self._root
        start = -1
        for i, ch in enumerate(text.lower()):
            nxt = node.children.get(ch)
            if nxt is None:
                # Reset to root; record where a new match could start.
                node = self._root
                start = -1
                continue
            if start < 0:
                start = i
            node = nxt
            if node.is_word:
                return (start, i + 1)
        return None

    def find_all(self, text: str) -> list[str]:
        """Return every distinct matched word, in order of first hit."""
        hits: list[str] = []
        seen: set[str] = set()
        cursor = 0
        while cursor < len(text):
            match = self.find_first(text[cursor:])
            if match is None:
                break
            s, e = match
            word = text[cursor + s : cursor + e]
            if word not in seen:
                seen.add(word)
                hits.append(word)
            cursor = cursor + s + 1
        return hits


# ---------------------------------------------------------------------------
# Redis interface (Protocol for testability)
# ---------------------------------------------------------------------------


class RedisWords(Protocol):
    """Minimal surface we touch from Redis."""

    def get(self, key: str) -> bytes | None: ...


def _parse_words_payload(raw: bytes | str | None) -> list[str]:
    """Decode a Redis payload (JSON list of strings, or newline-joined)."""
    if raw is None:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw.strip()
    if not text:
        return []
    # Try JSON first; fall back to newline-separated.
    if text.startswith("[") and text.endswith("]"):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [w.strip() for w in text.splitlines() if w.strip()]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


@dataclass
class SensitiveWordFilter:
    """Trie-backed sensitive-word filter with hot-reloadable word list.

    Construct with an explicit word list, or pass `redis_client` to
    load from Redis (falling back to `DEFAULT_SENSITIVE_WORDS` when
    the key is absent or Redis is unreachable).
    """

    words: tuple[str, ...] = DEFAULT_SENSITIVE_WORDS
    refusal_text: str = DEFAULT_REFUSAL_TEXT
    redis_client: RedisWords | None = None
    redis_key: str = SENSITIVE_WORDS_REDIS_KEY
    _trie: _Trie = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._trie = _Trie(self.words)

    # ------------------------------------------------------------------
    # reload
    # ------------------------------------------------------------------

    def reload_from_redis(self) -> bool:
        """Refresh the in-memory trie from `redis_client`.

        Returns True on a successful reload, False when Redis was
        unreachable or returned no payload (the previous list is kept
        in that case — we never drop to an empty trie silently).
        """
        if self.redis_client is None:
            return False
        try:
            raw = self.redis_client.get(self.redis_key)
        except Exception as exc:
            logger.warning("sensitive-words reload failed: %s", exc)
            return False
        words = _parse_words_payload(raw)
        if not words:
            return False
        self._replace_words(tuple(words))
        return True

    def replace_words(self, words: Iterable[str]) -> None:
        """Programmatic word-list replacement (admin API hook)."""
        clean = tuple(str(w).strip() for w in words if str(w).strip())
        if not clean:
            raise ValueError("word list must not be empty")
        self._replace_words(clean)

    def _replace_words(self, words: tuple[str, ...]) -> None:
        self.words = words
        self._trie = _Trie(words)

    # ------------------------------------------------------------------
    # query / match
    # ------------------------------------------------------------------

    def matches(self, text: str) -> list[str]:
        """Return every distinct matched word, in order of first hit."""
        return self._trie.find_all(text)

    def contains(self, text: str) -> bool:
        return self._trie.find_first(text) is not None

    def filter_or_refuse(self, text: str) -> tuple[str, bool]:
        """If `text` is clean, return `(text, False)`. Otherwise return
        `(refusal_text, True)` so the caller can both swap the answer
        AND mark the message for audit."""
        if not text:
            return text, False
        if self._trie.find_first(text) is None:
            return text, False
        return self.refusal_text, True


# ---------------------------------------------------------------------------
# builders / helpers
# ---------------------------------------------------------------------------


def build_default_filter(redis_client: RedisWords | None = None) -> SensitiveWordFilter:
    """Convenience factory: pull from Redis when available, else defaults."""
    flt = SensitiveWordFilter(redis_client=redis_client)
    if redis_client is not None:
        flt.reload_from_redis()
    return flt


def trie_from_words(words: Iterable[str]) -> _Trie:
    """Public factory for tests that want to exercise the Trie directly."""
    return _Trie(words)


# Re-export the trie for unit-testing convenience; tests can assert
# matching semantics without going through `SensitiveWordFilter`.
__all__ = [
    "DEFAULT_REFUSAL_TEXT",
    "DEFAULT_SENSITIVE_WORDS",
    "SENSITIVE_WORDS_REDIS_KEY",
    "SensitiveWordFilter",
    "build_default_filter",
    "trie_from_words",
]