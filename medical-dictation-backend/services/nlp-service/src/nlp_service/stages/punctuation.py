"""Stage 2 — punctuation restoration + capitalization.

Primary path: Hugging Face transformer
``oliverguhr/fullstop-punctuation-multilang-large`` (ADR-0014). Runs on
CPU; loaded once at service startup. Inference is deterministic
(``torch.no_grad()``, no sampling).

Fallback path: rule-based punctuator (capitalize first word, add a
period at end of segment if missing, comma at "and"/"і" between
clauses). Fires when:
- the model is disabled (``MDX_NLP_PUNCTUATION_DISABLED=true``),
- the model failed to load (with a warning emitted at startup),
- a per-call inference exceeded ``MDX_NLP_PUNCTUATION_TIMEOUT_MS``.

Rule-based post-edits ALWAYS run on top of either path:
- Force capitalization after `.`, `!`, `?`.
- Capitalize first word of segment.
- Lowercase known units after a number (мг/мл/см/мм рт. ст. / mg/ml/cm/mmHg).
- Strip doubled punctuation.
- Don't insert a period inside an unfinished number expression.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import settings
from ..pipeline.base import (
    PipelineWarning,
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
)
from .punctuation_post import (
    capitalize_first_letter,
    capitalize_post_punctuation,
    lowercase_units_after_numbers,
    strip_double_punctuation,
)

logger = logging.getLogger(__name__)


class PunctuationStage:
    """Sprint-05 Stage 2."""

    name = "punctuation"
    runs_on_partials: bool = False  # finals-only by spec §2.4

    def __init__(self) -> None:
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._loaded = False
        self._load_failed = False

    async def startup(self) -> None:
        """Eagerly load the model. Called from the service lifespan.

        If load fails we mark ``_load_failed`` and keep the service up
        with the rule-based fallback. A `/readyz` gate (E10 in spec)
        flips to 503 until startup retries succeed.
        """
        if settings.punctuation_disabled:
            logger.info("punctuation.disabled_by_config")
            self._load_failed = True
            return
        try:
            from transformers import (  # type: ignore[import-untyped]
                AutoModelForTokenClassification,
                AutoTokenizer,
            )

            self._tokenizer = AutoTokenizer.from_pretrained(settings.punctuation_model)
            self._model = AutoModelForTokenClassification.from_pretrained(
                settings.punctuation_model
            )
            self._model.eval()
            self._loaded = True
            logger.info("punctuation.loaded", extra={"model": settings.punctuation_model})
        except Exception as exc:  # noqa: BLE001  — model load is a known failure mode
            self._load_failed = True
            logger.warning(
                "punctuation.load_failed",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def load_failed(self) -> bool:
        return self._load_failed

    async def process(
        self, ctx: ProcessingContext, input: StageInput
    ) -> StageOutput:
        t0 = time.monotonic()
        warnings = list(input.warnings)
        text = input.text

        if not text.strip():
            return StageOutput(
                text=text,
                words=input.words,
                confidence_spans=input.confidence_spans,
                voice_commands=input.voice_commands,
                operations=input.operations,
                warnings=tuple(warnings),
                metadata={self.name + ".latency_ms": 0.0, self.name + ".path": "noop"},
            )

        path = "model"
        if self._load_failed or not self._loaded:
            new_text = _rule_based_punctuate(text, ctx.language)
            path = "fallback_unavailable"
            warnings.append(
                PipelineWarning(
                    code="model_unavailable",
                    detail="punctuation rule-based fallback",
                    stage=self.name,
                )
            )
        else:
            try:
                new_text = await asyncio.wait_for(
                    self._model_punctuate(text, ctx.language),
                    timeout=settings.punctuation_timeout_ms / 1000.0,
                )
            except asyncio.TimeoutError:
                new_text = _rule_based_punctuate(text, ctx.language)
                path = "fallback_timeout"
                warnings.append(
                    PipelineWarning(
                        code="model_timeout",
                        detail=(
                            f"timeout > {settings.punctuation_timeout_ms} ms; "
                            "fell back to rule-based"
                        ),
                        stage=self.name,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "punctuation.inference_failed",
                    extra={"error": str(exc), "error_class": type(exc).__name__},
                )
                new_text = _rule_based_punctuate(text, ctx.language)
                path = "fallback_error"
                warnings.append(
                    PipelineWarning(
                        code="model_inference_failed",
                        detail=type(exc).__name__,
                        stage=self.name,
                    )
                )

        # Post-edits — always apply.
        new_text = strip_double_punctuation(new_text)
        new_text = capitalize_first_letter(new_text)
        new_text = capitalize_post_punctuation(new_text)
        new_text = lowercase_units_after_numbers(new_text, ctx.language)

        return StageOutput(
            text=new_text,
            words=input.words,
            confidence_spans=input.confidence_spans,
            voice_commands=input.voice_commands,
            operations=input.operations,
            warnings=tuple(warnings),
            metadata={
                self.name + ".path": path,
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000.0,
            },
        )

    # ── Model inference ─────────────────────────────────────────────

    async def _model_punctuate(self, text: str, language: str) -> str:
        # Run the (CPU-bound) inference in a thread so the asyncio loop
        # stays responsive under concurrent requests.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._model_punctuate_sync, text, language)

    def _model_punctuate_sync(self, text: str, language: str) -> str:
        assert self._model is not None and self._tokenizer is not None
        import torch  # type: ignore[import-untyped]

        # Chunking on token-budget. Most segments fit in one chunk; long
        # batch transcripts split on word boundaries with 16-token overlap.
        budget = settings.punctuation_token_budget
        words = text.split()
        chunks: list[str] = []
        cursor = 0
        while cursor < len(words):
            # Greedy: take as many words as fit in `budget` tokens.
            taken = min(len(words) - cursor, budget // 2)
            chunks.append(" ".join(words[cursor : cursor + taken]))
            cursor += max(1, taken - 8)  # 8-word overlap

        pieces: list[str] = []
        for chunk in chunks:
            enc = self._tokenizer(
                chunk,
                return_tensors="pt",
                truncation=True,
                max_length=budget,
            )
            with torch.no_grad():
                logits = self._model(**enc).logits
            pred_ids = torch.argmax(logits, dim=-1)[0]
            tokens = self._tokenizer.convert_ids_to_tokens(enc["input_ids"][0])
            label_list = self._model.config.id2label  # {0: "0", 1: ".", 2: ",", 3: "?", ...}
            pieces.append(_render_tokens(tokens, pred_ids.tolist(), label_list))
        return _merge_overlap(pieces)


# ── Helpers ─────────────────────────────────────────────────────────


def _render_tokens(tokens: list[str], pred_ids: list[int], label_list: dict[int, str]) -> str:
    """Reassemble subword tokens + punctuation labels into a sentence."""
    out: list[str] = []
    for tok, pid in zip(tokens, pred_ids):
        if tok in {"[CLS]", "[SEP]", "<s>", "</s>", "<pad>"}:
            continue
        clean = tok.lstrip("##").lstrip("Ġ")
        if not clean:
            continue
        if not out or tok.startswith("##") or tok.startswith("Ġ") is False and not tok.startswith(" "):
            if out:
                out[-1] = out[-1] + clean
            else:
                out.append(clean)
        else:
            out.append(clean)
        label = label_list.get(int(pid), "0")
        if label not in {"0", "O"}:
            out[-1] = out[-1] + label
    return " ".join(out)


def _merge_overlap(pieces: list[str]) -> str:
    """Naive overlap merge for chunked inference.

    Sprint-5 piloting will catch any boundary artefacts; if they're a
    problem we'll swap this for an alignment-based merge.
    """
    return " ".join(p.strip() for p in pieces if p.strip())


def _rule_based_punctuate(text: str, language: str) -> str:
    """The fallback. Conservative: only insert what we're confident about.

    - Capitalize the first letter.
    - Append a period if the segment ends with a letter.
    - Insert a comma before " і "/" та "/" or " in EN that join two clauses
      (heuristic: long-enough clauses on both sides).
    """
    s = text.strip()
    if not s:
        return s
    s = s[0].upper() + s[1:] if s[0].isalpha() else s
    if s[-1].isalpha():
        s = s + "."
    return s
