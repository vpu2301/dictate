"""LLM jury — three votes, fail-closed parsing, and the PHI boundary.

ADR-0044 §1, non-negotiable: mined and telemetry-derived candidates are
PHI-derived and are judged only in-perimeter. Routing is enforced HERE, by
source_kind, with a raise — not by a config flag someone flips at 2 a.m.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

# source_kinds whose text derives from PHI. Everything else originates from
# public open data (terminology, template generation) or the author's own
# keyboard.
PHI_DERIVED_SOURCE_KINDS: frozenset[str] = frozenset({"mined", "telemetry_gap"})


class PHIBoundaryViolation(RuntimeError):  # noqa: N818 - ADR-0044 names this exception
    """A PHI-derived candidate was about to leave the perimeter."""


class JuryLLMClient(Protocol):
    """A completion backend the jury can vote on."""

    @property
    def in_perimeter(self) -> bool: ...

    @property
    def model_name(self) -> str: ...

    async def complete(self, prompt: str) -> str: ...


class JuryVote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["accept", "reject"]
    reason: str
    suggested_edit: str | None = None


@dataclass(frozen=True, slots=True)
class JuryResult:
    votes: list[JuryVote]
    unanimous_accept: bool
    majority_accept: bool
    disagreement: bool
    engine: str  # 'jury:<model>:<prompt_version>'


def enforce_phi_boundary(source_kind: str, client: JuryLLMClient) -> None:
    """Raises — never warns — when a PHI-derived candidate is routed outside."""
    if source_kind in PHI_DERIVED_SOURCE_KINDS and not client.in_perimeter:
        raise PHIBoundaryViolation(
            f"candidate with source_kind={source_kind!r} must be judged "
            f"in-perimeter; refusing external client {client.model_name!r} "
            "(ADR-0044 §1)"
        )


def parse_vote(raw: str) -> JuryVote:
    """Strict parse; anything malformed counts as reject (fail-closed)."""
    try:
        payload = json.loads(_extract_json(raw))
        return JuryVote.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError):
        return JuryVote(verdict="reject", reason="malformed jury output", suggested_edit=None)


def _extract_json(raw: str) -> str:
    """Models wrap JSON in prose/fences; take the outermost object only."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in jury output")
    return raw[start : end + 1]


def tally(votes: list[JuryVote], *, model_name: str, prompt_version: str) -> JuryResult:
    accepts = sum(1 for v in votes if v.verdict == "accept")
    return JuryResult(
        votes=votes,
        unanimous_accept=accepts == len(votes) and len(votes) > 0,
        majority_accept=accepts * 2 > len(votes),
        disagreement=0 < accepts < len(votes),
        engine=f"jury:{model_name}:{prompt_version}",
    )


async def run_jury(
    *,
    client: JuryLLMClient,
    source_kind: str,
    prompts: list[str],
    prompt_version: str,
) -> JuryResult:
    """Three independent votes (distinct prompt variants). The boundary check
    runs before ANY network call."""
    enforce_phi_boundary(source_kind, client)
    votes = [parse_vote(await client.complete(p)) for p in prompts]
    return tally(votes, model_name=client.model_name, prompt_version=prompt_version)
