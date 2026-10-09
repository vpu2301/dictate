"""HTTP client to ``asr-service`` — the eval runner's transcription path.

WHY THE REAL SERVICE AND NOT A LOCAL WHISPER. The number this produces is
only worth storing if it measures the engine clinicians actually dictate
into: same model, same prompt catalogue, same NLP post-processing at read
time. Loading a second Whisper inside autocomplete-service would measure a
different system and call it the product's WER.

AUTH: the CALLER'S bearer is forwarded (the asr-service → nlp-service
precedent). No service credentials are minted or stored here, which has one
visible consequence — scoring needs `asr.write`/`asr.read`, and
``docs/auth/permissions.csv`` deliberately withholds those from
tenant_admin. A tenant_admin can record, author and publish; a clinician
runs the scoring. The alternative was a stored service token that could
transcribe anything in any tenant, to save one role a click.

Every failure here is a run-item error string, never an exception that ends
a run: one utterance asr-service chokes on must not cost the other thirty
their scores.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


class AsrClientError(Exception):
    """asr-service could not be reached, or answered something unusable.

    ``code`` is a short stable token stored in ``corpus_eval_run_items.error``
    and rendered in the console; ``detail`` is for logs.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class AsrClientConfig:
    base_url: str = "http://asr-service:8000"
    # Submission uploads a WAV and returns 202 immediately; polling is
    # cheap. Neither call waits for inference — the run is pumped by the
    # client, so no request here is long-lived.
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class AsrJobState:
    status: str
    model: str | None
    error_kind: str | None
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class AsrTranscript:
    text: str
    model: str
    nlp_applied: bool
    nlp_pipeline_version: str | None
    # ── the measurement conditions (corpus-v2 §1.3.5, §4 P0-4) ──────────
    # A WER is only comparable to another WER taken under the same decode
    # settings, and asr-service already reports these in the transcript
    # metadata — this client used to drop them on the floor, which is how
    # two runs a month apart became mutually uninterpretable.
    #
    # `temperature` is absent on purpose: no ASR surface in this system
    # exposes it, and writing a plausible default into an evidence field is
    # worse than leaving the gap visible.
    beam_size: int | None = None
    #: The engine's own VAD — the input to the "<1 s of speech" and
    #: ">50% silence" hallucination flags. None means the flags could not run.
    speech_ms: int | None = None
    prompt_id: str | None = None


class AsrClient:
    def __init__(self, *, config: AsrClientConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=2.0,
                read=config.timeout_seconds,
                write=config.timeout_seconds,
                pool=2.0,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _headers(authorization: str | None) -> dict[str, str]:
        return {"Authorization": authorization} if authorization else {}

    async def default_prompt_id(
        self, *, language: str, specialty: str, authorization: str | None
    ) -> UUID:
        """The prompt asr-service will condition on.

        Submission requires a prompt_id from the catalogue (the FK would
        otherwise 500 the upload). Prefer the specialty's default, fall back
        to any default in that language, then to anything in that language —
        an eval run must not die because one specialty has no prompt.
        """
        try:
            resp = await self._client.get(
                "/asr/prompts",
                params={"language": language},
                headers=self._headers(authorization),
            )
        except httpx.HTTPError as exc:
            raise AsrClientError("asr_unreachable", str(exc)) from None
        _raise_on_auth(resp, "asr.read")
        if resp.status_code != 200:
            raise AsrClientError("asr_prompts_failed", f"HTTP {resp.status_code}")

        rows: list[dict[str, Any]] = resp.json()
        if not rows:
            raise AsrClientError("no_prompt", f"no prompt catalogue for {language}")
        for row in rows:
            if row.get("specialty") == specialty and row.get("is_default"):
                return UUID(str(row["id"]))
        for row in rows:
            if row.get("is_default"):
                return UUID(str(row["id"]))
        return UUID(str(rows[0]["id"]))

    async def submit(
        self,
        *,
        wav: bytes,
        prompt_id: UUID,
        language: str,
        authorization: str | None,
    ) -> UUID:
        """Queue one utterance. No encounter is attached: eval audio is a
        synthetic script, not a visit, and linking it to one would put a
        recording of nobody into a real patient's timeline."""
        try:
            resp = await self._client.post(
                "/asr/jobs",
                files={"audio": ("take.wav", wav, "audio/wav")},
                data={"prompt_id": str(prompt_id), "language": language},
                headers=self._headers(authorization),
            )
        except httpx.HTTPError as exc:
            raise AsrClientError("asr_unreachable", str(exc)) from None
        _raise_on_auth(resp, "asr.write")
        if resp.status_code == 429:
            # The per-tenant concurrent-job cap. Retryable, and the pump
            # retries it on the next tick rather than failing the item.
            raise AsrClientError("asr_busy", "per-tenant concurrent job limit")
        if resp.status_code != 202:
            raise AsrClientError("asr_rejected", _problem(resp))
        return UUID(str(resp.json()["id"]))

    async def job_state(
        self, *, job_id: UUID, authorization: str | None
    ) -> AsrJobState:
        try:
            resp = await self._client.get(
                f"/asr/jobs/{job_id}", headers=self._headers(authorization)
            )
        except httpx.HTTPError as exc:
            raise AsrClientError("asr_unreachable", str(exc)) from None
        _raise_on_auth(resp, "asr.read")
        if resp.status_code != 200:
            raise AsrClientError("asr_job_missing", f"HTTP {resp.status_code}")
        body = resp.json()
        return AsrJobState(
            status=str(body.get("status")),
            model=body.get("model"),
            error_kind=body.get("error_kind"),
            error_detail=body.get("error_detail"),
        )

    async def transcript(
        self, *, job_id: UUID, authorization: str | None
    ) -> AsrTranscript:
        """The NLP-enriched transcript — deliberately, not the raw one.

        The corpus's gold column is post-NLP text ("140/90 мм рт. ст.", not
        "сто сорок на дев'яносто"), so scoring the raw ASR string would
        report the normaliser's every success as an error. This mirrors what
        scripts/eval/run_wer.py measures.
        """
        try:
            resp = await self._client.get(
                f"/asr/jobs/{job_id}/result", headers=self._headers(authorization)
            )
        except httpx.HTTPError as exc:
            raise AsrClientError("asr_unreachable", str(exc)) from None
        _raise_on_auth(resp, "asr.read")
        if resp.status_code != 200:
            raise AsrClientError("asr_result_failed", _problem(resp))
        body = resp.json()
        text = " ".join(
            str(seg.get("text") or "").strip() for seg in body.get("segments", [])
        ).strip()
        meta = body.get("metadata") or {}
        speech_seconds = meta.get("vad_seconds_speech")
        return AsrTranscript(
            text=text,
            model=str(meta.get("model") or "unknown"),
            nlp_applied=bool(body.get("nlp_applied")),
            nlp_pipeline_version=body.get("nlp_pipeline_version"),
            beam_size=_as_int(meta.get("beam_size")),
            speech_ms=(
                None if speech_seconds is None else int(round(float(speech_seconds) * 1000))
            ),
            prompt_id=(None if meta.get("prompt_id") is None else str(meta["prompt_id"])),
        )


def _as_int(value: object) -> int | None:
    """Metadata is another service's JSON: absent, null and malformed all
    have to mean "not reported" rather than "run failed"."""
    try:
        return None if value is None else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _raise_on_auth(resp: httpx.Response, action: str) -> None:
    """401 and 403 are the same class of answer: this CALLER may not do this.

    Observed the hard way against the dev stack — a token whose issuer the
    other service did not accept came back 401, and each utterance was
    marked failed in turn until a whole run had been burned on what was one
    problem with one bearer. Both codes now raise the same error, which the
    router turns into a single 403 for the operator with the run untouched.
    """
    if resp.status_code in (401, 403):
        raise AsrClientError(
            "asr_forbidden", f"caller cannot use {action} (HTTP {resp.status_code})"
        )


def _problem(resp: httpx.Response) -> str:
    """Squeeze an RFC-9457 problem document into one loggable line."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        for key in ("code", "reason", "detail", "title"):
            if body.get(key):
                return f"HTTP {resp.status_code} {body[key]}"
    return f"HTTP {resp.status_code}"
