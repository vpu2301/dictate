"""GET /v1/search/tips — what search does and does not do (sprint 15).

ADR-0021 accepted `simple` FTS with no stemming and promised the honest
user-facing explanation this sprint. The FE renders this content
verbatim so its copy can never drift from backend behaviour (the
core-service /note-structures motivation — but typed models, not bare
dicts). Static, versioned with the code: when search behaviour changes,
the tips change in the same PR.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from auth import Claims

from ..deps import requires_any

router = APIRouter(prefix="/v1/search", tags=["search"])


class SearchTip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    body: str


class SearchTipsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["uk", "en"]
    tips: list[SearchTip]


_TIPS: dict[str, list[SearchTip]] = {
    "uk": [
        SearchTip(
            key="no_stemming",
            title="Пошук за точною формою слова",
            body=(
                "Пошук не відмінює слова: «гіпертензія» не знайде «гіпертензії». "
                "Шукайте за основою слова або спробуйте кілька форм."
            ),
        ),
        SearchTip(
            key="synonyms",
            title="Синоніми та абревіатури розкриваються автоматично",
            body=(
                "Медичні абревіатури автоматично розширюються до повних форм і "
                "навпаки: «ІМ» знайде «інфаркт міокарда» та «MI». Знайдені "
                "розширення показуються під полем пошуку. Вимкнути можна "
                "параметром «точний пошук» (expand=false)."
            ),
        ),
        SearchTip(
            key="filters_and",
            title="Фільтри поєднуються через «І»",
            body=(
                "Пацієнт, автор, статус, дати та коди МКХ-10 звужують результат "
                "одночасно: кожен доданий фільтр зменшує список."
            ),
        ),
        SearchTip(
            key="whole_words",
            title="Пошук за цілими словами",
            body=(
                "Запит збігається з цілими словами тексту, а не з частинами: "
                "«кард» не знайде «кардіолог». Для кодів МКХ-10 користуйтеся "
                "полем кодів — воно шукає і за префіксом."
            ),
        ),
    ],
    "en": [
        SearchTip(
            key="no_stemming",
            title="Search matches exact word forms",
            body=(
                "Search does not stem words: 'hypertension' will not match "
                "'hypertensive'. Search by the word stem yourself or try "
                "several forms."
            ),
        ),
        SearchTip(
            key="synonyms",
            title="Synonyms and abbreviations expand automatically",
            body=(
                "Medical abbreviations expand to full forms and back: 'MI' "
                "finds «інфаркт міокарда». Applied expansions are shown under "
                "the search box; disable with exact search (expand=false)."
            ),
        ),
        SearchTip(
            key="filters_and",
            title="Filters compose with AND",
            body=(
                "Patient, author, status, dates and ICD-10 codes narrow the "
                "result together: every added filter shrinks the list."
            ),
        ),
        SearchTip(
            key="whole_words",
            title="Whole-word matching",
            body=(
                "Queries match whole words, not fragments: 'card' will not "
                "find 'cardiology'. For ICD-10 codes use the code field — it "
                "matches by prefix."
            ),
        ),
    ],
}


@router.get("/tips", response_model=SearchTipsResponse)
async def get_search_tips(
    claims: Annotated[
        Claims,
        Depends(requires_any(("report.read", "report"), ("stats.read", "tenant"))),
    ],
    language: Annotated[Literal["uk", "en"], Query()] = "uk",
) -> SearchTipsResponse:
    return SearchTipsResponse(language=language, tips=_TIPS[language])
