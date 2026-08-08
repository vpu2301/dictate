"""Intent extraction (`generator.fast`, JSON-constrained decode).

Output feeds two consumers with very different risk profiles: the retrieval
plan (a wrong concept costs relevance) and the web query builder (a wrong
concept could cost privacy, rule QS1). So extraction is followed by a
**sanitizer** that drops anything identifier-shaped before the intent is
allowed to exist — the model is instructed not to produce such things, and
the code assumes it eventually will anyway.

Malformed output → one retry → `intent_unparsed` in the trace + a keyword
fallback intent built from the question's own tokens. The fallback never
reaches the web: `plan.py` drops the `web` source when the intent is a
fallback, because keyword-split question text is exactly the untrusted
material QS1 exists to keep off the wire.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError

from evidence_models import ClinicalConcept, ClinicalIntent, QuestionType
from evidence_safety import identifier_shaped
from models import GatewayError, ModelGatewayClient

logger = logging.getLogger(__name__)

EXTRACTOR_ROLE = "generator.fast"
MAX_CONCEPTS = 8

_INTENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "question_type": {"type": "string", "enum": [t.value for t in QuestionType]},
        "concepts": {
            "type": "array",
            "maxItems": MAX_CONCEPTS,
            "items": {"type": "string", "maxLength": 80},
        },
        "population": {"type": ["string", "null"], "maxLength": 80},
        "negations": {"type": "array", "items": {"type": "string", "maxLength": 80}},
    },
    "required": ["question_type", "concepts", "population", "negations"],
    "additionalProperties": False,
}

# The identifier screen is the shared one from `evidence_safety` — the same
# code the web query builder runs, so the two cannot drift apart. This copy
# runs earlier, before the value is ever stored in provenance.
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_STOPWORDS = {
    "what",
    "which",
    "when",
    "does",
    "with",
    "from",
    "that",
    "this",
    "have",
    "should",
    "would",
    "there",
    "their",
    "about",
    "than",
    "then",
    "into",
    "яка",
    "який",
    "які",
    "чи",
    "для",
    "при",
    "коли",
    "після",
    "перед",
    "щодо",
    "лікування",
    "терапія",
}


class IntentUnparsedError(Exception):
    """Model output could not be turned into a ClinicalIntent."""


def _sanitize(term: str) -> str | None:
    candidate = " ".join(term.strip().split())
    if not candidate or len(candidate) > 80:
        return None
    shape = identifier_shaped(candidate)
    if shape is not None:
        logger.warning(
            "intent.identifier_shaped_term_dropped",
            extra={"shape": shape, "length": len(candidate)},  # never the value
        )
        return None
    # A "concept" that is really a sentence is the model ignoring the prompt;
    # letting it through would put verbatim question text on the wire.
    if len(candidate.split()) > 6:
        logger.info("intent.sentence_shaped_term_dropped")
        return None
    return candidate


def _to_intent(payload: dict[str, object]) -> ClinicalIntent:
    raw_concepts = payload.get("concepts") or []
    if not isinstance(raw_concepts, list):
        raise IntentUnparsedError("concepts is not a list")
    concepts: list[ClinicalConcept] = []
    seen: set[str] = set()
    for raw in raw_concepts[:MAX_CONCEPTS]:
        cleaned = _sanitize(str(raw))
        if cleaned and cleaned.casefold() not in seen:
            concepts.append(ClinicalConcept(text=cleaned))
            seen.add(cleaned.casefold())
    if not concepts:
        raise IntentUnparsedError("no usable concepts survived sanitization")

    population_raw = payload.get("population")
    population = _sanitize(str(population_raw)) if population_raw else None
    negations_raw = payload.get("negations") or []
    negations = (
        [c for c in (_sanitize(str(n)) for n in negations_raw) if c]
        if isinstance(negations_raw, list)
        else []
    )
    try:
        question_type = QuestionType(str(payload.get("question_type", "other")))
    except ValueError:
        question_type = QuestionType.other
    return ClinicalIntent(
        question_type=question_type,
        concepts=concepts,
        population=population,
        negations=negations,
    )


def keyword_fallback(question: str) -> ClinicalIntent:
    """Last resort when extraction fails twice.

    Marked by `question_type=other` with single-word concepts. `plan.py`
    refuses to send a fallback intent to the web connector.
    """
    words: list[str] = []
    for match in _WORD.finditer(question):
        word = match.group(0).casefold()
        if word in _STOPWORDS or word in words:
            continue
        words.append(word)
        if len(words) >= 6:
            break
    return ClinicalIntent(
        question_type=QuestionType.other,
        concepts=[ClinicalConcept(text=w) for w in words],
    )


async def extract_intent(
    question: str, *, gateway: ModelGatewayClient, prompt: str
) -> tuple[ClinicalIntent, str]:
    """Returns (intent, trace_note). Never raises — the pipeline always gets
    a plan, even when the extractor is down."""
    last_error = "unknown"
    for attempt in range(2):
        try:
            result = await gateway.generate(
                EXTRACTOR_ROLE,
                prompt.format(question=question),
                max_tokens=256,
                temperature=0.0,
                json_schema=_INTENT_SCHEMA,
            )
            payload = json.loads(result.text)
            if not isinstance(payload, dict):
                raise IntentUnparsedError("output is not a JSON object")
            intent = _to_intent(payload)
        except (GatewayError, json.JSONDecodeError, ValidationError, IntentUnparsedError) as exc:
            last_error = type(exc).__name__
            logger.info("intent.extract_failed", extra={"attempt": attempt, "error": last_error})
            continue
        return intent, "ok" if attempt == 0 else "retried"
    return keyword_fallback(question), f"intent_unparsed:{last_error}"


def is_fallback(intent: ClinicalIntent, question: str) -> bool:
    """True when `intent` came from `keyword_fallback` for `question`.

    Compared structurally rather than flagged on the model, because
    `ClinicalIntent` is a frozen v1 contract and adding a provenance field to
    it for this would be a contract change (RC1) in service of an internal
    detail.
    """
    return intent == keyword_fallback(question)
