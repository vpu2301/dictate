"""/corpus/eval — authoring, publishing and scoring (migration 0091).

The repository layer is stubbed (monkeypatched per test) rather than
emulated in SQL: what these tests are about is the pipeline's decisions —
what is refused, what is frozen, what a WER is computed against and what a
run does when asr-service misbehaves. RLS and the SQL itself are
integration territory, as in test_corpus_eval_route.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from autocomplete_service import eval_lines, eval_normalize, eval_stats, eval_wer
from autocomplete_service import eval_repository as eval_repo
from autocomplete_service.deps import install_state
from autocomplete_service.integrations.asr_client import (
    AsrClientError,
    AsrJobState,
    AsrTranscript,
)
from autocomplete_service.routers import corpus_eval_pipeline as pipeline
from autocomplete_service.routers.corpus_eval_pipeline import (
    AdhocRequest,
    ImportRequest,
    LineRequest,
    StartRunRequest,
    advance_run,
    compare_runs,
    create_line,
    delete_line,
    import_csv,
    publish_snapshot,
    save_adhoc,
    start_run,
    update_line,
)
from fastapi import HTTPException

from auth import Claims

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _claims(roles=("clinician",)) -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=list(roles), scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


def _wav(seconds: float = 1.0) -> bytes:
    frames = int(16_000 * seconds)
    data = b"\x00" * (frames * 2)
    fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


class _Conn:
    """Every repo call is stubbed, so the connection only has to satisfy
    ``tenant_connection``: a transaction and the ``set_config`` execute."""

    async def execute(self, sql: str, *args):
        return ""

    async def fetch(self, sql: str, *args):
        return []

    async def fetchrow(self, sql: str, *args):
        return None

    def transaction(self):
        class _Tx:
            async def start(self): ...
            async def commit(self): ...
            async def rollback(self): ...

        return _Tx()


class _Acquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _Pool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def write_event(self, **kw):
        self.events.append(kw)


class _Metric:
    def __init__(self) -> None:
        self.points: list[tuple[int, dict]] = []

    def add(self, value, attrs=None):
        self.points.append((value, attrs or {}))


class _RateLimiter:
    async def check(self, *, user_id):
        return (True, 0)


class _State:
    def __init__(self, conn=None, asr=None) -> None:
        self.app_pool = _Pool(conn or _Conn())
        self.audit_writer = _Audit()
        self.phrase_rate_limiter = _RateLimiter()
        self.pii_rejections_metric = _Metric()
        self.asr_client = asr


def _line_row(script_id="uk-cardiology-a001", **over) -> dict:
    row = {
        "id": uuid4(),
        "script_id": script_id,
        "language": "uk",
        "specialty": "cardiology",
        "subset": None,
        "say": "Скарги на задишку при навантаженні.",
        "transcript": "Скарги на задишку при навантаженні.",
        "condition": None,
        "source": "authored",
        # 0092: a console-authored line joins the tunable half by default.
        "dataset": "dev",
        # 0095: not part of the paired design unless asked for.
        "paired": False,
        "created_by": uuid4(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(over)
    return row


# ══ authoring ══════════════════════════════════════════════════════════


async def test_new_line_gets_an_authored_id_that_cannot_shadow_a_vendored_one(
    monkeypatch,
):
    """The generated id carries an `a`, so growing the vendored script later
    can never collide with a line a tenant already recorded."""
    monkeypatch.setattr(eval_repo, "taken_script_ids", _async(set()))
    captured: dict = {}

    async def _insert(conn, **kw):
        captured.update(kw)
        return _line_row(script_id=kw["script_id"])

    monkeypatch.setattr(eval_repo, "insert_script_item", _insert)
    state = _State()
    install_state(state)

    dto = await create_line(
        LineRequest(
            language="uk",
            specialty="cardiology",
            subset="numbers_doses_units",
            say="Тиск сто сорок на дев'яносто.",
            transcript="Тиск 140/90.",
        ),
        _claims(),
    )
    assert dto.script_id == "uk-cardiology-a001"
    assert dto.script_id not in eval_lines.ROW_BY_ID
    # The gold text is stored as given; `say` is what gets read aloud.
    assert captured["transcript"] == "Тиск 140/90."
    assert captured["source"] == "authored"
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_line_added"


async def test_new_line_defaults_the_gold_text_to_the_spoken_form(monkeypatch):
    monkeypatch.setattr(eval_repo, "taken_script_ids", _async(set()))
    captured: dict = {}

    async def _insert(conn, **kw):
        captured.update(kw)
        return _line_row(script_id=kw["script_id"], transcript=kw["transcript"])

    monkeypatch.setattr(eval_repo, "insert_script_item", _insert)
    install_state(_State())

    await create_line(
        LineRequest(language="en", specialty="radiology", say="Lung fields are clear."),
        _claims(),
    )
    assert captured["transcript"] == "Lung fields are clear."


async def test_new_line_refuses_pii_before_a_row_exists(monkeypatch):
    """The sweep is at the boundary: nothing is written, and the finding
    names the pattern class without echoing the match."""
    inserted = []
    monkeypatch.setattr(eval_repo, "taken_script_ids", _async(set()))
    monkeypatch.setattr(
        eval_repo, "insert_script_item", _record(inserted, _line_row())
    )
    state = _State()
    install_state(state)

    with pytest.raises(HTTPException) as exc:
        await create_line(
            LineRequest(
                language="uk",
                specialty="general",
                say="Пацієнт, ІПН 1234567890, скаржиться на біль.",
            ),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "pii_detected"
    assert "ipn" in exc.value.detail["findings"]["say"]
    assert "1234567890" not in json.dumps(exc.value.detail, ensure_ascii=False)
    assert inserted == []
    sec = state.audit_writer.events[0]
    assert sec["kind"] == "autocomplete.phrase.write_rejected_pii"
    assert "1234567890" not in json.dumps(sec["payload"], ensure_ascii=False)


async def test_new_line_refuses_an_invented_subset(monkeypatch):
    """The six adversarial subsets are the measurement's buckets; a seventh
    invented at the keyboard would produce a WER nobody can read."""
    install_state(_State())
    with pytest.raises(HTTPException) as exc:
        await create_line(
            LineRequest(
                language="uk", specialty="general", subset="my_own_subset", say="Тест."
            ),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["fields"] == [
        {"field": "subset", "code": "unknown_subset"}
    ]


@pytest.mark.parametrize("route", ["update", "delete"])
async def test_vendored_lines_are_immutable(route):
    """A vendored line is shared by every tenant and by every corpus already
    exported from it."""
    install_state(_State())
    with pytest.raises(HTTPException) as exc:
        if route == "update":
            await update_line(
                "uk-cardiology-101",
                LineRequest(language="uk", specialty="cardiology", say="Змінено."),
                _claims(),
            )
        else:
            await delete_line("uk-cardiology-101", _claims())
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "builtin_line_immutable"


async def test_deleting_a_line_takes_its_take_with_it(monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(eval_repo, "delete_script_item", _async(True))

    async def _del_take(conn, script_id):
        deleted.append(script_id)
        return True

    monkeypatch.setattr(eval_repo, "delete_take", _del_take)
    state = _State()
    install_state(state)

    await delete_line("uk-cardiology-a001", _claims())
    assert deleted == ["uk-cardiology-a001"]
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_line_deleted"


# ══ ad-hoc capture ═════════════════════════════════════════════════════


async def test_adhoc_without_the_attestation_is_refused(monkeypatch):
    """The attestation is the only control over names — no regex catches
    them — so it is a hard gate, not a checkbox that defaults on."""
    monkeypatch.setattr(eval_repo, "taken_script_ids", _async(set()))
    install_state(_State())

    with pytest.raises(HTTPException) as exc:
        await save_adhoc(
            AdhocRequest(
                language="uk",
                specialty="general",
                say="Свіжий приклад формулювання.",
                condition="headset",
                audio_wav_base64=base64.b64encode(_wav()).decode(),
            ),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "attestation_required"


async def test_adhoc_creates_line_and_take_and_records_the_attestation(monkeypatch):
    wav = _wav(2.0)
    monkeypatch.setattr(eval_repo, "taken_script_ids", _async(set()))

    async def _insert(conn, **kw):
        return _line_row(script_id=kw["script_id"], source=kw["source"], say=kw["say"],
                         transcript=kw["transcript"], subset=kw["subset"])

    take_kwargs: dict = {}

    async def _upsert(conn, **kw):
        take_kwargs.update(kw)
        return {
            "id": uuid4(), "script_id": kw["script_id"],
            "script_version": kw["script_version"], "recorded_by": kw["recorded_by"],
            "language": kw["language"], "specialty": kw["specialty"],
            "subset": kw["subset"], "condition": kw["condition"],
            "duration_ms": kw["duration_ms"], "audio_sha256": kw["audio_sha256"],
            "size_bytes": len(wav), "created_at": NOW, "updated_at": NOW,
        }

    monkeypatch.setattr(eval_repo, "insert_script_item", _insert)
    monkeypatch.setattr(eval_repo, "upsert_take", _upsert)
    state = _State()
    install_state(state)

    resp = await save_adhoc(
        AdhocRequest(
            language="uk",
            specialty="general",
            subset="code_switching",
            say="Пацієнту призначено ACE inhibitor.",
            condition="headset",
            audio_wav_base64=base64.b64encode(wav).decode(),
            no_patient_data=True,
        ),
        _claims(),
    )
    assert resp.line.source == "adhoc"
    assert resp.take.duration_ms == 2000
    assert take_kwargs["audio_sha256"] == hashlib.sha256(wav).hexdigest()
    # The line and the take describe the same words: the take stores the
    # line's text, not the request's.
    assert take_kwargs["say"] == "Пацієнту призначено ACE inhibitor."
    event = state.audit_writer.events[0]
    assert event["kind"] == "corpus.eval_adhoc_captured"
    assert event["payload"]["no_patient_data_attested"] is True


async def test_adhoc_refuses_audio_that_is_not_the_corpus_format(monkeypatch):
    monkeypatch.setattr(eval_repo, "taken_script_ids", _async(set()))
    install_state(_State())
    with pytest.raises(HTTPException) as exc:
        await save_adhoc(
            AdhocRequest(
                language="uk", specialty="general", say="Тест формату.",
                condition="headset",
                audio_wav_base64=base64.b64encode(b"not a wav" * 16).decode(),
                no_patient_data=True,
            ),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "not_wav"


# ══ publishing ═════════════════════════════════════════════════════════


def _publish_row(script_id="uk-cardiology-101", **over) -> dict:
    wav = _wav(1.5)
    row = {
        "take_id": uuid4(),
        "script_id": script_id,
        "language": "uk",
        "specialty": "cardiology",
        "subset": None,
        "say": "Скарги на задишку.",
        "transcript": "Скарги на задишку.",
        "condition": "headset",
        "duration_ms": 1500,
        "audio_sha256": hashlib.sha256(wav).hexdigest(),
        "audio_wav": wav,
        "source": "builtin",
        # A vendored line is the frozen v1 corpus (0092).
        "dataset": "test",
        "paired": False,
        "recorded_by": uuid4(),
        # Epic F: the speaker consented, so nothing is dropped from the
        # snapshot. The withdrawal path has its own test.
        "excluded_by_consent": False,
    }
    row.update(over)
    return row


async def test_publish_freezes_the_takes_with_a_manifest_digest(monkeypatch):
    rows = [
        _publish_row(),
        _publish_row(
            "uk-noisy-a001", subset="phone_mic_noisy", source="adhoc",
            condition="noisy", specialty="general",
        ),
    ]
    monkeypatch.setattr(eval_repo, "fetch_for_publish", _async(rows))
    monkeypatch.setattr(eval_repo, "next_snapshot_version", _async(3))
    stored: dict = {}

    async def _insert_snapshot(conn, **kw):
        stored.update(kw)
        return {
            "id": uuid4(), "version": kw["version"],
            "utterance_count": kw["utterance_count"],
            "total_duration_ms": kw["total_duration_ms"],
            "manifest_sha256": kw["manifest_sha256"],
            "published_by": kw["published_by"], "created_at": NOW,
        }

    items_written: dict = {}

    async def _insert_items(conn, **kw):
        items_written.update(kw)

    monkeypatch.setattr(eval_repo, "insert_snapshot", _insert_snapshot)
    monkeypatch.setattr(eval_repo, "insert_snapshot_items", _insert_items)
    # Epic F: publishing registers the snapshot in the data register.
    monkeypatch.setattr(eval_repo, "upsert_registry_entry", _async({"id": uuid4()}))
    state = _State()
    install_state(state)

    dto = await publish_snapshot(_claims(("tenant_admin",)))
    assert dto.version == 3
    assert dto.utterance_count == 2
    assert dto.total_duration_ms == 3000
    assert len(dto.manifest_sha256) == 64

    manifest = json.loads(stored["manifest"])
    assert manifest["snapshot_version"] == 3
    by_id = {u["utterance_id"]: u for u in manifest["utterances"]}
    # The adversarial subset keeps its corpus path, and every file is hashed.
    assert by_id["uk-noisy-a001"]["path"] == "subsets/phone_mic_noisy/uk-noisy-a001"
    assert set(by_id["uk-noisy-a001"]["sha256"]) == {
        "audio.wav", "transcript.txt", "metadata.json"
    }
    # Each snapshot item points at the take it froze, so a later re-recording
    # is detectable rather than invisible.
    assert {i["take_id"] for i in items_written["items"]} == {
        r["take_id"] for r in rows
    }
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_published"


async def test_publish_refuses_the_whole_set_on_one_pii_finding(monkeypatch):
    rows = [
        _publish_row(),
        _publish_row(
            "uk-general-a002",
            say="Телефон пацієнта 0501234567.",
            transcript="Телефон пацієнта 0501234567.",
        ),
    ]
    monkeypatch.setattr(eval_repo, "fetch_for_publish", _async(rows))
    inserted: list = []
    monkeypatch.setattr(eval_repo, "insert_snapshot", _record(inserted, None))
    state = _State()
    install_state(state)

    with pytest.raises(HTTPException) as exc:
        await publish_snapshot(_claims())
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "pii_detected"
    assert "uk-general-a002" in exc.value.detail["findings"]
    assert inserted == []


async def test_publish_404_when_there_is_nothing_recorded(monkeypatch):
    monkeypatch.setattr(eval_repo, "fetch_for_publish", _async([]))
    install_state(_State())
    with pytest.raises(HTTPException) as exc:
        await publish_snapshot(_claims())
    assert exc.value.status_code == 404


# ══ scoring ════════════════════════════════════════════════════════════


class _FakeAsr:
    """asr-service, scripted. `behaviour` maps a script_id to what happens."""

    def __init__(self, *, transcripts=None, fail_submit=None, job_status="complete"):
        self.transcripts = transcripts or {}
        self.fail_submit = fail_submit or {}
        self.job_status = job_status
        self.submitted: list[tuple[str, UUID]] = []
        self.jobs: dict[UUID, str] = {}
        # What asr-service reports about HOW it decoded (0092): the run's
        # reproducibility record and the input to the VAD-based flags.
        self.speech_ms: int | None = None

    async def default_prompt_id(self, *, language, specialty, authorization):
        return UUID(int=7)

    async def submit(self, *, wav, prompt_id, language, authorization):
        script_id = self._id_for(wav)
        if script_id in self.fail_submit:
            raise AsrClientError(self.fail_submit[script_id])
        job_id = uuid4()
        self.jobs[job_id] = script_id
        self.submitted.append((script_id, job_id))
        return job_id

    def _id_for(self, wav: bytes) -> str:
        return self.wav_owner.get(wav, "?")

    async def job_state(self, *, job_id, authorization):
        return AsrJobState(
            status=self.job_status, model="large-v3", error_kind=None,
            error_detail=None,
        )

    async def transcript(self, *, job_id, authorization):
        script_id = self.jobs.get(job_id, self.job_owner.get(job_id, "?"))
        return AsrTranscript(
            text=self.transcripts.get(script_id, ""),
            model="large-v3",
            nlp_applied=True,
            nlp_pipeline_version="v1",
            beam_size=5,
            speech_ms=self.speech_ms,
            prompt_id=str(UUID(int=7)),
        )


async def test_start_run_lists_every_utterance_and_skips_unscorable_german(
    monkeypatch,
):
    snapshot_id = uuid4()
    items = [
        _snapshot_item("uk-a"),
        _snapshot_item("de-a", language="de", transcript="t", source="authored"),
    ]
    run_id = uuid4()
    written: dict = {}

    monkeypatch.setattr(
        eval_repo,
        "get_snapshot",
        _async({"id": snapshot_id, "version": 2, "manifest_sha256": "a" * 64}),
    )
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async(items))
    monkeypatch.setattr(eval_repo, "insert_run", _async({"id": run_id}))

    async def _insert_items(conn, **kw):
        written.update(kw)

    monkeypatch.setattr(eval_repo, "insert_run_items", _insert_items)
    monkeypatch.setattr(
        eval_repo, "count_by_status", _async({"pending": 1, "skipped": 1})
    )
    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(
        eval_repo,
        "list_run_items",
        _async([
            _item_row("uk-a", "pending"),
            _item_row("de-a", "skipped", error="language_unsupported"),
        ]),
    )
    state = _State()
    install_state(state)

    detail = await start_run(StartRunRequest(snapshot_id=snapshot_id), _claims())
    assert {sid: st for sid, _cond, st, _ in written["items"]} == {
        "uk-a": "pending", "de-a": "skipped",
    }
    # A German line is not a failure and not a zero — it is out of scope for
    # batch ASR, and the run says which.
    assert [i.error for i in detail.items if i.script_id == "de-a"] == [
        "language_unsupported"
    ]
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_run_started"


async def test_advance_scores_a_completed_job_against_the_snapshot_gold_text(
    monkeypatch,
):
    """The reference is the SNAPSHOT's text, not the live line's: that is the
    whole reason a snapshot exists."""
    run_id, snapshot_id = uuid4(), uuid4()
    job_id = uuid4()
    snapshot_items = [
        _snapshot_item(
            "uk-a",
            specialty="cardiology",
            subset="numbers_doses_units",
            transcript="тиск 140 на 90",
        ),
    ]
    scored_calls: list[dict] = []

    asr = _FakeAsr(transcripts={"uk-a": "тиск 140 на 80"})
    asr.job_owner = {job_id: "uk-a"}

    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async(snapshot_items))
    monkeypatch.setattr(
        eval_repo,
        "in_flight_items",
        _async([_item_row("uk-a", "transcribing", asr_job_id=job_id)]),
    )

    async def _scored(conn, **kw):
        scored_calls.append(kw)

    monkeypatch.setattr(eval_repo, "mark_item_scored", _scored)
    monkeypatch.setattr(eval_repo, "note_run_model", _async(None))
    monkeypatch.setattr(eval_repo, "claim_pending", _async([]))
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"scored": 1}))
    monkeypatch.setattr(
        eval_repo,
        "scored_items",
        _async([_scored_row("uk-a")]),
    )
    finished: dict = {}

    async def _finish(conn, **kw):
        finished.update(kw)
        return None

    monkeypatch.setattr(eval_repo, "finish_run", _finish)
    monkeypatch.setattr(eval_repo, "list_run_items", _async([_item_row("uk-a", "scored")]))
    state = _State(asr=asr)
    install_state(state)

    await advance_run(run_id, _claims(), authorization="Bearer t")

    assert scored_calls[0]["hypothesis"] == "тиск 140 на 80"
    # "90" → "80" is one substitution in a four-word reference.
    assert scored_calls[0]["wer"] == pytest.approx(0.25)
    assert scored_calls[0]["ref_words"] == 4
    # Nothing left open → the run closes with its aggregate and its model.
    assert finished["status"] == "complete"
    assert finished["model"] == "large-v3"
    summary = json.loads(finished["summary"])
    assert summary["overall"]["wer"] == pytest.approx(0.25)
    assert "numbers_doses_units" in summary["by_subset"]
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_run_completed"


async def test_advance_closes_a_run_that_scored_nothing_as_failed(monkeypatch):
    """A run where every utterance failed is not a WER of zero utterances
    dressed up as success."""
    run_id, snapshot_id = uuid4(), uuid4()
    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async([]))
    monkeypatch.setattr(eval_repo, "in_flight_items", _async([]))
    monkeypatch.setattr(eval_repo, "claim_pending", _async([]))
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"failed": 3}))
    monkeypatch.setattr(eval_repo, "scored_items", _async([]))
    finished: dict = {}

    async def _finish(conn, **kw):
        finished.update(kw)
        return None

    monkeypatch.setattr(eval_repo, "finish_run", _finish)
    monkeypatch.setattr(eval_repo, "list_run_items", _async([]))
    install_state(_State(asr=_FakeAsr()))

    await advance_run(run_id, _claims(), authorization="Bearer t")
    assert finished["status"] == "failed"
    assert json.loads(finished["summary"])["overall"]["wer"] is None


async def test_advance_answers_403_when_the_caller_cannot_use_asr(monkeypatch):
    """tenant_admin holds corpus.review but deliberately not asr.* — one
    clear refusal beats thirty utterances failing as though the audio were
    at fault."""
    run_id, snapshot_id = uuid4(), uuid4()

    class _Forbidden(_FakeAsr):
        async def default_prompt_id(self, **kw):
            raise AsrClientError("asr_forbidden", "caller lacks asr.read")

    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async([]))
    monkeypatch.setattr(eval_repo, "in_flight_items", _async([]))
    monkeypatch.setattr(
        eval_repo, "claim_pending", _async([_item_row("uk-a", "transcribing")])
    )
    monkeypatch.setattr(
        eval_repo,
        "snapshot_audio",
        _async(_snapshot_item("uk-a", audio_wav=_wav())),
    )
    install_state(_State(asr=_Forbidden()))

    with pytest.raises(HTTPException) as exc:
        await advance_run(run_id, _claims(("tenant_admin",)), authorization="Bearer t")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "asr_permission_required"


async def test_advance_fails_only_the_utterance_whose_audio_vanished(monkeypatch):
    run_id, snapshot_id = uuid4(), uuid4()
    failures: list[dict] = []

    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async([]))
    monkeypatch.setattr(eval_repo, "in_flight_items", _async([]))
    monkeypatch.setattr(
        eval_repo, "claim_pending", _async([_item_row("uk-gone", "transcribing")])
    )
    monkeypatch.setattr(
        eval_repo,
        "snapshot_audio",
        _async({"script_id": "uk-gone", "condition": "headset",
                "language": "uk", "specialty": "general",
                "transcript": "т", "audio_sha256": "0" * 64, "audio_wav": None}),
    )

    async def _failed(conn, **kw):
        failures.append(kw)

    monkeypatch.setattr(eval_repo, "mark_item_failed", _failed)
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"pending": 1}))
    monkeypatch.setattr(eval_repo, "list_run_items", _async([]))
    install_state(_State(asr=_FakeAsr()))

    await advance_run(run_id, _claims(), authorization="Bearer t")
    assert failures[0]["error"] == "audio_missing"


async def test_advance_puts_a_busy_utterance_back_in_the_queue(monkeypatch):
    """asr-service's concurrency cap is back-pressure, not an error: the
    utterance stays retryable instead of being scored as a failure."""
    run_id, snapshot_id = uuid4(), uuid4()
    released: list[dict] = []
    failed: list[dict] = []

    asr = _FakeAsr(fail_submit={"uk-a": "asr_busy"})
    asr.wav_owner = {}

    async def _submit(**kw):
        raise AsrClientError("asr_busy", "per-tenant concurrent job limit")

    asr.submit = _submit  # type: ignore[method-assign]

    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async([]))
    monkeypatch.setattr(eval_repo, "in_flight_items", _async([]))
    monkeypatch.setattr(
        eval_repo, "claim_pending", _async([_item_row("uk-a", "transcribing")])
    )
    monkeypatch.setattr(
        eval_repo,
        "snapshot_audio",
        _async(_snapshot_item("uk-a", audio_wav=_wav())),
    )

    async def _release(conn, **kw):
        released.append(kw)

    async def _failed(conn, **kw):
        failed.append(kw)

    monkeypatch.setattr(eval_repo, "release_item", _release)
    monkeypatch.setattr(eval_repo, "mark_item_failed", _failed)
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"pending": 1}))
    monkeypatch.setattr(eval_repo, "list_run_items", _async([]))
    install_state(_State(asr=asr))

    await advance_run(run_id, _claims(), authorization="Bearer t")
    assert released[0]["error"] == "asr_busy"
    assert failed == []


async def test_advance_on_a_finished_run_is_a_read(monkeypatch):
    run_id, snapshot_id = uuid4(), uuid4()
    monkeypatch.setattr(
        eval_repo, "get_run", _async(_run_row(run_id, snapshot_id, status="complete"))
    )
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"scored": 2}))
    monkeypatch.setattr(eval_repo, "list_run_items", _async([]))
    state = _State(asr=None)  # touching asr at all would be an AttributeError
    install_state(state)

    detail = await advance_run(run_id, _claims(), authorization="Bearer t")
    assert detail.run.status == "complete"


# ══ the scoring contract ═══════════════════════════════════════════════


def test_wer_matches_the_cli_harness():
    """scripts/eval/wer_lib.py is the release gate's implementation and
    eval_wer.py is the service's. Two numbers called WER that disagree is
    the worst outcome available, so parity is asserted rather than assumed.
    """
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location(
        "wer_lib", root / "scripts" / "eval" / "wer_lib.py"
    )
    assert spec and spec.loader
    wer_lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wer_lib)

    cases = [
        ("тиск 140 на 90", "тиск 140 на 80"),
        ("Blood pressure 138/84", "blood pressure 138 84"),
        ("інфаркт міокарда", "інфаркту міокарда"),
        ("", ""),
        ("", "щось"),
        ("щось", ""),
        ("Пацієнт скаржиться на біль", "пацієнт скаржиться на біль"),
    ]
    for ref, hyp in cases:
        assert eval_wer.wer(ref, hyp) == wer_lib.wer(ref, hyp), (ref, hyp)
        assert eval_wer.cer(ref, hyp) == wer_lib.cer(ref, hyp), (ref, hyp)
        assert eval_wer.tokenize(ref) == wer_lib.tokenize(ref)


def test_aggregate_weights_by_reference_words_not_by_utterance():
    """A three-word line and a thirty-word line are not half the evidence
    each."""
    summary = eval_wer.aggregate([
        {"wer": 1.0, "cer": 1.0, "ref_words": 3, "subset": None,
         "language": "uk", "reference": "a b c"},
        {"wer": 0.0, "cer": 0.0, "ref_words": 30, "subset": "drug_names",
         "language": "uk", "reference": "x" * 100},
    ])
    assert summary["overall"]["wer"] == pytest.approx(3 / 33, abs=1e-4)
    assert summary["by_subset"]["baseline"]["wer"] == 1.0
    assert summary["by_subset"]["drug_names"]["wer"] == 0.0


# ══ helpers ════════════════════════════════════════════════════════════


def _async(result):
    async def _fn(*args, **kwargs):
        return result

    return _fn


def _record(sink: list, result):
    async def _fn(*args, **kwargs):
        sink.append(kwargs)
        return result

    return _fn


def _run_row(
    run_id, snapshot_id, *, status="running", model="unknown", dataset="test", **over
) -> dict:
    row = {
        "id": run_id,
        "snapshot_id": snapshot_id,
        "snapshot_version": 1,
        "status": status,
        "model": model,
        "started_by": uuid4(),
        "started_at": NOW,
        "finished_at": None,
        "summary": None,
        # 0092 — the measurement conditions.
        "dataset": dataset,
        "normalizer_version": eval_normalize.VERSION,
        "corpus_sha256": "a" * 64,
        "engine": {},
        "bootstrap_seed": eval_stats.DEFAULT_SEED,
    }
    row.update(over)
    return row


def _item_row(script_id, status, *, asr_job_id=None, error=None, **over) -> dict:
    row = {
        "script_id": script_id,
        # 0095: a run item names the RECORDING it scored, not just the line.
        "condition": "headset",
        "status": status,
        "asr_job_id": asr_job_id,
        "hypothesis": None,
        "wer": None,
        "cer": None,
        "ref_words": None,
        "error": error,
        "wer_norm": None,
        "cer_norm": None,
        "ref_words_norm": None,
        "ref_chars_norm": None,
        "dose_tokens": None,
        "dose_exact": None,
        "flags": [],
        "speech_ms": None,
        "updated_at": NOW,
    }
    row.update(over)
    return row


def _snapshot_item(script_id, **over) -> dict:
    row = {
        "script_id": script_id,
        "language": "uk",
        "specialty": "general",
        "subset": None,
        "transcript": "т",
        "condition": "headset",
        "duration_ms": 1000,
        "audio_sha256": "0" * 64,
        "source": "builtin",
        "dataset": "test",
        "paired": False,
    }
    row.update(over)
    return row


def _scored_row(script_id, **over) -> dict:
    """One row as ``scored_items`` returns it, v2 columns included."""
    row = {
        "script_id": script_id,
        "wer": 0.25,
        "cer": 0.05,
        "ref_words": 4,
        "ref_chars": 14,
        "hypothesis": "тиск 140 на 80",
        "wer_norm": 0.25,
        "cer_norm": 0.05,
        "ref_words_norm": 4,
        "ref_chars_norm": 14,
        "dose_tokens": 2,
        "dose_exact": False,
        "flags": [],
        "speech_ms": 900,
        "subset": "numbers_doses_units",
        "language": "uk",
        "condition": "headset",
        "dataset": "test",
        "paired": False,
        "reference": "тиск 140 на 90",
    }
    row.update(over)
    return row


assert pipeline.SCORABLE_LANGUAGES == ("uk", "en")


# ══ corpus-v2: sets, import, comparison ════════════════════════════════


async def test_a_run_scores_one_set_and_records_how_it_was_measured(monkeypatch):
    """§1.3.5: the conditions are written down BEFORE anything is measured.
    A stored WER whose normalisation rules and corpus digest are unknown
    cannot be compared with any other WER."""
    snapshot_id, run_id = uuid4(), uuid4()
    items = [
        _snapshot_item("uk-old", dataset="test"),
        _snapshot_item("uk-new-1", dataset="dev"),
        _snapshot_item("uk-new-2", dataset="dev"),
    ]
    monkeypatch.setattr(
        eval_repo,
        "get_snapshot",
        _async({"id": snapshot_id, "version": 4, "manifest_sha256": "b" * 64}),
    )
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async(items))
    run_kwargs: dict = {}

    async def _insert_run(conn, **kw):
        run_kwargs.update(kw)
        return {"id": run_id}

    written: dict = {}

    async def _insert_items(conn, **kw):
        written.update(kw)

    monkeypatch.setattr(eval_repo, "insert_run", _insert_run)
    monkeypatch.setattr(eval_repo, "insert_run_items", _insert_items)
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"pending": 2}))
    monkeypatch.setattr(
        eval_repo, "get_run", _async(_run_row(run_id, snapshot_id, dataset="dev"))
    )
    monkeypatch.setattr(eval_repo, "list_run_items", _async([]))
    state = _State()
    install_state(state)

    await start_run(
        StartRunRequest(snapshot_id=snapshot_id, dataset="dev"), _claims()
    )
    # Only the dev half is listed — the holdout is not measured by a dev run.
    assert [sid for sid, _cond, _st, _err in written["items"]] == [
        "uk-new-1",
        "uk-new-2",
    ]
    assert run_kwargs["dataset"] == "dev"
    assert run_kwargs["normalizer_version"] == eval_normalize.VERSION
    assert run_kwargs["corpus_sha256"] == "b" * 64
    assert run_kwargs["bootstrap_seed"] == eval_stats.DEFAULT_SEED
    assert state.audit_writer.events[0]["payload"]["dataset"] == "dev"


async def test_asking_a_pre_split_snapshot_for_dev_is_an_answerable_409(monkeypatch):
    """A snapshot published before the split holds only holdout rows.
    Answering with a run that completes over an empty summary would read as
    a good result."""
    snapshot_id = uuid4()
    monkeypatch.setattr(
        eval_repo,
        "get_snapshot",
        _async({"id": snapshot_id, "version": 1, "manifest_sha256": "c" * 64}),
    )
    monkeypatch.setattr(
        eval_repo, "list_snapshot_items", _async([_snapshot_item("uk-a", dataset="test")])
    )
    install_state(_State())

    with pytest.raises(HTTPException) as exc:
        await start_run(
            StartRunRequest(snapshot_id=snapshot_id, dataset="dev"), _claims()
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "empty_dataset"
    assert exc.value.detail["available"] == ["test"]


async def test_advance_stores_both_scores_the_dose_verdict_and_the_flags(monkeypatch):
    """One completed utterance yields one observation: raw WER, normalised
    WER, dose accuracy and quarantine flags, written in one statement."""
    run_id, snapshot_id, job_id = uuid4(), uuid4(), uuid4()
    snapshot_items = [
        _snapshot_item(
            "uk-a",
            subset="numbers_doses_units",
            transcript="Призначено 25 мг двічі на добу.",
            duration_ms=4000,
        ),
    ]
    scored_calls: list[dict] = []
    asr = _FakeAsr(
        transcripts={"uk-a": "Призначено двадцять п'ять міліграмів двічі на добу."}
    )
    asr.job_owner = {job_id: "uk-a"}
    asr.speech_ms = 3200

    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async(snapshot_items))
    monkeypatch.setattr(
        eval_repo,
        "in_flight_items",
        _async([_item_row("uk-a", "transcribing", asr_job_id=job_id)]),
    )

    async def _scored(conn, **kw):
        scored_calls.append(kw)

    engine_calls: list[dict] = []
    monkeypatch.setattr(eval_repo, "mark_item_scored", _scored)
    monkeypatch.setattr(eval_repo, "note_run_model", _async(None))
    monkeypatch.setattr(eval_repo, "merge_run_engine", _record(engine_calls, None))
    monkeypatch.setattr(eval_repo, "claim_pending", _async([]))
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"scored": 1}))
    monkeypatch.setattr(eval_repo, "scored_items", _async([_scored_row("uk-a")]))
    monkeypatch.setattr(eval_repo, "finish_run", _async(None))
    monkeypatch.setattr(
        eval_repo, "list_run_items", _async([_item_row("uk-a", "scored")])
    )
    install_state(_State(asr=asr))

    await advance_run(run_id, _claims(), authorization="Bearer t")

    call = scored_calls[0]
    # The spoken form and the gold form are the same prescription: raw WER
    # punishes the difference, normalised WER does not.
    assert call["wer"] > 0
    assert call["wer_norm"] == 0
    assert call["dose_tokens"] == 2
    assert call["dose_exact"] is True
    assert json.loads(call["flags"]) == []
    assert call["speech_ms"] == 3200
    # The decode conditions asr-service reported travel onto the run.
    engine = json.loads(engine_calls[0]["engine"])
    assert engine["beam_size"] == 5
    assert engine["language_hints"] == ["uk"]
    # Nothing here exposes temperature; the gap is recorded, not guessed.
    assert engine["temperature"] is None


async def test_a_hallucinated_take_keeps_its_score_and_loses_its_vote(monkeypatch):
    """§4 P0-3: quarantined, not deleted — the summary lists it so it gets
    re-recorded, and the corpus average stops carrying a recording fault."""
    run_id, snapshot_id, job_id = uuid4(), uuid4(), uuid4()
    snapshot_items = [_snapshot_item("uk-a", transcript="Скарги на задишку.")]
    scored_calls: list[dict] = []
    asr = _FakeAsr(transcripts={"uk-a": "Дякую за перегляд!"})
    asr.job_owner = {job_id: "uk-a"}

    monkeypatch.setattr(eval_repo, "get_run", _async(_run_row(run_id, snapshot_id)))
    monkeypatch.setattr(eval_repo, "reset_stale_claims", _async(0))
    monkeypatch.setattr(eval_repo, "list_snapshot_items", _async(snapshot_items))
    monkeypatch.setattr(
        eval_repo,
        "in_flight_items",
        _async([_item_row("uk-a", "transcribing", asr_job_id=job_id)]),
    )

    async def _scored(conn, **kw):
        scored_calls.append(kw)

    finished: dict = {}

    async def _finish(conn, **kw):
        finished.update(kw)
        return None

    monkeypatch.setattr(eval_repo, "mark_item_scored", _scored)
    monkeypatch.setattr(eval_repo, "note_run_model", _async(None))
    monkeypatch.setattr(eval_repo, "merge_run_engine", _async(None))
    monkeypatch.setattr(eval_repo, "claim_pending", _async([]))
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"scored": 2}))
    monkeypatch.setattr(
        eval_repo,
        "scored_items",
        _async([
            _scored_row("uk-a", flags=["known_hallucination"], wer=1.0, wer_norm=1.0),
            _scored_row("uk-b", wer=0.1, wer_norm=0.1),
        ]),
    )
    monkeypatch.setattr(eval_repo, "finish_run", _finish)
    monkeypatch.setattr(
        eval_repo, "list_run_items", _async([_item_row("uk-a", "scored")])
    )
    install_state(_State(asr=asr))

    await advance_run(run_id, _claims(), authorization="Bearer t")

    assert json.loads(scored_calls[0]["flags"]) == ["known_hallucination"]
    summary = json.loads(finished["summary"])
    assert summary["overall"]["utterances"] == 1          # only uk-b counts
    assert summary["overall"]["wer"] == pytest.approx(0.1)
    assert summary["flagged"]["count"] == 1
    assert summary["flagged"]["items"][0]["script_id"] == "uk-a"
    # The whole reproducibility record travels inside the summary.
    assert summary["conditions"]["normalizer_version"] == eval_normalize.VERSION
    assert summary["conditions"]["corpus_sha256"] == "a" * 64


# ── CSV import ────────────────────────────────────────────────────────


def _import_body(csv_text: str, **over):
    body = {
        "filename": "corpus-v2-replicas.csv",
        "csv_base64": base64.b64encode(csv_text.encode()).decode(),
    }
    body.update(over)
    return ImportRequest(**body)


_CSV = (
    "id,lang,category,condition,set,text\n"
    "uk-num-008,uk,numbers,headset,dev,\"Метформін п'ятсот міліграмів.\"\n"
    "uk-num-009,uk,numbers,phone_noise,dev,\"Тиск сто тридцять п'ять.\"\n"
)


async def test_import_dry_run_writes_no_lines_but_is_still_journalled(monkeypatch):
    """"We previewed this and did not commit it" is a fact worth keeping."""
    inserted: list = []
    journalled: list = []
    monkeypatch.setattr(eval_repo, "existing_script_ids", _async(set()))
    monkeypatch.setattr(
        eval_repo, "insert_script_item_with_id", _record(inserted, _line_row())
    )
    monkeypatch.setattr(eval_repo, "coverage_by_category", _async([]))
    monkeypatch.setattr(
        eval_repo, "insert_import", _record(journalled, {"id": uuid4()})
    )
    state = _State()
    install_state(state)

    report = await import_csv(_import_body(_CSV), _claims())
    assert report.dry_run is True
    assert report.rows_added == 2
    assert inserted == []
    assert journalled[0]["dry_run"] is True
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_imported"


async def test_import_commit_inserts_and_skips_what_is_already_there(monkeypatch):
    """Idempotent by id: re-running a corrected file adds the new lines and
    leaves the recorded ones alone."""
    inserted: list = []
    monkeypatch.setattr(eval_repo, "existing_script_ids", _async({"uk-num-008"}))
    monkeypatch.setattr(
        eval_repo, "insert_script_item_with_id", _record(inserted, _line_row())
    )
    monkeypatch.setattr(eval_repo, "coverage_by_category", _async([]))
    monkeypatch.setattr(eval_repo, "insert_import", _async({"id": uuid4()}))
    monkeypatch.setattr(eval_repo, "upsert_registry_entry", _async({"id": uuid4()}))
    install_state(_State())

    report = await import_csv(_import_body(_CSV, dry_run=False), _claims())
    assert report.rows_added == 1
    assert report.rows_skipped == 1
    assert [c["script_id"] for c in inserted] == ["uk-num-009"]
    assert report.skipped[0].code == "already_exists"
    # The id comes from the file, which is what makes idempotency possible.
    assert inserted[0]["dataset"] == "dev"
    assert inserted[0]["subset"] == "numbers_doses_units"


async def test_import_refuses_the_holdout_without_an_explicit_confirmation(
    monkeypatch,
):
    csv_text = (
        "id,lang,category,condition,set,text\n"
        "uk-base-050,uk,base,headset,test,\"Пацієнт почувається краще.\"\n"
    )
    monkeypatch.setattr(eval_repo, "existing_script_ids", _async(set()))
    monkeypatch.setattr(eval_repo, "coverage_by_category", _async([]))
    monkeypatch.setattr(eval_repo, "insert_import", _async({"id": uuid4()}))
    monkeypatch.setattr(eval_repo, "upsert_registry_entry", _async({"id": uuid4()}))
    install_state(_State())

    report = await import_csv(_import_body(csv_text, dry_run=False), _claims())
    assert report.rows_added == 0
    assert report.rejected[0].code == "test_requires_confirmation"


async def test_import_rejects_a_file_that_is_not_the_section_6_format(monkeypatch):
    install_state(_State())
    with pytest.raises(HTTPException) as exc:
        await import_csv(_import_body("id,lang,text\nuk-a,uk,т\n"), _claims())
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "missing_columns"


# ── run comparison ────────────────────────────────────────────────────


async def test_compare_reports_a_paired_delta_with_an_interval(monkeypatch):
    snapshot_id = uuid4()
    a_id, b_id = uuid4(), uuid4()
    monkeypatch.setattr(
        eval_repo,
        "get_run",
        _fetch_by_id({
            a_id: _run_row(a_id, snapshot_id, status="complete"),
            b_id: _run_row(b_id, snapshot_id, status="complete"),
        }),
    )
    monkeypatch.setattr(
        eval_repo,
        "scored_items",
        _fetch_by_id({
            a_id: [_scored_row(f"u{i}", wer_norm=0.30) for i in range(8)],
            b_id: [_scored_row(f"u{i}", wer_norm=0.22) for i in range(8)],
        }, arg_index=1),
    )
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"scored": 8}))
    install_state(_State())

    dto = await compare_runs(_claims(), baseline=a_id, candidate=b_id)
    assert dto.metric == "wer_norm"
    assert dto.comparison["delta"] == pytest.approx(-0.08)
    assert dto.comparison["significant"] is True
    assert dto.comparison["utterances"] == 8


async def test_compare_excludes_quarantined_utterances_from_the_delta(monkeypatch):
    """A take that hallucinated in one run and not the other would otherwise
    dominate the delta with a recording problem."""
    snapshot_id = uuid4()
    a_id, b_id = uuid4(), uuid4()
    monkeypatch.setattr(
        eval_repo,
        "get_run",
        _fetch_by_id({
            a_id: _run_row(a_id, snapshot_id),
            b_id: _run_row(b_id, snapshot_id),
        }),
    )
    monkeypatch.setattr(
        eval_repo,
        "scored_items",
        _fetch_by_id({
            a_id: [
                _scored_row("u0", wer_norm=0.3),
                _scored_row("u1", wer_norm=1.9, flags=["wer_over_100"]),
            ],
            b_id: [
                _scored_row("u0", wer_norm=0.3),
                _scored_row("u1", wer_norm=0.2),
            ],
        }, arg_index=1),
    )
    monkeypatch.setattr(eval_repo, "count_by_status", _async({"scored": 2}))
    install_state(_State())

    dto = await compare_runs(_claims(), baseline=a_id, candidate=b_id)
    assert dto.comparison["utterances"] == 1
    assert dto.comparison["delta"] == 0.0


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("dataset", "dev", "dataset_mismatch"),
        # Any version OTHER than the one the run under test carries; "v2" is
        # the live rules version now (corpus-v3 Epic A), so it would match.
        ("normalizer_version", "v0", "normalizer_mismatch"),
    ],
)
async def test_compare_refuses_runs_that_are_not_comparable(
    monkeypatch, field, value, code
):
    """§7's honesty rules are the endpoint's purpose, not an obstacle to it."""
    snapshot_id = uuid4()
    a_id, b_id = uuid4(), uuid4()
    monkeypatch.setattr(
        eval_repo,
        "get_run",
        _fetch_by_id({
            a_id: _run_row(a_id, snapshot_id),
            b_id: _run_row(b_id, snapshot_id, **{field: value}),
        }),
    )
    install_state(_State())

    with pytest.raises(HTTPException) as exc:
        await compare_runs(_claims(), baseline=a_id, candidate=b_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == code


async def test_compare_refuses_two_different_snapshots(monkeypatch):
    """Different audio and different gold text: any difference between the
    numbers is unattributable."""
    a_id, b_id = uuid4(), uuid4()
    monkeypatch.setattr(
        eval_repo,
        "get_run",
        _fetch_by_id({
            a_id: _run_row(a_id, uuid4()),
            b_id: _run_row(b_id, uuid4()),
        }),
    )
    install_state(_State())

    with pytest.raises(HTTPException) as exc:
        await compare_runs(_claims(), baseline=a_id, candidate=b_id)
    assert exc.value.detail["error"] == "snapshot_mismatch"


def _fetch_by_id(table: dict, arg_index: int = 1):
    """A repo stub that answers per run id rather than with one value."""

    async def _fn(*args, **kwargs):
        return table[args[arg_index]]

    return _fn
