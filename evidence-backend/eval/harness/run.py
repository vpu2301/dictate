#!/usr/bin/env python3
"""Retrieval eval harness (rule T4 / ADR-0019 pattern, spec D9-D10).

Modes (--mode): dense | lexical | hybrid | hybrid_rerank (default) |
rerank_pure (diagnostic: cross-encoder order without boosts).
Metrics: recall@20 (grade ≥ 2 = relevant), nDCG@10 (graded), MRR@10.
Every run persists to evidence_eval.runs; --adopt promotes it to baseline
and snapshots the seed set into evidence_eval.questions/judgments; without
--adopt the run is compared against the adopted baseline using the protected
thresholds in eval/gates.yaml (breach ⇒ exit 1).

  uv run --project services/evidence-retrieval python eval/harness/run.py \
      [--mode hybrid_rerank] [--adopt] [--ablation]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import subprocess  # nosec B404 — fixed git argv for the run's git_sha
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from db import create_pool, tenant_connection
from evidence_retrieval.adapters.opensearch import LexicalSearch
from evidence_retrieval.adapters.pg import dense_search
from evidence_retrieval.config import settings
from evidence_retrieval.domain.connectors.corpus import GLOBAL_TENANT
from evidence_retrieval.domain.fusion import rrf_fuse
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import prepare
from evidence_retrieval.domain.rerank import rerank_passages
from evidence_retrieval.domain.scoring import finalize

from evidence_models import ConnectorKind, EvidencePassage
from models import ModelGatewayClient

ROOT = Path(__file__).resolve().parent.parent.parent
RUBRIC_VERSION = "1.0"
JUDGE = "dev-agent-v1"  # clinical advisor countersign pending (sign-off row)

MODES = ["dense", "lexical", "hybrid", "hybrid_rerank", "rerank_pure"]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _git_sha() -> str:
    out = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
    )
    return out.stdout.strip() or "unknown"


async def _resolve_judgments_conn(
    conn: asyncpg.Connection, judgments: list[dict[str, Any]]
) -> dict[str, dict[UUID, int]]:
    """canonical_ref locators → chunk ids of the LATEST version (survives
    re-ingestion, spec §2). A trailing '$' anchors as ends-with."""
    resolved: dict[str, dict[UUID, int]] = {}
    for j in judgments:
        section = str(j["section_like"])
        anchored = section.endswith("$")
        pattern = section[:-1] if anchored else section
        rows = await conn.fetch(
            """
            SELECT c.id FROM chunks c
            JOIN document_versions dv ON dv.id = c.document_version_id
            JOIN documents d ON d.id = dv.document_id
            WHERE d.canonical_id = $1
              AND dv.version = (SELECT max(v.version) FROM document_versions v
                                WHERE v.document_id = d.id)
              AND ($3 AND c.section_path LIKE '%' || $2
                   OR NOT $3 AND c.section_path ILIKE '%' || $2 || '%')
            """,
            j["canonical_id"],
            pattern,
            anchored,
        )
        grades = resolved.setdefault(str(j["qid"]), {})
        for row in rows:
            grades[row["id"]] = max(grades.get(row["id"], 0), int(j["grade"]))
    return resolved


async def _resolve_judgments(
    pool: asyncpg.Pool, judgments: list[dict[str, Any]]
) -> dict[str, dict[UUID, int]]:
    async with tenant_connection(pool, GLOBAL_TENANT) as conn:
        return await _resolve_judgments_conn(conn, judgments)


def _ndcg_at(ranked: list[UUID], grades: dict[UUID, int], k: int) -> float:
    dcg = sum((2 ** grades.get(cid, 0) - 1) / math.log2(i + 2) for i, cid in enumerate(ranked[:k]))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def _recall_at(ranked: list[UUID], grades: dict[UUID, int], k: int) -> float:
    relevant = {cid for cid, g in grades.items() if g >= 2}
    if not relevant:
        return 1.0
    return len(relevant & set(ranked[:k])) / len(relevant)


def _mrr_at(ranked: list[UUID], grades: dict[UUID, int], k: int) -> float:
    for i, cid in enumerate(ranked[:k]):
        if grades.get(cid, 0) >= 2:
            return 1.0 / (i + 1)
    return 0.0


async def _rank_for(
    mode: str,
    query: str,
    *,
    pool: asyncpg.Pool,
    lexical: LexicalSearch,
    gateway: ModelGatewayClient,
    k: int = 20,
    use_lexicon: bool = True,
) -> list[UUID]:
    prepared = prepare(query)
    filters = Filters()
    dense: list[EvidencePassage] = []
    lex: list[EvidencePassage] = []
    if mode != "lexical":
        vector = (await gateway.embed("embed.dense", [prepared.normalized]))[0]
        dense = await dense_search(
            pool,
            tenant_id=GLOBAL_TENANT,
            partition_tenant=GLOBAL_TENANT,
            query_vector=vector,
            k=k,
            filters=filters,
            snapshot_members=None,
            kind=ConnectorKind.local_corpus,
            connector_id="eval",
        )
    if mode != "dense":
        lex = await lexical.search(
            partition_tenant=GLOBAL_TENANT,
            query_text=prepared.lexical_query if use_lexicon else prepared.normalized,
            k=k,
            filters=filters,
            snapshot_members=None,
            kind=ConnectorKind.local_corpus,
            connector_id="eval",
        )
    if mode == "dense":
        passages = dense
    elif mode == "lexical":
        passages = lex
    else:
        passages = rrf_fuse(dense, lex)
        if mode in ("hybrid_rerank", "rerank_pure"):
            passages, degraded = await rerank_passages(
                gateway,
                query=prepared.normalized,
                passages=passages[:32],
                budget_ms=60_000,
                batch_size=32,
            )
            if degraded:
                raise RuntimeError("rerank degraded during eval — refusing to record")
            if mode == "rerank_pure":
                passages = sorted(
                    passages,
                    key=lambda p: (
                        -(p.scores.rerank if p.scores and p.scores.rerank is not None else -99),
                        str(p.chunk_id),
                    ),
                )
            else:
                passages = finalize(passages, today=datetime.now(tz=UTC).date())
    return [p.chunk_id for p in passages if p.chunk_id is not None]


async def evaluate(
    mode: str, *, use_lexicon: bool = True, langs: set[str] | None = None
) -> dict[str, float]:
    questions = _load_jsonl(ROOT / "eval" / "seed" / "questions.jsonl")
    judgments = _load_jsonl(ROOT / "eval" / "seed" / "judgments.jsonl")
    pool = await create_pool(settings.db_app_role_dsn, application_name="evidence-eval", max_size=4)
    lexical = LexicalSearch(url=settings.opensearch_url, index=settings.opensearch_index)
    gateway = ModelGatewayClient(settings.gateway_base_url)
    try:
        grades_by_q = await _resolve_judgments(pool, judgments)
        recalls, ndcgs, mrrs = [], [], []
        if langs is not None:
            questions = [q for q in questions if q["lang"] in langs]
        for q in questions:
            qid = str(q["qid"])
            grades = grades_by_q.get(qid, {})
            if not grades:
                raise RuntimeError(f"{qid}: no judgments resolved — locator drift?")
            ranked = await _rank_for(
                mode,
                str(q["text"]),
                pool=pool,
                lexical=lexical,
                gateway=gateway,
                use_lexicon=use_lexicon,
            )
            recalls.append(_recall_at(ranked, grades, 20))
            ndcgs.append(_ndcg_at(ranked, grades, 10))
            mrrs.append(_mrr_at(ranked, grades, 10))
        n = len(questions)
        return {
            "recall_at_20": round(sum(recalls) / n, 4),
            "ndcg_at_10": round(sum(ndcgs) / n, 4),
            "mrr_at_10": round(sum(mrrs) / n, 4),
            "questions": n,
        }
    finally:
        await gateway.aclose()
        await lexical.aclose()
        await pool.close()


async def _snapshot_seed_set(conn: asyncpg.Connection) -> None:
    questions = _load_jsonl(ROOT / "eval" / "seed" / "questions.jsonl")
    judgments = _load_jsonl(ROOT / "eval" / "seed" / "judgments.jsonl")
    qid_map: dict[str, Any] = {}
    for q in questions:
        qid_map[str(q["qid"])] = await conn.fetchval(
            """
            INSERT INTO evidence_eval.questions (text, lang, specialty, created_by)
            SELECT $1, $2, $3, $4
            WHERE NOT EXISTS (SELECT 1 FROM evidence_eval.questions WHERE text = $1)
            RETURNING id
            """,
            q["text"],
            q["lang"],
            q.get("specialty"),
            JUDGE,
        ) or await conn.fetchval(
            "SELECT id FROM evidence_eval.questions WHERE text = $1", q["text"]
        )
    resolved = await _resolve_judgments_conn(conn, judgments)
    for j in judgments:
        ref = f"{j['canonical_id']}|{j['section_like']}"
        for chunk_id in resolved.get(str(j["qid"]), {}):
            await conn.execute(
                """
                INSERT INTO evidence_eval.judgments
                    (question_id, canonical_ref, chunk_id, grade, judge, rubric_version)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (question_id, canonical_ref, judge, rubric_version) DO NOTHING
                """,
                qid_map[str(j["qid"])],
                ref,
                chunk_id,
                int(j["grade"]),
                JUDGE,
                RUBRIC_VERSION,
            )


async def persist_run(mode: str, metrics: dict[str, float], *, adopt: bool) -> int:
    import yaml

    gates = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text())["retrieval"]
    pool = await create_pool(settings.db_app_role_dsn, application_name="evidence-eval", max_size=2)
    try:
        async with tenant_connection(pool, GLOBAL_TENANT) as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO evidence_eval.runs (kind, finished_at, config, metrics, git_sha, model_pins)
                VALUES ('retrieval', now(), $1, $2, $3, $4) RETURNING id
                """,
                json.dumps({"mode": mode, "rubric_version": RUBRIC_VERSION, "judge": JUDGE}),
                json.dumps(metrics),
                _git_sha(),
                json.dumps({"embed": "BAAI/bge-m3", "rerank": "BAAI/bge-reranker-v2-m3"}),
            )
            if adopt:
                await conn.execute(
                    """
                    INSERT INTO evidence_eval.baseline (kind, run_id, adopted_by)
                    VALUES ('retrieval', $1, $2)
                    ON CONFLICT (kind) DO UPDATE
                        SET run_id = EXCLUDED.run_id, adopted_at = now(),
                            adopted_by = EXCLUDED.adopted_by
                    """,
                    run_id,
                    JUDGE,
                )
                await _snapshot_seed_set(conn)
                print(f"baseline adopted: run {run_id}")
                return 0
            baseline = await conn.fetchrow(
                """
                SELECT r.metrics FROM evidence_eval.baseline b
                JOIN evidence_eval.runs r ON r.id = b.run_id WHERE b.kind = 'retrieval'
                """
            )
        if baseline is None:
            print("no adopted baseline yet — run with --adopt to set one")
            return 0
        base = json.loads(baseline["metrics"])
        failures = []
        if metrics["recall_at_20"] < base["recall_at_20"] - gates["recall_at_20_max_drop"]:
            failures.append(
                f"recall@20 {metrics['recall_at_20']} < baseline {base['recall_at_20']} - gate"
            )
        if metrics["ndcg_at_10"] < base["ndcg_at_10"] - gates["ndcg_at_10_max_drop"]:
            failures.append(
                f"nDCG@10 {metrics['ndcg_at_10']} < baseline {base['ndcg_at_10']} - gate"
            )
        if failures:
            print("EVAL GATE FAILED:")
            for failure in failures:
                print(f"  {failure}")
            return 1
        print("eval gate: green vs baseline")
        return 0
    finally:
        await pool.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="hybrid_rerank", choices=MODES)
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="run all four production modes, print the table, persist nothing",
    )
    parser.add_argument(
        "--lexicon-effect",
        action="store_true",
        help="informational (spec §9.7): lexical metrics with vs without expansion",
    )
    parser.add_argument("--by-lang", action="store_true", help="ablation split uk vs en")
    args = parser.parse_args()

    if args.lexicon_effect:
        with_lex = await evaluate("lexical", use_lexicon=True)
        without = await evaluate("lexical", use_lexicon=False)
        print(f"lexical WITH lexicon:    {with_lex}")
        print(f"lexical WITHOUT lexicon: {without}")
        print(
            f"recall@20 delta: {round(with_lex['recall_at_20'] - without['recall_at_20'], 4)}"
            f"  nDCG@10 delta: {round(with_lex['ndcg_at_10'] - without['ndcg_at_10'], 4)}"
        )
        return 0

    if args.by_lang:
        print(f"{'mode':<15} {'lang':<5} {'recall@20':>10} {'nDCG@10':>10} {'MRR@10':>10}")
        for mode in ["dense", "lexical", "hybrid", "hybrid_rerank"]:
            for lang in ["uk", "en"]:
                m = await evaluate(mode, langs={lang})
                print(
                    f"{mode:<15} {lang:<5} {m['recall_at_20']:>10} {m['ndcg_at_10']:>10} "
                    f"{m['mrr_at_10']:>10}"
                )
        return 0

    if args.ablation:
        print(f"{'mode':<15} {'recall@20':>10} {'nDCG@10':>10} {'MRR@10':>10}")
        for mode in ["dense", "lexical", "hybrid", "hybrid_rerank"]:
            metrics = await evaluate(mode)
            print(
                f"{mode:<15} {metrics['recall_at_20']:>10} {metrics['ndcg_at_10']:>10} "
                f"{metrics['mrr_at_10']:>10}"
            )
        return 0

    metrics = await evaluate(args.mode)
    print(f"mode={args.mode} metrics={metrics}")
    return await persist_run(args.mode, metrics, adopt=args.adopt)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
