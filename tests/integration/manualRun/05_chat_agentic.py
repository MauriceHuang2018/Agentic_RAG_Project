"""M3 Live E2E — Agentic RAG + 意图识别（docs/m3_agentic_rag_intent/）

Replicates tests/integration/manualRun/04_index.py style — live infra
(litellm :4000 + qdrant :6333 + PG) with HTTP shell around :8001.

Sub-tracks:
    M3.1 — ConfidenceRouter live E2E (direct vs agent)
    M3.2 — AgentRunner live E2E (5-iter plan/retrieve/reflect/rewrite/synth)
    M3.3 — LongContextFallback live E2E
    M3.4 — PostProcessor live E2E (SensitiveWordFilter + PII Masker)

Usage:
    uv run python tests/integration/manualRun/05_chat_agentic.py [m3_1|m3_2|m3_3|m3_4|all]

Env: requires .env with LITELLM_MODEL=openai/qwen3.5-plus, build_qdrant_client
with https=False (per docs/m2_e2e_fixes/), FastAPI running on :8001, and
admin user seeded (see tools/disable_smoke_user.py for the legacy
smoke placeholder cleanup).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Windows console 默认 GBK；强制 UTF-8 输出避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

BASE = "http://localhost:8001"
USERNAME = "admin"
PASSWORD = "admin_pass"
DEFAULT_TIMEOUT = 120  # dashscope P95 ~30s; complex agent path may run 5 iters × 30s
RESULTS_PATH = Path("/tmp/m3_e2e_results.json")


def login(base: str = BASE) -> str:
    """POST /api/v1/auth/login → JWT access_token."""
    r = httpx.post(
        f"{base}/api/v1/auth/login",
        data={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def post_query(
    token: str,
    q: str,
    *,
    max_iterations: int = 5,
    workspace_id: str | None = None,
    base: str = BASE,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST /api/v1/chat/query → structured response dict."""
    body: dict[str, Any] = {"query": q, "max_iterations": max_iterations}
    if workspace_id:
        body["workspace_id"] = workspace_id
    r = httpx.post(
        f"{base}/api/v1/chat/query",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=timeout,
    )
    try:
        payload = r.json()
    except json.JSONDecodeError:
        payload = {"raw": r.text}
    return {
        "status_code": r.status_code,
        "latency": r.elapsed.total_seconds(),
        "response": payload,
    }


# ---------------------------------------------------------------------------
# M3.1 — ConfidenceRouter
# ---------------------------------------------------------------------------

SIMPLE_QUERIES: list[str] = [
    "What is the title of this document?",
    "Who is the author of this document?",
    "How many pages does this document have?",
    "When was this document published?",
    "What is the main topic?",
    "这个文档讲什么?",
    "文档标题是什么?",
    "作者是谁?",
    "页数是多少?",
    "出版日期?",
]

COMPLEX_QUERIES: list[str] = [
    "对比 A 和 B 的差异",
    "分析 X 和 Y 的优缺点",
    "比较 X 和 Y 在 Z 维度上的区别",
    "总结 Z 的核心要点",
    "compare A with B",
    "summarize the key findings",
    "X 文档第 3 章讲了什么，对比 Y 文档的对应内容",
    "Find all references to X and explain their significance",
    "why does X happen in section 5?",
    "X 是如何影响 Y 的？",
]

EDGE_QUERIES: list[tuple[str, int | None]] = [
    ("", 400),  # EmptyQueryError → HTTP 400
    ("🧪✨中文", None),
    ("x" * 2500, None),  # > MAX_CLASSIFY_QUERY_LEN=2000
]


def _summarise(r: dict[str, Any]) -> str:
    """Render one result as a short status string."""
    if r["status_code"] != 200:
        return f"HTTP {r['status_code']} ({r['latency']:.1f}s)"
    body = r["response"]
    if isinstance(body, dict):
        route = body.get("route", "?")
        iters = body.get("iterations", "?")
        answer_preview = (body.get("answer") or "")[:30]
        return f"route={route} iter={iters} ({r['latency']:.1f}s) ans='{answer_preview}...'"
    return f"non-JSON ({r['latency']:.1f}s)"


def test_m3_1_router(token: str) -> dict[str, Any]:
    """23 query (10 simple + 10 complex + 3 edge) → route distribution check."""
    print("\n===== M3.1 router live E2E =====")
    summary: dict[str, Any] = {"simple": [], "complex": [], "edge": []}

    for q in SIMPLE_QUERIES:
        try:
            r = post_query(token, q, max_iterations=1)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        summary["simple"].append({"query": q, **r})
        print(f"[simple]  {q[:40]:40} → {_summarise(r)}")

    for q in COMPLEX_QUERIES:
        try:
            r = post_query(token, q, max_iterations=3, timeout=360)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        summary["complex"].append({"query": q, **r})
        print(f"[complex] {q[:40]:40} → {_summarise(r)}")

    for q, expected in EDGE_QUERIES:
        try:
            r = post_query(token, q, max_iterations=1, timeout=120)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        summary["edge"].append({"query": q[:40], "expected_status": expected, **r})
        print(f"[edge]    {q[:40]:40} → {_summarise(r)}")

    # Summarise pass/fail per category
    simple_pass = sum(
        1
        for x in summary["simple"]
        if x["status_code"] == 200
        and isinstance(x["response"], dict)
        and x["response"].get("route") == "direct"
    )
    complex_pass = sum(
        1
        for x in summary["complex"]
        if x["status_code"] == 200
        and isinstance(x["response"], dict)
        and x["response"].get("route") == "agent"
    )
    edge_pass = sum(
        1
        for x in summary["edge"]
        if x["status_code"] == (x.get("expected_status") or 200)
    )
    print(
        f"\n[M3.1 summary] simple route=direct: {simple_pass}/{len(SIMPLE_QUERIES)}"
        f" | complex route=agent: {complex_pass}/{len(COMPLEX_QUERIES)}"
        f" | edge: {edge_pass}/{len(EDGE_QUERIES)}"
    )

    # Persist results for later aggregation by the Assess stage
    try:
        RESULTS_PATH.write_text(
            json.dumps({"m3_1": summary}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[persisted] {RESULTS_PATH}")
    except OSError as exc:
        print(f"[persisted FAIL] {exc}")

    return summary


# ---------------------------------------------------------------------------
# M3.2 — AgentRunner multi-hop (plan/retrieve/reflect/rewrite/synthesize)
# ---------------------------------------------------------------------------

MULTIHOP_QUERIES: list[str] = [
    # multihop_2: plan≥2 sub_queries, retrieve≥2, reflect≥1, synth=1
    "对比第 1 章和第 3 章关于 X 的论述",
    # multihop_3: plan≥3 sub_queries, retrieve≥3, reflect≥1, rewrite≥1
    "X 文档和 Y 文档对 Z 的看法有什么异同",
    # reflect_continue: 触发 reflect=continue ≥1
    "深度分析 X 的 3 个维度",
    # rewrite_path: 触发 rewrite ≥1
    "X 是怎么影响 Y 的？再解释为什么",
    # synth_size: synthesize 输出 ≥100 chars
    "X 的详细总结",
]

MULTIHOP_EDGE_QUERIES: list[str] = [
    # edge_max_iter: 超复杂 query → iterations=5, truncated_by_max_iter=True
    "深度综合分析 X 和 Y 的关系，跨多章节比较 A、B、C 三个概念的异同，列举 5 个例子并解释为什么",
    # edge_simple_agent: LLM 判定走 agent，但内容简单
    "什么是 X 的核心定义",
    # edge_history: 同 conversation_id 连发 2 次 multi-hop
    # (handled separately below via conversation_id chaining)
]


def test_m3_2_agent_core(token: str) -> dict[str, Any]:
    """5 multi-hop query + 2 edge query (history-chain runs in-band) → agent path."""
    print("\n===== M3.2 agent_core live E2E =====")
    summary: dict[str, Any] = {"multihop": [], "edge": []}

    # ---- 5 multi-hop queries (each its own conversation) ----
    for q in MULTIHOP_QUERIES:
        try:
            r = post_query(token, q, max_iterations=5, timeout=360)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        summary["multihop"].append({"query": q, **r})
        print(f"[multihop] {q[:40]:40} → {_summarise(r)}")

    # ---- edge: max_iter 截断 ----
    for q in MULTIHOP_EDGE_QUERIES[:1]:
        try:
            r = post_query(token, q, max_iterations=5, timeout=360)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        summary["edge"].append({"query": q, "kind": "max_iter", **r})
        print(f"[edge max] {q[:40]:40} → {_summarise(r)}")

    # ---- edge: simple-agent ----
    simple_agent_q = MULTIHOP_EDGE_QUERIES[1]
    try:
        r = post_query(token, simple_agent_q, max_iterations=5, timeout=360)
    except httpx.HTTPError as exc:
        r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
    summary["edge"].append({"query": simple_agent_q, "kind": "simple_agent", **r})
    print(f"[edge s-a] {simple_agent_q[:40]:40} → {_summarise(r)}")

    # ---- edge: history-chain (turn 1 + turn 2 same conversation_id) ----
    cid: str | None = None
    try:
        # turn 1: explicit conversation_id=None to let server create one
        body: dict[str, Any] = {
            "query": "X 文档的核心论点是什么？",
            "max_iterations": 3,
        }
        r1 = httpx.post(
            f"{BASE}/api/v1/chat/query",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=360,
        )
        r1.raise_for_status()
        payload1 = r1.json()
        cid = payload1.get("conversation_id")
        # turn 2: reuse conversation_id
        body2: dict[str, Any] = {
            "query": "再解释一下 Y 部分",
            "max_iterations": 3,
            "conversation_id": cid,
        }
        r2 = httpx.post(
            f"{BASE}/api/v1/chat/query",
            headers={"Authorization": f"Bearer {token}"},
            json=body2,
            timeout=360,
        )
        r2.raise_for_status()
        payload2 = r2.json()
        summary["edge"].append(
            {
                "query": "history turn 2",
                "kind": "history",
                "turn1": {"status_code": r1.status_code, "latency": r1.elapsed.total_seconds(), "response": payload1},
                "turn2": {"status_code": r2.status_code, "latency": r2.elapsed.total_seconds(), "response": payload2},
            }
        )
        print(
            f"[edge hist] turn1 route={payload1.get('route')} iter={payload1.get('iterations')}"
            f" → turn2 route={payload2.get('route')} iter={payload2.get('iterations')}"
        )
    except httpx.HTTPError as exc:
        summary["edge"].append(
            {"query": "history chain", "kind": "history", "error": str(exc)}
        )
        print(f"[edge hist] ERROR {exc}")

    # ---- Aggregate ----
    multihop_pass = sum(
        1
        for x in summary["multihop"]
        if x["status_code"] == 200
        and isinstance(x["response"], dict)
        and x["response"].get("route") == "agent"
        and x["response"].get("iterations", 0) >= 2
    )
    edge_pass = sum(
        1
        for x in summary["edge"]
        if x.get("kind") == "max_iter"
        and x["status_code"] == 200
        and isinstance(x["response"], dict)
        and x["response"].get("iterations", 0) >= 1
    ) + sum(
        1
        for x in summary["edge"]
        if x.get("kind") == "simple_agent"
        and x["status_code"] == 200
    ) + sum(
        1
        for x in summary["edge"]
        if x.get("kind") == "history"
        and x.get("turn2", {}).get("status_code") == 200
    )
    print(
        f"\n[M3.2 summary] multihop route=agent iter>=2: {multihop_pass}/{len(MULTIHOP_QUERIES)}"
        f" | edge cases passed: {edge_pass}/3"
    )

    try:
        RESULTS_PATH.write_text(
            json.dumps(
                {"m3_1": _load_previous_m3_1(), "m3_2": summary},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[persisted FAIL] {exc}")

    return summary


def _load_previous_m3_1() -> dict[str, Any]:
    """Best-effort merge of an earlier m3_1 run so the persisted file stays complete."""
    try:
        existing = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and "m3_1" in existing:
            return existing["m3_1"]
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# M3.3 — LongContextFallback
# ---------------------------------------------------------------------------

LONG_CONTEXT_QUERIES: list[str] = [
    # low_score_1: corpus 不存在 + 高 mismatch
    "量子纠缠在古希腊哲学中的应用",
    # low_score_2: 拼写错误 + nonsense
    "asdfghjkl qwertyuiop nonsense query",
    # sparse_1: 特殊术语不存在
    "请找 XYZ123ABC 这个特殊术语",
    # empty_corpus_like: 完全不相关领域
    "罗马帝国衰落的财政因素与宋朝比较",
]


def test_m3_3_long_context(token: str) -> dict[str, Any]:
    """4 low-score query → fallback_triggered + route=long_context (or graceful 200/404)."""
    print("\n===== M3.3 long_context live E2E =====")
    summary: dict[str, Any] = {"long_context": []}

    for q in LONG_CONTEXT_QUERIES:
        try:
            r = post_query(token, q, max_iterations=5, timeout=360)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        summary["long_context"].append({"query": q, **r})
        print(f"[longctx] {q[:40]:40} → {_summarise(r)}")

    # ---- Aggregate ----
    triggered = sum(
        1
        for x in summary["long_context"]
        if x["status_code"] == 200
        and isinstance(x["response"], dict)
        and x["response"].get("fallback_triggered") is True
    )
    long_ctx_route = sum(
        1
        for x in summary["long_context"]
        if x["status_code"] == 200
        and isinstance(x["response"], dict)
        and x["response"].get("route") == "long_context"
    )
    print(
        f"\n[M3.3 summary] fallback_triggered=True: {triggered}/{len(LONG_CONTEXT_QUERIES)}"
        f" | route=long_context: {long_ctx_route}/{len(LONG_CONTEXT_QUERIES)}"
    )

    try:
        existing = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing["m3_3"] = summary
    try:
        RESULTS_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[persisted FAIL] {exc}")

    return summary


# ---------------------------------------------------------------------------
# M3.4 — PostProcessor (SensitiveWordFilter + PII Masker)
# ---------------------------------------------------------------------------

PII_QUERIES: list[tuple[str, str, list[str]]] = [
    # (query, kind, patterns that MUST NOT appear in the answer)
    (
        "我的手机号是 13812345678，请帮我查文档",
        "phone",
        ["13812345678"],
    ),
    (
        "联系方式：13987654321",
        "phone",
        ["13987654321"],
    ),
    (
        "身份证 110101199001011234",
        "idcard",
        ["110101199001011234"],
    ),
    (
        "邮箱 test@example.com",
        "email",
        ["test@example.com"],
    ),
]


SENSITIVE_QUERY = "敏感词：傻逼（用于触发 SensitiveWordFilter）"

COMBINATION_QUERY = (
    "我的手机 13812345678，邮箱 a@b.com，敏感词傻逼"
)


def test_m3_4_post_processor(token: str) -> dict[str, Any]:
    """4 PII + 1 sensitive + 1 combination query → mask applied, words redacted."""
    print("\n===== M3.4 post_processor live E2E =====")
    summary: dict[str, Any] = {"pii": [], "sensitive": [], "combination": []}

    # ---- PII masking ----
    for q, kind, forbidden in PII_QUERIES:
        try:
            r = post_query(token, q, max_iterations=1, timeout=60)
        except httpx.HTTPError as exc:
            r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
        ans = ""
        if r["status_code"] == 200 and isinstance(r["response"], dict):
            ans = r["response"].get("answer", "") or ""
        leaked = [tok for tok in forbidden if tok in ans]
        summary["pii"].append(
            {
                "query": q,
                "kind": kind,
                "leaked": leaked,
                **r,
            }
        )
        verdict = "✅ masked" if not leaked else f"❌ leaked {leaked}"
        print(f"[pii/{kind:6}] {q[:40]:40} → {_summarise(r)} | {verdict}")

    # ---- Sensitive word ----
    try:
        r = post_query(token, SENSITIVE_QUERY, max_iterations=1, timeout=60)
    except httpx.HTTPError as exc:
        r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
    ans = ""
    if r["status_code"] == 200 and isinstance(r["response"], dict):
        ans = r["response"].get("answer", "") or ""
    leaked_word = "傻逼" in ans
    summary["sensitive"].append(
        {
            "query": SENSITIVE_QUERY,
            "leaked_word": leaked_word,
            **r,
        }
    )
    verdict = "✅ redacted" if not leaked_word else "❌ leaked"
    print(f"[sensitive]   {SENSITIVE_QUERY[:40]:40} → {_summarise(r)} | {verdict}")

    # ---- Combination ----
    try:
        r = post_query(token, COMBINATION_QUERY, max_iterations=1, timeout=60)
    except httpx.HTTPError as exc:
        r = {"status_code": 0, "latency": 0.0, "response": {"error": str(exc)}}
    ans = ""
    if r["status_code"] == 200 and isinstance(r["response"], dict):
        ans = r["response"].get("answer", "") or ""
    leaked_combo = []
    if "13812345678" in ans:
        leaked_combo.append("phone")
    if "a@b.com" in ans:
        leaked_combo.append("email")
    if "傻逼" in ans:
        leaked_combo.append("sensitive")
    summary["combination"].append(
        {
            "query": COMBINATION_QUERY,
            "leaked": leaked_combo,
            **r,
        }
    )
    verdict = "✅ all masked" if not leaked_combo else f"❌ leaked {leaked_combo}"
    print(f"[combo]       {COMBINATION_QUERY[:40]:40} → {_summarise(r)} | {verdict}")

    # ---- Aggregate ----
    pii_pass = sum(1 for x in summary["pii"] if not x["leaked"])
    sens_pass = 1 if not summary["sensitive"][0]["leaked_word"] else 0
    combo_pass = 1 if not summary["combination"][0]["leaked"] else 0
    print(
        f"\n[M3.4 summary] pii masked: {pii_pass}/{len(PII_QUERIES)}"
        f" | sensitive redacted: {sens_pass}/1"
        f" | combination all-masked: {combo_pass}/1"
    )

    try:
        existing = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing["m3_4"] = summary
    try:
        RESULTS_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[persisted FAIL] {exc}")

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 Live E2E driver")
    parser.add_argument(
        "scope",
        nargs="?",
        default="m3_1",
        choices=["m3_1", "m3_2", "m3_3", "m3_4", "all"],
        help="which sub-track to run (default: m3_1)",
    )
    args = parser.parse_args()

    t0 = time.perf_counter()
    token = login()
    print(f"Login OK (token={token[:16]}...) — scope={args.scope}")

    if args.scope in ("m3_1", "all"):
        test_m3_1_router(token)
    if args.scope in ("m3_2", "all"):
        test_m3_2_agent_core(token)
    if args.scope in ("m3_3", "all"):
        test_m3_3_long_context(token)
    if args.scope in ("m3_4", "all"):
        test_m3_4_post_processor(token)

    elapsed = time.perf_counter() - t0
    print(f"\nTotal elapsed: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())