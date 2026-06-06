"""Pipeline orchestrator with idempotence cache.

Why a single orchestrator class instead of inlining the loop:
1. The cache + idempotence key are infrastructure concerns that don't
   belong in any stage's responsibility.
2. Sprint 7's eval harness will replay historical inputs through the
   exact same orchestrator with frozen pipeline_version +
   abbreviation_snapshot.fingerprint — byte-equal output is the
   reproducibility contract.
3. Idempotence violations are detectable HERE (compare cache hit vs
   fresh run) — the alert lives in ``mdx_nlp_idempotence_violations_total``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from typing import Any

from opentelemetry import metrics

from .base import (
    AbbreviationSnapshot,
    PipelineWarning,
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
)

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.nlp.orchestrator")
_cache_hits = _meter.create_counter(
    "mdx_nlp_cache_hits_total",
    description="Idempotence-cache hits at orchestrator",
    unit="1",
)
_cache_misses = _meter.create_counter(
    "mdx_nlp_cache_misses_total",
    description="Idempotence-cache misses at orchestrator",
    unit="1",
)
_idempotence_violations = _meter.create_counter(
    "mdx_nlp_idempotence_violations_total",
    description="Identical inputs produced different outputs — bug.",
    unit="1",
)
_stage_latency_ms = _meter.create_histogram(
    "mdx_nlp_request_duration_ms",
    description="Per-stage latency",
    unit="ms",
)


class CacheProtocol:
    """Tiny duck-typed cache surface; real impl in main_deps backed by Redis."""

    async def get(self, key: str) -> bytes | None: ...  # pragma: no cover

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None: ...  # pragma: no cover


class Orchestrator:
    """Run the configured stages in order, with cache + telemetry.

    ``cache`` may be None — tests instantiate without one. Production
    always supplies a Redis-backed instance.
    """

    def __init__(
        self,
        *,
        stages: list[Stage],
        cache: CacheProtocol | None = None,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self._stages = stages
        self._cache = cache
        self._cache_ttl = cache_ttl_seconds

    async def run(
        self,
        ctx: ProcessingContext,
        initial: StageInput,
    ) -> StageOutput:
        key = idempotence_key(ctx, initial)
        if self._cache is not None:
            cached = await self._cache.get(key)
            if cached is not None:
                _cache_hits.add(1, {"language": ctx.language})
                return _decode_cached(cached)
            _cache_misses.add(1, {"language": ctx.language})

        current = StageOutput(
            text=initial.text,
            words=initial.words,
            confidence_spans=initial.confidence_spans,
            voice_commands=initial.voice_commands,
            operations=initial.operations,
            warnings=initial.warnings,
        )

        import time

        for stage in self._stages:
            if ctx.is_partial and not stage.runs_on_partials:
                current = StageOutput(
                    text=current.text,
                    words=current.words,
                    confidence_spans=current.confidence_spans,
                    voice_commands=current.voice_commands,
                    operations=current.operations,
                    warnings=current.warnings,
                    metadata={**current.metadata, f"{stage.name}.skipped_partial": True},
                )
                continue
            t0 = time.monotonic()
            try:
                out = await stage.process(ctx, current.as_input())
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "nlp.stage_failed",
                    extra={"stage": stage.name, "error": str(exc)},
                )
                out = StageOutput(
                    text=current.text,
                    words=current.words,
                    confidence_spans=current.confidence_spans,
                    voice_commands=current.voice_commands,
                    operations=current.operations,
                    warnings=current.warnings
                    + (
                        PipelineWarning(
                            code="stage_failed",
                            detail=f"{type(exc).__name__}: {exc}",
                            stage=stage.name,
                        ),
                    ),
                    metadata={**current.metadata, f"{stage.name}.error": type(exc).__name__},
                )
            dt_ms = (time.monotonic() - t0) * 1000.0
            _stage_latency_ms.record(
                dt_ms, {"stage": stage.name, "language": ctx.language}
            )
            current = StageOutput(
                text=out.text,
                words=out.words,
                confidence_spans=out.confidence_spans,
                voice_commands=out.voice_commands,
                operations=out.operations,
                warnings=out.warnings,
                metadata={**current.metadata, **out.metadata},
            )

        if self._cache is not None:
            await self._cache.set(key, _encode_for_cache(current), self._cache_ttl)
        return current


# ── Idempotence key ─────────────────────────────────────────────────


def idempotence_key(ctx: ProcessingContext, initial: StageInput) -> str:
    """Stable hash over (input, ctx). Pipeline_version + snapshot
    fingerprint are part of the hash so a bump invalidates the cache."""
    doc: dict[str, Any] = {
        "v": "nlp-cache-v1",
        "pipeline_version": ctx.pipeline_version,
        "tenant_id": str(ctx.tenant_id),
        "language": ctx.language,
        "specialty": ctx.specialty,
        "reference_date": ctx.reference_date.isoformat(),
        "is_partial": ctx.is_partial,
        "snapshot_fingerprint": ctx.abbreviation_snapshot.fingerprint,
        "decimal_separator": ctx.decimal_separator,
        "bp_separator": ctx.bp_separator,
        "date_format": ctx.date_format,
        "template_sections": [
            {"id": str(s.id), "name": s.name, "aliases": list(s.aliases)}
            for s in ctx.template_sections
        ],
        "text": initial.text,
        "words": [
            {
                "text": w.text,
                "start_s": w.start_s,
                "end_s": w.end_s,
                "probability": w.probability,
                "is_voice_command_token": w.is_voice_command_token,
            }
            for w in initial.words
        ],
    }
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ── Cache (de)serialization ─────────────────────────────────────────


def _encode_for_cache(out: StageOutput) -> bytes:
    return json.dumps(
        {
            "text": out.text,
            "words": [
                {
                    "text": w.text,
                    "start_s": w.start_s,
                    "end_s": w.end_s,
                    "probability": w.probability,
                    "is_voice_command_token": w.is_voice_command_token,
                }
                for w in out.words
            ],
            "confidence_spans": [asdict(s) for s in out.confidence_spans],
            "voice_commands": [asdict(c) for c in out.voice_commands],
            "operations": [asdict(o) for o in out.operations],
            "warnings": [asdict(w) for w in out.warnings],
            "metadata": _coerce_jsonable(out.metadata),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _decode_cached(raw: bytes) -> StageOutput:
    from .base import (
        CommandSlot,
        ConfidenceSpan,
        Operation,
        PipelineWarning,
        Word,
    )

    doc = json.loads(raw.decode("utf-8"))
    return StageOutput(
        text=doc["text"],
        words=tuple(Word(**w) for w in doc.get("words", [])),
        confidence_spans=tuple(ConfidenceSpan(**s) for s in doc.get("confidence_spans", [])),
        voice_commands=tuple(CommandSlot(**c) for c in doc.get("voice_commands", [])),
        operations=tuple(Operation(**o) for o in doc.get("operations", [])),
        warnings=tuple(PipelineWarning(**w) for w in doc.get("warnings", [])),
        metadata=dict(doc.get("metadata", {})),
    )


def _coerce_jsonable(d: dict[str, Any]) -> dict[str, Any]:
    """Best-effort: strip non-JSON values from per-stage metadata."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out
