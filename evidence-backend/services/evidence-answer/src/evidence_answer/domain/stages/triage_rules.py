"""Deterministic triage rule list, uk + en (rule CS1: deterministic before
generative).

Two things this list must get right, and they pull against each other:

* An emergency described as **happening now** must deflect, every time. The
  gated eval subset (AC-S04-B-3) is 100% or the sprint does not ship.
* A clinician asking **about** an emergency must NOT be deflected. "What is
  the protocol for anaphylaxis?" is the single most reasonable question this
  product will ever be asked, and a keyword matcher that blocks it is worse
  than no matcher.

So every emergency rule is a conjunction: an emergency concept AND a
present-tense/first-person immediacy marker — or a phrase that can only be
read as immediate ("patient is coding", "пацієнт непритомний"). Reference
phrasings ("protocol for", "guidelines on", "які рекомендації") suppress the
immediacy signal outright.

Rules are ordered; the first match wins and its id lands in the trace.

Every pattern here was shaped by `eval/triage/run.py` failing on it. Three
lessons are baked in and should not be undone:

* Reference terms need word boundaries — `дозуванн` was matching inside
  `передозування`, so "opioid overdose, patient unresponsive" was read as a
  dosing lookup and allowed through.
* A *strong* immediacy marker ("негайно", "urgent", "help") outranks
  reference phrasing: "профузна кровотеча, терміново потрібна тактика" is a
  person bleeding, not someone reading about bleeding.
* Asking for the next action ("що робити", "what are the next steps") is
  itself an immediacy signal — "Code blue, no pulse, what are the next steps"
  carried no other one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evidence_models import TriageReason


@dataclass(frozen=True, slots=True)
class TriageRule:
    id: str
    reason: TriageReason
    # All patterns must match for the rule to fire (conjunction).
    patterns: tuple[re.Pattern[str], ...]


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


# ── suppressors ─────────────────────────────────────────────────────────
# Phrasings that mark a question as a reference lookup rather than a call for
# help. Checked BEFORE the emergency rules; they never suppress self-harm.
_REFERENCE_PHRASING = (
    _p(
        r"\b(protocol|guideline|guidance|recommendation|algorithm|management|"
        r"treatment\s+of|approach\s+to|criteria|dose|dosing|indications?|"
        r"differential|workup|work-?up|evidence|literature|review|definition)\b"
    ),
    # NOT a bare "what is|what are": "what are the next steps" is a person in
    # a room asking for the next action, and treating it as a reference
    # question let "Code blue, no pulse, what are the next steps" through.
    # Genuine reference questions are already caught by the term list above.
    _p(
        r"\b(how\s+(?:is|are|do|does|should)\s+\w+\s+(?:managed|treated|diagnosed)|"
        r"which\s+(?:drug|agent|therapy)|when\s+to\s+(?:use|give|start))\b"
    ),
    # \b matters: without it `дозуванн` matches inside `передозування` and
    # turns an overdose report into a dosing question.
    _p(
        r"\b(протокол|настанов|рекомендац|алгоритм|тактик|лікуванн[яю]|діагностик|"
        r"показанн|дозуванн|критері|огляд\s+літератури|визначенн|згідно)"
    ),
    _p(
        r"\b(як\s+(?:лікувати|діагностувати|вести)|чим\s+лікувати|яка\s+тактика|"
        r"які\s+(?:рекомендації|показання|критерії)|що\s+таке)"
    ),
)

# Immediacy so unambiguous that reference phrasing cannot explain it away.
# "терміново потрібна тактика" is someone bleeding who wants the tactic NOW.
# NOT "urgent": in English it is a routine clinical adjective ("indications
# for urgent coronary angiography"), and as a strong marker it overrode the
# reference suppression and refused a textbook reference question. It stays in
# the ordinary immediacy list, where it needs an emergency term beside it.
_STRONG_IMMEDIACY = (
    _p(r"\b(right\s+now|help|stat)\b"),
    _p(r"\b(негайно|терміново|допоможіть|допоможи|швидка|невідкладн)"),
)

# ── immediacy markers ───────────────────────────────────────────────────
# Asking for the next action counts: a telegraphic report followed by "what
# now" is a live situation, and it may carry no other marker at all.
_IMMEDIACY_EN = _p(
    r"\b(right\s+now|now|currently|in\s+front\s+of\s+me|in\s+the\s+room|"
    r"i\s+have\s+a\s+patient|my\s+patient\s+is|patient\s+is|he\s+is|she\s+is|"
    r"they\s+are|we\s+are|is\s+not\s+breathing|help|emergency|urgent(?:ly)?|"
    r"what\s+do\s+i\s+do|what\s+should\s+i\s+do|what\s+now|"
    r"(?:what\s+are\s+the\s+)?next\s+steps?)\b"
)
_IMMEDIACY_UK = _p(
    r"(зараз|негайно|терміново|у\s+мене\s+пацієнт|пацієнт\s+(?:не|за|у|в)|"
    r"хворий\s+(?:не|за|у|в)|він\s+не\s+|вона\s+не\s+|допоможіть|допоможи|"
    r"що\s+робити|які\s+дії|дії\?|швидка|невідкладн)"
)

_EMERGENCY_EN = _p(
    r"\b(cardiac\s+arrest|coding|code\s+blue|not\s+breathing|stopped\s+breathing|"
    r"no\s+pulse|pulseless|unresponsive|unconscious|anaphyla(?:xis|ctic)|"
    r"massive\s+(?:bleed|h[ae]emorrhage)|exsanguinating|"
    r"stroke\s+symptoms|facial\s+droop|slurred\s+speech|"
    r"crushing\s+chest\s+pain|stemi|status\s+epilepticus|seizing|"
    r"airway\s+obstruction|choking|shock|sepsis\s+with\s+hypotension|"
    r"overdose|poisoned|suicide\s+attempt|hanged|obstetric\s+h[ae]emorrhage)\b"
)
_EMERGENCY_UK = _p(
    r"(зупинка\s+серц|асистол|фібриляц[іи]я\s+шлуночк|не\s+дихає|зупинилось\s+дихання|"
    r"без\s+пульсу|немає\s+пульсу|непритомн|без\s+свідомості|анафілакт|"
    r"масивн[аеі]\s+кровотеч|профузн[аеі]\s+кровотеч|"
    r"ознаки\s+інсульту|перекошене\s+обличчя|мова\s+порушена|"
    r"пекучий\s+біль\s+у\s+грудях|інфаркт\s+зараз|епілептичний\s+статус|судоми\s+не\s+припиня|"
    r"обструкц[іи]я\s+дихальних\s+шляхів|задихається|подавився|"
    r"шок|отруївся|передозуванн|спроба\s+самогубства|повісився|"
    r"тиск\s+не\s+визначається|не\s+реагує)"
)

_SELF_HARM = (
    _p(
        r"\b(i\s+want\s+to\s+(?:die|kill\s+myself|end\s+(?:it|my\s+life))|"
        r"kill\s+myself|end\s+my\s+life|take\s+my\s+own\s+life|"
        r"how\s+(?:much|many)\s+\w+\s+(?:to|would)\s+kill\s+me|lethal\s+dose\s+for\s+me)\b"
    ),
    _p(
        r"(хочу\s+(?:померти|вбити\s+себе|покінчити)|вбити\s+себе|"
        r"покінчити\s+(?:з\s+собою|життям)|смертельна\s+доза\s+для\s+мене|"
        r"скільки\s+таблеток\s+щоб\s+померти)"
    ),
)

_PERSONAL_ADVICE = (
    _p(
        r"\b(i\s+have\s+(?:a\s+)?(?:pain|fever|rash|headache|cough|lump)|"
        r"my\s+(?:symptoms?|test\s+results?|diagnosis)|"
        r"should\s+i\s+(?:take|see|worry|go\s+to)|"
        r"is\s+it\s+(?:serious|dangerous)\s+(?:if|that)\s+i|do\s+i\s+need\s+(?:to\s+see|a\s+doctor))\b"
    ),
    _p(
        r"(у\s+мене\s+(?:болить|температура|висип|кашель)|мої\s+(?:симптоми|аналізи)|"
        r"чи\s+варто\s+мені|чи\s+треба\s+мені\s+(?:до\s+лікаря|звертатися)|"
        r"мені\s+(?:призначили|поставили\s+діагноз))"
    ),
)

# Not anchored on "how to": the request is unsafe because of what it asks
# for, not because of how it was phrased. "What is the protocol to poison
# someone undetectably?" is the same request wearing a reference costume.
_UNSAFE_REQUEST = (
    _p(
        r"\b(lethal\s+dose\s+(?:of|for)\s+(?!me\b)|"
        r"poison\s+(?:someone|somebody|a\s+patient|him|her|them)|"
        r"overdose\s+(?:someone|a\s+patient)|"
        r"sedate\s+(?:someone|a\s+patient)\s+without|"
        r"undetectable\s+(?:poison|drug|dose)|"
        r"hide\s+(?:the\s+)?(?:cause\s+of\s+death|evidence\s+of)|"
        r"falsify\s+[\w\s]{0,20}?(?:record|report|certificate|note))\b"
    ),
    _p(
        r"(смертельна\s+доза\s+(?:для|щоб)\s+(?!мене)|"
        r"отруїти\s+(?:людину|пацієнта|когось|його|її)|як\s+отруїти|"
        r"непомітн[аеиі]\s+отрут|"
        r"приховати\s+(?:причину\s+смерті|докази)|підробити\s+(?:запис|висновок|звіт))"
    ),
)

_OUT_OF_SCOPE = (
    _p(
        r"\b(ignore\s+(?:all\s+|your\s+|the\s+)*(?:previous\s+|prior\s+|above\s+)?"
        r"(?:instructions?|rules|prompts?)|"
        r"what\s+(?:model|llm|system\s+prompt)\s+are\s+you|reveal\s+your\s+prompt|"
        r"write\s+me\s+a\s+(?:poem|song|joke)|tell\s+me\s+a\s+joke|"
        r"what(?:'s|\s+is)\s+the\s+weather|who\s+won\s+the)\b"
    ),
    _p(
        r"(ігноруй\s+(?:свої\s+|усі\s+|всі\s+|мої\s+)*(?:попередні\s+)?"
        r"(?:інструкції|правила|команди)|"
        r"яка\s+(?:ти\s+)?модель|покажи\s+(?:свій\s+)?системний\s+промпт|"
        r"напиши\s+(?:вірш|пісню|жарт)|розкажи\s+анекдот|яка\s+погода)"
    ),
)


def _rules() -> tuple[TriageRule, ...]:
    rules: list[TriageRule] = []
    # Self-harm first: it is never a reference lookup and never suppressed.
    for i, pattern in enumerate(_SELF_HARM, start=1):
        rules.append(
            TriageRule(id=f"self_harm.{i}", reason=TriageReason.self_harm, patterns=(pattern,))
        )
    rules.append(
        TriageRule(
            id="emergency.en.active",
            reason=TriageReason.emergency,
            patterns=(_EMERGENCY_EN, _IMMEDIACY_EN),
        )
    )
    rules.append(
        TriageRule(
            id="emergency.uk.active",
            reason=TriageReason.emergency,
            patterns=(_EMERGENCY_UK, _IMMEDIACY_UK),
        )
    )
    for i, pattern in enumerate(_UNSAFE_REQUEST, start=1):
        rules.append(
            TriageRule(
                id=f"unsafe_request.{i}", reason=TriageReason.unsafe_request, patterns=(pattern,)
            )
        )
    for i, pattern in enumerate(_PERSONAL_ADVICE, start=1):
        rules.append(
            TriageRule(
                id=f"personal_advice.{i}",
                reason=TriageReason.personal_advice,
                patterns=(pattern,),
            )
        )
    for i, pattern in enumerate(_OUT_OF_SCOPE, start=1):
        rules.append(
            TriageRule(
                id=f"out_of_scope.{i}", reason=TriageReason.out_of_scope, patterns=(pattern,)
            )
        )
    return tuple(rules)


RULES: tuple[TriageRule, ...] = _rules()


def looks_like_reference_question(text: str) -> bool:
    return any(pattern.search(text) for pattern in _REFERENCE_PHRASING)


def has_strong_immediacy(text: str) -> bool:
    return any(pattern.search(text) for pattern in _STRONG_IMMEDIACY)


def match(text: str) -> TriageRule | None:
    """First matching rule, or None.

    Reference phrasing suppresses the emergency rules ONLY — a request for a
    lethal dose does not become safe because it was phrased as "what is the
    protocol for" — and it does not suppress anything when the text also
    carries a strong immediacy marker.
    """
    suppress_emergency = looks_like_reference_question(text) and not has_strong_immediacy(text)
    for rule in RULES:
        if suppress_emergency and rule.reason is TriageReason.emergency:
            continue
        if all(pattern.search(text) for pattern in rule.patterns):
            return rule
    return None
