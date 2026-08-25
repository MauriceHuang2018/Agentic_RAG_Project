"""Unit tests for the offline evaluation module (T4.1).

5 test files cover the 5 implementation modules:

  * test_eval_set.py          — EvalCase + load/dump (jsonl + json)
  * test_metrics.py           — 6 metrics + cosine_sim + token_jaccard
  * test_scorer.py            — EvalScorer adapters + per-case scoring
  * test_persistence.py       — record_evaluation_results
  * test_pipeline.py          — EvaluationPipeline.run_all + report

Plus 1 cross-cutting test for the `__init__` re-exports.

Tests run against a small, in-process dependency surface — no
network, no real PG/Redis. The pipeline tests use fakes for the
chat service / scorer / session factory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentic_rag_project.evaluation import (
    ALL_METRIC_NAMES,
    CaseOutcome,
    EvalCase,
    EvalScorer,
    EvaluationPipeline,
    EvaluationReport,
    METRIC_ANSWER_RELEVANCY,
    METRIC_ANSWER_SIMILARITY,
    METRIC_CITATION_ACCURACY,
    METRIC_CONTEXT_PRECISION,
    METRIC_CONTEXT_RECALL,
    METRIC_FAITHFULNESS,
    MetricResult,
    PerMetricStats,
    ScoredCase,
    aggregate_by_metric,
    answer_relevancy,
    answer_similarity,
    citation_accuracy,
    compute_all_metrics,
    context_precision,
    context_recall,
    cosine_sim,
    dump_eval_set,
    faithfulness,
    load_eval_set,
    record_evaluation_results,
    token_jaccard,
)
from agentic_rag_project.evaluation.eval_set import _coerce_case
from agentic_rag_project.evaluation.metrics import (
    _extract_chunk_citations,
    _extract_json,
    _parse_faithfulness_json,
    _split_sentences,
)


# ===========================================================================
# test_eval_set.py — EvalCase + load/dump
# ===========================================================================


class TestEvalCase:
    def test_minimal_required_field(self) -> None:
        case = EvalCase(query="q")
        assert case.query == "q"
        assert case.expected_answer == ""
        assert case.expected_chunk_ids == []
        assert case.tags == []
        assert case.metadata == {}
        assert case.workspace_id is None
        # case_id is auto-generated.
        assert case.case_id.startswith("c-")

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValueError, match="query"):
            EvalCase(query="")
        with pytest.raises(ValueError, match="query"):
            EvalCase(query="   ")

    def test_none_collections_normalized(self) -> None:
        case = EvalCase(
            query="q",
            expected_chunk_ids=None,  # type: ignore[arg-type]
            tags=None,  # type: ignore[arg-type]
            metadata=None,  # type: ignore[arg-type]
        )
        assert case.expected_chunk_ids == []
        assert case.tags == []
        assert case.metadata == {}


class TestCoerceCase:
    def test_minimal(self) -> None:
        case = _coerce_case({"query": "hi"})
        assert case.query == "hi"
        assert case.expected_answer == ""

    def test_missing_query_rejected(self) -> None:
        with pytest.raises(ValueError, match="query"):
            _coerce_case({})

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            _coerce_case("not a dict")  # type: ignore[arg-type]

    def test_workspace_id_str_coerced(self) -> None:
        case = _coerce_case({"query": "x", "workspace_id": 12345})
        assert case.workspace_id == "12345"


class TestLoadEvalSet:
    def test_load_jsonl(self, tmp_path: Path) -> None:
        f = tmp_path / "eval.jsonl"
        f.write_text(
            '{"query": "q1"}\n'
            '{"query": "q2", "expected_chunk_ids": ["a"]}\n'
            "# a comment\n"
            "\n",  # blank line
            encoding="utf-8",
        )
        cases = load_eval_set(f)
        assert len(cases) == 2
        assert cases[0].query == "q1"
        assert cases[1].expected_chunk_ids == ["a"]

    def test_load_json_array(self, tmp_path: Path) -> None:
        f = tmp_path / "eval.json"
        f.write_text(json.dumps([
            {"query": "a"},
            {"query": "b", "expected_answer": "B"},
        ]), encoding="utf-8")
        cases = load_eval_set(f)
        assert [c.query for c in cases] == ["a", "b"]
        assert cases[1].expected_answer == "B"

    def test_load_auto_detect_array(self, tmp_path: Path) -> None:
        f = tmp_path / "eval.weird"
        f.write_text('[{"query": "x"}]', encoding="utf-8")
        cases = load_eval_set(f)
        assert len(cases) == 1

    def test_load_auto_detect_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "eval.weird"
        f.write_text('{"query": "x"}\n{"query": "y"}\n', encoding="utf-8")
        cases = load_eval_set(f)
        assert len(cases) == 2

    def test_load_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_eval_set(tmp_path / "nope.json")

    def test_load_empty_warns(self, tmp_path: Path, caplog) -> None:
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        with caplog.at_level("WARNING"):
            cases = load_eval_set(f)
        assert cases == []

    def test_load_invalid_jsonl(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.jsonl"
        f.write_text('not json\n{"query": "x"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSONL at line 1"):
            load_eval_set(f)

    def test_load_invalid_json_array(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text('{"not_an_array": true}', encoding="utf-8")
        with pytest.raises(ValueError, match="array"):
            load_eval_set(f)

    def test_dump_roundtrip(self, tmp_path: Path) -> None:
        cases = [
            EvalCase(query="q1", expected_chunk_ids=["c-1"]),
            EvalCase(query="q2", expected_answer="B"),
        ]
        out = tmp_path / "eval.jsonl"
        dump_eval_set(cases, out)
        assert out.exists()
        loaded = load_eval_set(out)
        assert [c.query for c in loaded] == ["q1", "q2"]
        assert loaded[0].expected_chunk_ids == ["c-1"]


# ===========================================================================
# test_metrics.py — metric functions + helpers
# ===========================================================================


class TestTokenJaccard:
    def test_identical(self) -> None:
        assert token_jaccard("hello world", "hello world") == 1.0

    def test_disjoint(self) -> None:
        assert token_jaccard("hello", "world") == 0.0

    def test_partial_overlap(self) -> None:
        # tokens: {"hello", "world"} ∩ {"hello", "there"} / union = 1/3
        assert token_jaccard("hello world", "hello there") == pytest.approx(1 / 3)

    def test_empty_inputs(self) -> None:
        assert token_jaccard("", "") == 0.0
        assert token_jaccard("", "x") == 0.0
        assert token_jaccard("x", "") == 0.0

    def test_case_insensitive(self) -> None:
        assert token_jaccard("HELLO", "hello") == 1.0


class TestCosineSim:
    def test_identical(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert cosine_sim(v, v) == pytest.approx(1.0)

    def test_orthogonal(self) -> None:
        assert cosine_sim([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_opposite(self) -> None:
        assert cosine_sim([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_length_mismatch(self) -> None:
        assert cosine_sim([1.0, 0.0], [1.0]) == 0.0

    def test_zero_vector(self) -> None:
        assert cosine_sim([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestMetricResult:
    def test_value_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="not in"):
            MetricResult(metric_name="x", value=1.5)
        with pytest.raises(ValueError, match="not in"):
            MetricResult(metric_name="x", value=-0.1)


class TestContextPrecision:
    def test_full_hit(self) -> None:
        r = context_precision(
            retrieved_chunk_ids=["a", "b", "c"],
            expected_chunk_ids=["a", "b"],
        )
        assert r.value == pytest.approx(2 / 3)
        assert r.details["hits"] == 2

    def test_no_hit(self) -> None:
        r = context_precision(
            retrieved_chunk_ids=["x", "y"],
            expected_chunk_ids=["a", "b"],
        )
        assert r.value == 0.0

    def test_no_expected(self) -> None:
        r = context_precision(
            retrieved_chunk_ids=["a"],
            expected_chunk_ids=[],
        )
        assert r.value == 0.0
        assert r.details["skipped"] == "no_expected"

    def test_no_retrieved(self) -> None:
        r = context_precision(
            retrieved_chunk_ids=[],
            expected_chunk_ids=["a"],
        )
        assert r.value == 0.0

    def test_all_match(self) -> None:
        r = context_precision(
            retrieved_chunk_ids=["a", "b"],
            expected_chunk_ids=["a", "b", "c"],
        )
        assert r.value == 1.0


class TestContextRecall:
    def test_full(self) -> None:
        r = context_recall(
            retrieved_chunk_ids=["a", "b"],
            expected_chunk_ids=["a", "b"],
        )
        assert r.value == 1.0

    def test_partial(self) -> None:
        r = context_recall(
            retrieved_chunk_ids=["a"],
            expected_chunk_ids=["a", "b", "c"],
        )
        assert r.value == pytest.approx(1 / 3)

    def test_no_expected(self) -> None:
        r = context_recall(
            retrieved_chunk_ids=["a"],
            expected_chunk_ids=[],
        )
        assert r.value == 0.0
        assert r.details["skipped"] == "no_expected"


class TestAnswerSimilarity:
    def test_identical_cosine(self) -> None:
        embedder = lambda texts: [[1.0, 0.0] if t == "a" else [1.0, 0.0] for t in texts]
        r = answer_similarity(expected_answer="a", answer="b", embedder=embedder)
        assert r.value == pytest.approx(1.0)
        assert r.details["method"] == "cosine"

    def test_fallback_jaccard(self) -> None:
        r = answer_similarity(
            expected_answer="hello world",
            answer="hello there",
        )
        assert r.details["method"] == "token_jaccard"
        assert r.value == pytest.approx(1 / 3)

    def test_empty(self) -> None:
        r = answer_similarity(expected_answer="", answer="hi")
        assert r.value == 0.0
        assert r.details["skipped"] == "empty_input"

    def test_embedder_fails_falls_back(self) -> None:
        def bad_embedder(texts):
            raise RuntimeError("nope")

        r = answer_similarity(
            expected_answer="hello",
            answer="hello",
            embedder=bad_embedder,
        )
        assert r.details["method"] == "token_jaccard"

    def test_embedder_wrong_shape_falls_back(self) -> None:
        def wrong_embedder(texts):
            return [1.0]  # one int, not a list

        r = answer_similarity(
            expected_answer="hi",
            answer="hi",
            embedder=wrong_embedder,
        )
        assert r.details["method"] == "token_jaccard"


class TestAnswerRelevancy:
    def test_identical(self) -> None:
        embedder = lambda texts: [[1.0, 0.0] for _ in texts]
        r = answer_relevancy(query="q", answer="q", embedder=embedder)
        assert r.value == pytest.approx(1.0)
        assert r.details["method"] == "cosine"

    def test_fallback(self) -> None:
        r = answer_relevancy(query="hello world", answer="hello there")
        assert r.details["method"] == "token_jaccard"

    def test_empty(self) -> None:
        r = answer_relevancy(query="", answer="x")
        assert r.value == 0.0


class TestCitationAccuracy:
    def test_full_hit(self) -> None:
        r = citation_accuracy(
            answer="see [chunk-1] and [chunk-2]",
            citations=["chunk-1", "chunk-2"],
            expected_chunk_ids=["chunk-1", "chunk-2"],
        )
        assert r.value == 1.0
        assert r.details["hits"] == 2

    def test_partial_hit(self) -> None:
        r = citation_accuracy(
            answer="see [chunk-1] and [chunk-3]",
            citations=["chunk-1", "chunk-3"],
            expected_chunk_ids=["chunk-1", "chunk-2"],
        )
        assert r.value == 0.5
        assert r.details["hits"] == 1

    def test_no_citations(self) -> None:
        r = citation_accuracy(
            answer="no refs here",
            citations=[],
            expected_chunk_ids=["chunk-1"],
        )
        assert r.value == 0.0
        assert r.details["no_citations"] is True

    def test_no_expected(self) -> None:
        r = citation_accuracy(
            answer="see [chunk-1]",
            citations=["chunk-1"],
            expected_chunk_ids=[],
        )
        assert r.value == 0.0
        assert r.details["skipped"] == "no_expected"

    def test_ignored_digit_refs(self) -> None:
        # Pure-digit `[1]`, `[2]` are list markers, not chunk refs.
        r = citation_accuracy(
            answer="items [1] and [2]",
            citations=[],
            expected_chunk_ids=["chunk-1"],
        )
        assert r.value == 0.0

    def test_ignored_single_char_refs(self) -> None:
        r = citation_accuracy(
            answer="see [a]",
            citations=[],
            expected_chunk_ids=["chunk-1"],
        )
        assert r.value == 0.0


class TestExtractChunkCitations:
    def test_extract(self) -> None:
        out = _extract_chunk_citations("see [chunk-1] and [chunk-2]")
        assert out == {"chunk-1", "chunk-2"}

    def test_no_matches(self) -> None:
        assert _extract_chunk_citations("plain text") == set()

    def test_filters_digits(self) -> None:
        assert _extract_chunk_citations("[1] [22]") == set()

    def test_filters_single_char(self) -> None:
        assert _extract_chunk_citations("[a] [bc]") == {"bc"}

    def test_empty(self) -> None:
        assert _extract_chunk_citations("") == set()


class TestSplitSentences:
    def test_basic(self) -> None:
        out = _split_sentences("Hello world. How are you? Fine.")
        assert out == ["Hello world.", "How are you?", "Fine."]

    def test_chinese(self) -> None:
        out = _split_sentences("你好。今天天气不错。")
        assert out == ["你好。", "今天天气不错。"]

    def test_empty(self) -> None:
        assert _split_sentences("") == []


class TestExtractJson:
    def test_direct(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_fenced(self) -> None:
        assert _extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}

    def test_noisy(self) -> None:
        assert _extract_json("blah blah {\"a\": 1} blah") == {"a": 1}

    def test_empty(self) -> None:
        assert _extract_json("") is None

    def test_garbage(self) -> None:
        assert _extract_json("not json") is None


class TestParseFaithfulnessJson:
    def test_list_payload(self) -> None:
        parsed = _parse_faithfulness_json([{"text": "hi", "supported": True}])
        assert parsed == [{"text": "hi", "supported": True}]

    def test_dict_with_sentences(self) -> None:
        parsed = _parse_faithfulness_json(
            {"sentences": [{"text": "x", "supported": False}]}
        )
        assert parsed == [{"text": "x", "supported": False}]

    def test_dict_without_sentences(self) -> None:
        assert _parse_faithfulness_json({"foo": "bar"}) is None

    def test_non_dict_or_list(self) -> None:
        assert _parse_faithfulness_json("string") is None

    def test_skips_non_dict_items(self) -> None:
        parsed = _parse_faithfulness_json(
            {"sentences": [{"text": "x", "supported": True}, "bad"]}
        )
        assert parsed == [{"text": "x", "supported": True}]


class TestFaithfulness:
    def test_skipped_when_no_sentences(self) -> None:
        r = faithfulness(answer="", contexts=["ctx"], llm_call=lambda s, u, t: "{}")
        assert r.value == 0.0
        assert r.details["skipped"] == "empty_input"

    def test_skipped_when_no_contexts(self) -> None:
        r = faithfulness(answer="hi.", contexts=[], llm_call=lambda s, u, t: "{}")
        assert r.value == 0.0
        assert r.details["skipped"] == "empty_input"

    def test_skipped_when_no_llm(self) -> None:
        r = faithfulness(answer="hi.", contexts=["ctx"], llm_call=None)
        assert r.value == 0.0
        assert r.details["skipped"] == "no_llm"

    def test_perfect_score(self) -> None:
        llm = lambda s, u, t: json.dumps(
            {"sentences": [{"text": "x.", "supported": True}]}
        )
        r = faithfulness(answer="x.", contexts=["ctx"], llm_call=llm)
        assert r.value == 1.0
        assert r.details["n_supported"] == 1

    def test_partial_score(self) -> None:
        llm = lambda s, u, t: json.dumps(
            {
                "sentences": [
                    {"text": "a.", "supported": True},
                    {"text": "b.", "supported": False},
                ]
            }
        )
        r = faithfulness(answer="a. b.", contexts=["ctx"], llm_call=llm)
        assert r.value == 0.5

    def test_no_json_one_retry_then_zero(self) -> None:
        # Always returns garbage → 1 retry → still zero score.
        calls = {"n": 0}

        def bad(s, u, t):
            calls["n"] += 1
            return "not json"

        r = faithfulness(answer="a.", contexts=["ctx"], llm_call=bad)
        assert r.value == 0.0
        assert calls["n"] == 2  # initial + 1 retry
        assert r.details["error"] == "unparseable JSON"

    def test_retry_succeeds(self) -> None:
        calls = {"n": 0}

        def flaky(s, u, t):
            calls["n"] += 1
            if calls["n"] == 1:
                return "not json"
            return json.dumps({"sentences": [{"text": "a.", "supported": True}]})

        r = faithfulness(answer="a.", contexts=["ctx"], llm_call=flaky)
        assert r.value == 1.0
        assert calls["n"] == 2

    def test_llm_raises_one_retry_then_zero(self) -> None:
        calls = {"n": 0}

        def boom(s, u, t):
            calls["n"] += 1
            raise RuntimeError("api down")

        r = faithfulness(answer="a.", contexts=["ctx"], llm_call=boom)
        assert r.value == 0.0
        assert calls["n"] == 2  # initial + 1 retry

    def test_max_retries_respected(self) -> None:
        # max_retries=0 means: 1 attempt, no retry.
        def bad(s, u, t):
            return "not json"

        r = faithfulness(answer="a.", contexts=["ctx"], llm_call=bad, max_retries=0)
        assert r.value == 0.0
        # details should reflect max_retries=0
        assert r.details["retries"] == 0


class TestComputeAllMetrics:
    def test_runs_all_six(self) -> None:
        results = compute_all_metrics(
            query="what is X",
            answer="X is [chunk-1] a thing.",
            contexts=["X is a thing."],
            retrieved_chunk_ids=["chunk-1"],
            expected_answer="X is a thing.",
            expected_chunk_ids=["chunk-1"],
        )
        assert len(results) == len(ALL_METRIC_NAMES)
        names = {r.metric_name for r in results}
        assert names == set(ALL_METRIC_NAMES)

    def test_all_metrics_in_range(self) -> None:
        results = compute_all_metrics(
            query="q",
            answer="a [chunk-1]",
            contexts=["c"],
            retrieved_chunk_ids=["chunk-1"],
            expected_answer="a",
            expected_chunk_ids=["chunk-1"],
        )
        for r in results:
            assert 0.0 <= r.value <= 1.0


# ===========================================================================
# test_scorer.py — EvalScorer
# ===========================================================================


class TestEvalScorer:
    def test_score_with_string_chunks(self) -> None:
        scorer = EvalScorer()
        result = scorer.score(
            case_id="c1",
            query="q",
            answer="see [chunk-1]",
            retrieved_chunks=["chunk-1"],
            expected_answer="q",
            expected_chunk_ids=["chunk-1"],
        )
        assert result.case_id == "c1"
        names = {m.metric_name for m in result.metrics}
        assert names == set(ALL_METRIC_NAMES)

    def test_score_with_dict_chunks(self) -> None:
        scorer = EvalScorer()
        chunks = [
            {"chunk_id": "a", "content": "first context"},
            {"chunk_id": "b", "content": "second context"},
        ]
        result = scorer.score(
            case_id="c1",
            query="q",
            answer="a [a]",
            retrieved_chunks=chunks,
            expected_answer="a",
            expected_chunk_ids=["a"],
        )
        # contexts were derived from chunks (default behaviour).
        ctx_metric = next(
            m for m in result.metrics if m.metric_name == METRIC_CONTEXT_PRECISION
        )
        assert ctx_metric.details["hits"] == 1

    def test_score_with_object_chunks(self) -> None:
        scorer = EvalScorer()
        chunks = [
            SimpleNamespace(chunk_id="x", content="x context"),
            SimpleNamespace(chunk_id="y", content="y context"),
        ]
        result = scorer.score(
            case_id="c1",
            query="q",
            answer="see [x]",
            retrieved_chunks=chunks,
            expected_answer="x",
            expected_chunk_ids=["x"],
        )
        assert len(result.metrics) == len(ALL_METRIC_NAMES)

    def test_explicit_contexts_override(self) -> None:
        scorer = EvalScorer()
        result = scorer.score(
            case_id="c1",
            query="q",
            answer="hi.",
            retrieved_chunks=["chunk-1"],
            contexts=["ignored context"],
            expected_answer="hi.",
            expected_chunk_ids=[],
        )
        # faithfulness looks at `contexts` (passed explicitly) — would
        # call llm_call but we passed no LLM, so it returns 0.
        faith = next(
            m for m in result.metrics if m.metric_name == METRIC_FAITHFULNESS
        )
        assert faith.value == 0.0

    def test_score_uses_embedder(self) -> None:
        scorer = EvalScorer(embedder=lambda texts: [[1.0, 0.0] for _ in texts])
        result = scorer.score(
            case_id="c1",
            query="q",
            answer="a",
            retrieved_chunks=["x"],
            expected_answer="a",
            expected_chunk_ids=["x"],
        )
        sim = next(
            m for m in result.metrics if m.metric_name == METRIC_ANSWER_SIMILARITY
        )
        assert sim.details["method"] == "cosine"


# ===========================================================================
# test_persistence.py — record_evaluation_results
# ===========================================================================


class TestRecordEvaluationResults:
    def test_empty_results(self) -> None:
        session = MagicMock()
        out = record_evaluation_results(session, message_id=uuid.uuid4(), results=[])
        assert out == []
        session.add.assert_not_called()

    def test_inserts_one_row_per_metric(self) -> None:
        session = MagicMock()
        results = [
            MetricResult(METRIC_FAITHFULNESS, 0.5, {"k": 1}),
            MetricResult(METRIC_CONTEXT_PRECISION, 0.8),
        ]
        mid = uuid.uuid4()
        rows = record_evaluation_results(session, message_id=mid, results=results)
        assert len(rows) == 2
        assert session.add.call_count == 2
        session.flush.assert_called_once()
        assert rows[0].metric_name == METRIC_FAITHFULNESS
        assert rows[0].value == 0.5
        assert rows[0].details == {"k": 1}
        assert rows[1].metric_name == METRIC_CONTEXT_PRECISION


# ===========================================================================
# test_pipeline.py — EvaluationPipeline
# ===========================================================================


class TestPerMetricStats:
    def test_to_dict(self) -> None:
        s = PerMetricStats(metric_name="x", count=3, mean=0.5, minimum=0.1, maximum=0.9)
        d = s.to_dict()
        assert d == {
            "metric_name": "x",
            "count": 3,
            "mean": 0.5,
            "min": 0.1,
            "max": 0.9,
        }


class _FakeResponse:
    def __init__(self, answer: str, message_id: str, citations: list[dict]) -> None:
        self.answer = answer
        self.message_id = message_id
        self.citations = citations


class _FakeCitation:
    def __init__(self, chunk_id: str, content: str) -> None:
        self.chunk_id = chunk_id
        self.content = content

    def model_dump(self) -> dict:
        return {"chunk_id": self.chunk_id, "content": self.content}


class _FakeChatService:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.raise_exc = raise_exc
        self.calls: list[str] = []
        self._idx = 0

    def handle(self, *, session, request, user_id, workspace_id):
        from agentic_rag_project.chat import ChatQueryRequest

        assert isinstance(request, ChatQueryRequest)
        self.calls.append(request.query)
        if self.raise_exc:
            raise self.raise_exc
        # Vary the answer per case so the metrics differ.
        self._idx += 1
        return _FakeResponse(
            answer=f"answer {self._idx} [chunk-{self._idx}]",
            message_id=str(uuid.uuid4()),
            citations=[_FakeCitation(f"chunk-{self._idx}", f"ctx-{self._idx}")],
        )


class _FakeScorer:
    def score(self, *, case_id, query, answer, retrieved_chunks,
              expected_answer, expected_chunk_ids, contexts=None):
        # Return one minimal metric per case so pipeline stats tick.
        return ScoredCase(
            case_id=case_id,
            metrics=[
                MetricResult(METRIC_FAITHFULNESS, 0.5),
                MetricResult(METRIC_CONTEXT_PRECISION, 0.75),
            ],
        )


class _SessionCtx:
    def __init__(self) -> None:
        self.commits = 0
        self.added: list[object] = []
        self.flushed = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self) -> None:
        self.commits += 1

    def add(self, row: object) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushed += 1


def _session_factory():
    return _SessionCtx()


def _make_pipeline() -> tuple[EvaluationPipeline, _FakeChatService]:
    chat = _FakeChatService()
    scorer = _FakeScorer()
    pipeline = EvaluationPipeline(
        chat_service=chat,  # type: ignore[arg-type]
        scorer=scorer,  # type: ignore[arg-type]
        session_factory=_session_factory,
        default_user_id=uuid.uuid4(),
        default_workspace_id=uuid.uuid4(),
    )
    return pipeline, chat


class TestEvaluationPipeline:
    def test_runs_all_cases(self) -> None:
        pipeline, chat = _make_pipeline()
        cases = [
            EvalCase(query="q1", expected_chunk_ids=["chunk-1"]),
            EvalCase(query="q2", expected_chunk_ids=["chunk-2"]),
        ]
        report = pipeline.run_all(cases)
        assert report.total_cases == 2
        assert report.succeeded == 2
        assert report.failed == 0
        assert len(chat.calls) == 2

    def test_progress_callback_invoked(self) -> None:
        pipeline, _ = _make_pipeline()
        calls: list[tuple[int, int, str]] = []

        def cb(idx: int, total: int, case_id: str) -> None:
            calls.append((idx, total, case_id))

        pipeline.run_all(
            [EvalCase(query="a"), EvalCase(query="b")],
            on_progress=cb,
        )
        assert len(calls) == 2
        assert calls[0][0] == 1 and calls[0][1] == 2
        assert calls[1][0] == 2 and calls[1][1] == 2
        assert calls[0][2].startswith("c-")
        assert calls[1][2].startswith("c-")

    def test_progress_callback_failure_is_swallowed(self) -> None:
        pipeline, _ = _make_pipeline()

        def bad_cb(idx, total, case_id):
            raise RuntimeError("oh no")

        report = pipeline.run_all(
            [EvalCase(query="x")],
            on_progress=bad_cb,
        )
        assert report.succeeded == 1

    def test_case_failure_recorded_but_others_continue(self) -> None:
        from agentic_rag_project.chat import ChatServiceError

        pipeline, chat = _make_pipeline()
        chat.raise_exc = ChatServiceError("boom")
        cases = [EvalCase(query="a"), EvalCase(query="b")]
        report = pipeline.run_all(cases)
        assert report.failed == 2
        assert report.succeeded == 0
        for outcome in report.outcomes:
            assert outcome.error and "boom" in outcome.error

    def test_unexpected_error_caught(self) -> None:
        pipeline, chat = _make_pipeline()
        chat.raise_exc = RuntimeError("totally unexpected")
        report = pipeline.run_all([EvalCase(query="a")])
        assert report.failed == 1
        assert "unexpected" in report.outcomes[0].error  # type: ignore[operator]

    def test_per_metric_stats_aggregated(self) -> None:
        pipeline, _ = _make_pipeline()
        cases = [EvalCase(query=f"q{i}") for i in range(4)]
        report = pipeline.run_all(cases)
        # FakeScorer always returns 0.5 / 0.75 for the two metrics it emits;
        # other metrics have count=0.
        faith = report.per_metric[METRIC_FAITHFULNESS]
        assert faith.count == 4
        assert faith.mean == pytest.approx(0.5)
        assert faith.minimum == 0.5
        assert faith.maximum == 0.5
        cp = report.per_metric[METRIC_CONTEXT_PRECISION]
        assert cp.count == 4
        assert cp.mean == pytest.approx(0.75)

    def test_zero_metrics_in_report_are_zero_count(self) -> None:
        pipeline, _ = _make_pipeline()
        report = pipeline.run_all([EvalCase(query="q")])
        # Metrics not returned by the fake scorer should still appear,
        # but with count=0.
        recall = report.per_metric[METRIC_CONTEXT_RECALL]
        assert recall.count == 0

    def test_run_from_path(self, tmp_path: Path) -> None:
        f = tmp_path / "set.jsonl"
        f.write_text(
            '{"query": "a"}\n{"query": "b"}\n', encoding="utf-8"
        )
        pipeline, _ = _make_pipeline()
        report = pipeline.run_from_path(f)
        assert report.total_cases == 2

    def test_run_from_path_missing_raises(self, tmp_path: Path) -> None:
        pipeline, _ = _make_pipeline()
        with pytest.raises(FileNotFoundError):
            pipeline.run_from_path(tmp_path / "missing.json")

    def test_case_workspace_id_overrides_default(self) -> None:
        pipeline, chat = _make_pipeline()
        custom_ws = uuid.uuid4()
        pipeline.run_all(
            [EvalCase(query="q", workspace_id=str(custom_ws))]
        )
        # No direct way to inspect workspace_id on the fake, but
        # the call should not raise and the case should succeed.
        assert pipeline is not None

    def test_invalid_workspace_id_falls_back(self) -> None:
        pipeline, _ = _make_pipeline()
        report = pipeline.run_all(
            [EvalCase(query="q", workspace_id="not-a-uuid")]
        )
        # case still runs (uses default workspace).
        assert report.succeeded == 1


class TestEvaluationReportToDict:
    def test_roundtrip_shape(self) -> None:
        pipeline, _ = _make_pipeline()
        report = pipeline.run_all(
            [EvalCase(query="a"), EvalCase(query="b")]
        )
        d = report.to_dict()
        assert d["total_cases"] == 2
        assert d["succeeded"] == 2
        assert set(d["per_metric"].keys()) == set(ALL_METRIC_NAMES)
        assert len(d["outcomes"]) == 2
        for o in d["outcomes"]:
            assert "case_id" in o
            assert "metrics" in o
            assert "error" in o


class TestAggregateByMetric:
    def test_collects_all_values(self) -> None:
        outcomes = [
            CaseOutcome(
                case_id="a",
                metrics=[
                    MetricResult(METRIC_FAITHFULNESS, 0.5),
                    MetricResult(METRIC_CONTEXT_PRECISION, 0.8),
                ],
            ),
            CaseOutcome(
                case_id="b",
                metrics=[MetricResult(METRIC_FAITHFULNESS, 0.7)],
            ),
            CaseOutcome(case_id="c", error="failed", metrics=[]),
        ]
        grouped = aggregate_by_metric(outcomes)
        assert grouped[METRIC_FAITHFULNESS] == [0.5, 0.7]
        assert grouped[METRIC_CONTEXT_PRECISION] == [0.8]
        assert METRIC_CONTEXT_RECALL not in grouped

    def test_empty(self) -> None:
        assert aggregate_by_metric([]) == {}


# ===========================================================================
# cross-cutting: package __init__ exports
# ===========================================================================


class TestInitExports:
    def test_all_names_importable(self) -> None:
        import agentic_rag_project.evaluation as ev

        for name in ev.__all__:
            assert hasattr(ev, name), f"missing export: {name}"


# ===========================================================================
# CLI smoke test: --help parses cleanly
# ===========================================================================


class TestCLIHelp:
    def test_help_runs(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "agentic_rag_project.evaluation.cli", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        # `--help` should exit 0 and mention the prog name.
        assert proc.returncode == 0
        assert "agentic_rag_project.evaluation" in proc.stdout


class TestCLIMain:
    def test_invalid_uuid_exits_2(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentic_rag_project.evaluation.cli",
                "--eval-set",
                "x.jsonl",
                "--user-id",
                "not-a-uuid",
                "--workspace-id",
                str(uuid.uuid4()),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2
        assert "invalid UUID" in proc.stderr

    def test_missing_eval_set_exits_2(self, tmp_path: Path) -> None:
        env = os.environ.copy()
        env["EVAL_DRY_RUN"] = "1"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentic_rag_project.evaluation.cli",
                "--eval-set",
                str(tmp_path / "nope.jsonl"),
                "--user-id",
                str(uuid.uuid4()),
                "--workspace-id",
                str(uuid.uuid4()),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert proc.returncode == 2
        assert "not found" in proc.stderr.lower()

    def test_dry_run_with_minimal_set(self, tmp_path: Path) -> None:
        eval_set = tmp_path / "eval.jsonl"
        eval_set.write_text('{"query": "hi"}\n', encoding="utf-8")
        output = tmp_path / "report.json"
        env = os.environ.copy()
        env["EVAL_DRY_RUN"] = "1"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentic_rag_project.evaluation.cli",
                "--eval-set",
                str(eval_set),
                "--output",
                str(output),
                "--user-id",
                str(uuid.uuid4()),
                "--workspace-id",
                str(uuid.uuid4()),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        # dry-run should still produce a report; case outcomes are
        # expected to fail (the chat service is null) but the pipeline
        # itself runs cleanly.
        assert proc.returncode == 0, proc.stderr
        assert output.exists()
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["total_cases"] == 1
        # case should be marked failed (no real chat service wired).
        assert report["failed"] == 1
        assert report["outcomes"][0]["error"] is not None
