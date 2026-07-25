#!/usr/bin/env python3
"""Regenerate the frozen replay fixtures.

Run ONLY when a pipeline change is intentional and the ADR records it.
Regenerating to make a red test green is how a determinism contract
dies — the failing test is the contract doing its job.

    uv run --project services/nlp-service python \
        services/nlp-service/tests/fixtures/replay/regenerate.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # tests/ on the path

from unit.test_replay_determinism import ctx_from_fixture, stages_for_version  # noqa: E402

from nlp_service.pipeline.base import StageInput  # noqa: E402
from nlp_service.pipeline.orchestrator import Orchestrator, _encode_for_cache  # noqa: E402

SMOKING_OPTIONS = [
    {
        "value": "never",
        "label": "не палить",
        "aliases": ["не палить", "не курить", "заперечує куріння"],
    },
    {"value": "current", "label": "палить", "aliases": ["палить", "курить", "активний курець"]},
    {
        "value": "former",
        "label": "палив у минулому",
        "aliases": ["кинув палити", "кинула палити", "колишній курець"],
    },
]

BASE_CONTEXT = {
    "tenant_id": "00000000-0000-0000-0000-00000000000a",
    "language": "uk",
    "specialty": None,
    "reference_date": "2026-07-22",
    "is_partial": False,
    "fingerprint": "replay-fixture-fp",
    "template_sections": [],
}

TYPED_CONTEXT = {
    **BASE_CONTEXT,
    "template_sections": [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "name": "Статус куріння",
            "aliases": ["куріння"],
            "section_key": "smoking_status",
            "field_type": "choice",
            "options": SMOKING_OPTIONS,
        }
    ],
}

CASES: list[tuple[str, str, str, dict]] = [
    # (filename, pipeline_version, text, context)
    (
        "v1_0_0_numbers_and_dates",
        "nlp-v1.0.0",
        "артеріальний тиск сто сорок на дев'яносто, температура тридцять сім і два",
        BASE_CONTEXT,
    ),
    (
        "v1_0_0_plain_prose",
        "nlp-v1.0.0",
        "пацієнт скаржиться на головний біль протягом трьох днів",
        BASE_CONTEXT,
    ),
    (
        "v1_0_0_smoking_prose_no_typed_sections",
        "nlp-v1.0.0",
        "пацієнт не палить, алкоголь заперечує",
        BASE_CONTEXT,
    ),
    (
        "v1_1_0_numbers_and_dates",
        "nlp-v1.1.0",
        "артеріальний тиск сто сорок на дев'яносто, температура тридцять сім і два",
        BASE_CONTEXT,
    ),
    (
        "v1_1_0_extraction_filled",
        "nlp-v1.1.0",
        "пацієнт курить, стаж двадцять років",
        TYPED_CONTEXT,
    ),
    (
        "v1_1_0_extraction_negated",
        "nlp-v1.1.0",
        "пацієнт не палить, алкоголь заперечує",
        TYPED_CONTEXT,
    ),
    (
        "v1_1_0_extraction_empty",
        "nlp-v1.1.0",
        "скарги на головний біль протягом трьох днів",
        TYPED_CONTEXT,
    ),
]


async def main() -> None:
    for name, version, text, context in CASES:
        doc = {
            "pipeline_version": version,
            "context": context,
            "input": {"text": text},
            "expected_encoded": "",
        }
        orch = Orchestrator(stages=stages_for_version(version))
        out = await orch.run(ctx_from_fixture(doc), StageInput(text=text))
        doc["expected_encoded"] = _encode_for_cache(out).decode("utf-8")
        path = HERE / f"{name}.json"
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        print(f"wrote {path.name} ({version})")


if __name__ == "__main__":
    asyncio.run(main())
