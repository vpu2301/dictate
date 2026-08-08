"""Planning + intent extraction — QS1's decision point and its sanitizer."""

from __future__ import annotations

from uuid import UUID

from conftest import FakeGateway
from evidence_answer.domain.stages.intent import (
    extract_intent,
    is_fallback,
    keyword_fallback,
)
from evidence_answer.domain.stages.plan import build_query, plan_quick_search

from evidence_models import ClinicalConcept, ClinicalIntent, QuestionType

TENANT = UUID("11111111-1111-1111-1111-111111111111")
PROMPT = "extract: {question}"

INTENT = ClinicalIntent(
    question_type=QuestionType.therapy,
    concepts=[ClinicalConcept(text="community acquired pneumonia")],
    population="adults",
)


def plan(**overrides: object):  # noqa: ANN201 — test helper
    kwargs = {
        "intent": INTENT,
        "question": "Empiric therapy for CAP?",
        "tenant_id": TENANT,
        "locale": "en",
        "k": 8,
        "web_k": 6,
        "web_enabled": True,
        "intent_is_fallback": False,
    }
    kwargs.update(overrides)
    return plan_quick_search(**kwargs)  # type: ignore[arg-type]


# ── planning ────────────────────────────────────────────────────────────


def test_corpus_and_web_are_planned_separately() -> None:
    """Two retrievals, not one: the web is off the critical path so a corpus
    answer can stream while a third party is still thinking."""
    result = plan()

    assert result.corpus.sources == ("local_corpus", "tenant_corpus")
    assert result.web is not None
    assert result.web.sources == ("web",)
    assert result.expects_web


def test_only_the_web_plan_carries_the_intent() -> None:
    result = plan()

    assert result.corpus.intent is None
    assert result.web is not None and result.web.intent is INTENT


def test_fallback_intent_never_reaches_the_web() -> None:
    """QS1: keyword-split question text is not a de-identified concept list,
    however harmless the words look."""
    result = plan(intent_is_fallback=True)

    assert result.web is None
    assert result.notes == ["web_skipped_fallback_intent"]


def test_conceptless_intent_never_reaches_the_web() -> None:
    empty = ClinicalIntent(question_type=QuestionType.other, concepts=[])
    result = plan(intent=empty)

    assert result.web is None
    assert result.notes == ["web_skipped_no_concepts"]


def test_web_can_be_disabled_by_flag() -> None:
    result = plan(web_enabled=False)

    assert result.web is None
    assert result.notes == ["web_disabled"]
    # The corpus plan is untouched: the answer still happens.
    assert result.corpus.sources == ("local_corpus", "tenant_corpus")


def test_corpus_query_combines_concepts_and_the_question() -> None:
    """No privacy boundary inside our own corpus, and dense retrieval handles
    full questions well."""
    query = build_query(INTENT, "Empiric therapy for CAP?")

    assert "community acquired pneumonia" in query
    assert "adults" in query
    assert "Empiric therapy for CAP?" in query


def test_corpus_query_falls_back_to_the_question_when_intent_is_empty() -> None:
    empty = ClinicalIntent(question_type=QuestionType.other, concepts=[])
    assert build_query(empty, "What now?") == "What now?"


# ── intent extraction + sanitizing ──────────────────────────────────────


async def test_extraction_parses_the_json_and_keeps_clinical_concepts(
    intent_json: str,
) -> None:
    gateway = FakeGateway(generate_by_role={"generator.fast": intent_json})

    intent, note = await extract_intent(
        "Empiric therapy for CAP?",
        gateway=gateway,
        prompt=PROMPT,  # type: ignore[arg-type]
    )

    assert note == "ok"
    assert [c.text for c in intent.concepts] == [
        "community acquired pneumonia",
        "amoxicillin",
    ]
    assert intent.population == "adults"


async def test_identifier_shaped_concepts_are_dropped_before_the_intent_exists() -> None:
    """The model was told not to emit these. The code assumes it eventually
    will (rule: defense in depth around QS1)."""
    payload = (
        '{"question_type": "therapy", "concepts": '
        '["MRN-99881", "19850613", "13.06.1985", "+380671234567", '
        '"patient@example.org", "pneumonia"], '
        '"population": null, "negations": []}'
    )
    gateway = FakeGateway(generate_by_role={"generator.fast": payload})

    intent, _ = await extract_intent(
        "…",
        gateway=gateway,
        prompt=PROMPT,  # type: ignore[arg-type]
    )

    assert [c.text for c in intent.concepts] == ["pneumonia"]


async def test_clinical_terms_with_digits_survive_the_sanitizer() -> None:
    """The sanitizer must not eat the vocabulary: `type 2 diabetes`,
    `COVID-19`, `CYP2C19`, `500 mg` are all legitimate."""
    payload = (
        '{"question_type": "therapy", "concepts": '
        '["type 2 diabetes", "COVID-19", "CKD stage 4", "CYP2C19", "vitamin B12"], '
        '"population": "elderly", "negations": []}'
    )
    gateway = FakeGateway(generate_by_role={"generator.fast": payload})

    intent, _ = await extract_intent(
        "…",
        gateway=gateway,
        prompt=PROMPT,  # type: ignore[arg-type]
    )

    assert len(intent.concepts) == 5


async def test_sentence_shaped_concept_is_dropped() -> None:
    """A "concept" that is really a sentence is the model ignoring the prompt
    — and letting it through would put verbatim question text on the wire."""
    payload = (
        '{"question_type": "therapy", "concepts": '
        '["the patient is a 65 year old man who presented with fever", "pneumonia"], '
        '"population": null, "negations": []}'
    )
    gateway = FakeGateway(generate_by_role={"generator.fast": payload})

    intent, _ = await extract_intent(
        "…",
        gateway=gateway,
        prompt=PROMPT,  # type: ignore[arg-type]
    )

    assert [c.text for c in intent.concepts] == ["pneumonia"]


async def test_duplicate_concepts_are_collapsed() -> None:
    payload = (
        '{"question_type": "therapy", "concepts": ["pneumonia", "Pneumonia", "PNEUMONIA"], '
        '"population": null, "negations": []}'
    )
    gateway = FakeGateway(generate_by_role={"generator.fast": payload})

    intent, _ = await extract_intent(
        "…",
        gateway=gateway,
        prompt=PROMPT,  # type: ignore[arg-type]
    )

    assert len(intent.concepts) == 1


async def test_malformed_output_retries_once_then_falls_back(intent_json: str) -> None:
    gateway = FakeGateway(generate_by_role={"generator.fast": "not json"})

    intent, note = await extract_intent(
        "Empiric therapy for pneumonia in adults?",
        gateway=gateway,  # type: ignore[arg-type]
        prompt=PROMPT,
    )

    assert note.startswith("intent_unparsed")
    assert len(gateway.prompts_seen) == 2  # one retry, then give up
    assert is_fallback(intent, "Empiric therapy for pneumonia in adults?")


async def test_gateway_outage_produces_a_fallback_not_an_exception() -> None:
    """The pipeline always gets a plan — a dead extractor degrades retrieval
    quality, it does not take the product down."""
    gateway = FakeGateway(generate_by_role={})

    intent, note = await extract_intent(
        "Empiric therapy for pneumonia?",
        gateway=gateway,
        prompt=PROMPT,  # type: ignore[arg-type]
    )

    assert note.startswith("intent_unparsed")
    assert intent.concepts


def test_keyword_fallback_drops_stopwords_and_short_tokens() -> None:
    intent = keyword_fallback("What is the empiric therapy for pneumonia in adults?")

    words = [c.text for c in intent.concepts]
    assert "pneumonia" in words
    assert "what" not in words
    assert intent.question_type is QuestionType.other


def test_is_fallback_recognises_only_its_own_output() -> None:
    question = "Empiric therapy for pneumonia?"

    assert is_fallback(keyword_fallback(question), question)
    assert not is_fallback(INTENT, question)
