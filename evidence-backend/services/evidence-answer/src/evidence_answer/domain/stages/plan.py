"""Retrieval planning: ClinicalIntent → retrieval requests.

Quick Search issues **two** retrievals, not one:

* the **corpus plan** (`local_corpus` + `tenant_corpus`) — fast, on the
  critical path; synthesis starts as soon as it returns;
* the **web plan** (`web`) — slow (a real network round trip to a third
  party), run concurrently and merged late (spec D7). Its passages arrive as
  `late_source` events after the summary has already streamed.

Splitting them is what makes the latency target reachable: the clinician sees
a cited corpus answer while the web is still being fetched.

QS1: the web plan carries the extracted `ClinicalIntent` and is **omitted
entirely** when the intent is a keyword fallback (question tokens are not
de-identified concepts, however harmless they look).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from evidence_models import ClinicalIntent


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    query: str
    sources: tuple[str, ...]
    k: int
    tenant_id: UUID
    # Only ever set on the web plan (QS1: nothing else may reach the web).
    intent: ClinicalIntent | None = None
    locale: str | None = None
    snapshot_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class QuickSearchPlan:
    corpus: RetrievalPlan
    web: RetrievalPlan | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def expects_web(self) -> bool:
        return self.web is not None


def build_query(intent: ClinicalIntent, question: str) -> str:
    """Retrieval query text.

    Concepts first (they carry the clinical signal), question text appended
    because the dense retriever handles full questions well and the corpus is
    ours — unlike the web path, there is no privacy boundary here.
    """
    terms = [c.text for c in intent.concepts]
    if intent.population:
        terms.append(intent.population)
    concept_query = " ".join(terms).strip()
    if not concept_query:
        return question.strip()
    return f"{concept_query} {question}".strip()


def plan_quick_search(
    *,
    intent: ClinicalIntent,
    question: str,
    tenant_id: UUID,
    locale: str,
    k: int,
    web_k: int,
    web_enabled: bool,
    intent_is_fallback: bool,
    snapshot_id: UUID | None = None,
) -> QuickSearchPlan:
    notes: list[str] = []
    corpus = RetrievalPlan(
        query=build_query(intent, question),
        sources=("local_corpus", "tenant_corpus"),
        k=k,
        tenant_id=tenant_id,
        locale=locale,
        snapshot_id=snapshot_id,
    )
    web: RetrievalPlan | None = None
    if not web_enabled:
        notes.append("web_disabled")
    elif intent_is_fallback:
        # QS1: a fallback intent is question tokens wearing a concept's
        # clothes. It stays inside the cluster.
        notes.append("web_skipped_fallback_intent")
    elif not intent.concepts:
        notes.append("web_skipped_no_concepts")
    else:
        web = RetrievalPlan(
            query=build_query(intent, question),
            sources=("web",),
            k=web_k,
            tenant_id=tenant_id,
            intent=intent,
            locale=locale,
        )
    return QuickSearchPlan(corpus=corpus, web=web, notes=notes)
