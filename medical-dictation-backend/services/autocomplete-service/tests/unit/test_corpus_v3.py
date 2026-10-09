"""Corpus-v3 epics C–F, stated as their own acceptance criteria.

Each test here is one line from the spec turned into an assertion. The
repository is stubbed, as in the other pipeline tests: what is under test is
the decision — what is refused, what is composed, what falls out of an
average — not the SQL, which is integration territory.
"""

from __future__ import annotations

import base64
import struct
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from autocomplete_service import eval_instructions, eval_register, eval_stats
from autocomplete_service import eval_repository as eval_repo
from autocomplete_service.deps import install_state
from autocomplete_service.routers import compliance, corpus_eval
from autocomplete_service.routers.corpus_eval import (
    FlagTakeRequest,
    SaveTakeRequest,
    replica_detail,
    retake_queue,
    save_take,
)
from fastapi import HTTPException

from auth import Claims

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _claims() -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=["clinician"], scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


def _wav(seconds: float = 1.5) -> bytes:
    data = b"\x00" * (int(16_000 * seconds) * 2)
    fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


class _Conn:
    """Answers the handful of raw queries that are not stubbed."""

    def __init__(self, *, consented: bool = True) -> None:
        self.consented = consented

    async def execute(self, sql: str, *args):
        return ""

    async def fetch(self, sql: str, *args):
        return []

    async def fetchrow(self, sql: str, *args):
        if "corpus_speaker_consents" in sql:
            return {"consent": 1} if self.consented else None
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


class _State:
    def __init__(self, conn) -> None:
        self.app_pool = _Pool(conn)
        self.audit_writer = _Audit()

        class _RL:
            async def check(self, *, user_id):
                return (True, 0)

        self.phrase_rate_limiter = _RL()


def _async(value):
    async def _call(*a, **kw):
        return value

    return _call


# ══ Epic C — the instruction lives on the replica ══════════════════════


TEMPLATES = [
    {
        "condition": "phone-speaker-distance",
        "category": None,
        "text": (
            "Запис із телефона на відстані близько метра. Увімкніть фоновий "
            "шум (радіо або розмова). Телефон до рота не підносити."
        ),
    },
    {"condition": "headset", "category": None, "text": "Запис у гарнітурі."},
    {
        "condition": None,
        "category": "numbers_doses_units",
        "text": "Числа промовляйте чітко, без пауз усередині дози.",
    },
    {"condition": None, "category": "baseline", "text": "Читайте спокійно."},
]


def test_instruction_is_the_condition_sentence_plus_the_category_one():
    by_condition, by_category = eval_instructions.index(TEMPLATES)
    out = eval_instructions.compose(
        condition="phone-speaker-distance",
        subset="numbers_doses_units",
        by_condition=by_condition,
        by_category=by_category,
    )
    assert out.text.startswith("Запис із телефона")
    assert out.text.endswith("без пауз усередині дози.")
    # Both halves stay separable — the console styles the phone+noise one
    # differently so it is not skimmed past.
    assert "Числа" not in out.condition_text
    assert "телефона" not in out.category_text


def test_a_replica_with_no_condition_gets_no_invented_one():
    """Null means nobody decided how to record this line. Filling in
    "headset" here would turn an open question into a silent default."""
    by_condition, by_category = eval_instructions.index(TEMPLATES)
    out = eval_instructions.compose(
        condition=None, subset=None, by_condition=by_condition, by_category=by_category
    )
    assert out.condition_text == ""
    assert out.text == "Читайте спокійно."


async def test_replica_detail_serves_the_assembled_instruction(monkeypatch):
    """Epic C's acceptance criterion: recording uk-num-010 (phone_noise),
    the user sees the phone+noise instruction BEFORE pressing space."""
    monkeypatch.setattr(
        eval_repo,
        "get_script_item",
        _async(
            {
                "script_id": "uk-num-010",
                "language": "uk",
                "specialty": "cardiology",
                "subset": "numbers_doses_units",
                "say": "пульс шістдесят вісім за хвилину",
                "transcript": "пульс шістдесят вісім за хвилину",
                "condition": "phone-speaker-distance",
                "source": "authored",
                "dataset": "dev",
                "paired": True,
            }
        ),
    )
    monkeypatch.setattr(eval_repo, "instruction_templates", _async(TEMPLATES))
    monkeypatch.setattr(
        eval_repo,
        "list_takes",
        _async([{"script_id": "uk-num-010", "condition": "headset"}]),
    )
    install_state(_State(_Conn()))

    dto = await replica_detail("uk-num-010", _claims())
    assert dto.condition == "phone-speaker-distance"
    assert "телефона" in dto.recording_instructions
    assert "фоновий шум" in dto.recording_instructions
    assert "без пауз усередині дози" in dto.recording_instructions
    assert dto.condition_confirmed_required is True
    # Paired and only one condition on file so far — the queue renders 1/2.
    assert dto.paired is True
    assert dto.conditions_recorded == ["headset"]


async def test_a_take_without_a_confirmed_condition_is_not_recorded(monkeypatch):
    """Epic C: "дубль, записаний без підтвердження умови, не отримує статус
    «записано»"."""
    monkeypatch.setattr(eval_repo, "get_script_item", _async(None))
    install_state(_State(_Conn()))
    with pytest.raises(HTTPException) as exc:
        await save_take(
            "uk-cardiology-101",
            SaveTakeRequest(
                condition="headset",
                audio_wav_base64=base64.b64encode(_wav()).decode(),
                condition_confirmed=False,
            ),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "condition_not_confirmed"


async def test_an_overridden_condition_is_journalled_as_a_mismatch(monkeypatch):
    """The override is allowed; being unable to find it afterwards is not."""
    attempts: list[dict] = []

    async def _attempt(conn, **kw):
        attempts.append(kw)

    monkeypatch.setattr(eval_repo, "get_script_item", _async(None))
    monkeypatch.setattr(eval_repo, "insert_take_attempt", _attempt)
    monkeypatch.setattr(
        eval_repo,
        "upsert_take",
        _async(
            {
                "id": uuid4(),
                "script_id": "uk-noisy-001",
                "script_version": "v2",
                "recorded_by": uuid4(),
                "language": "uk",
                "specialty": "general",
                "subset": "phone_mic_noisy",
                "condition": "headset",
                "condition_confirmed": True,
                "duration_ms": 1500,
                "audio_sha256": "a" * 64,
                "size_bytes": 100,
                "flagged_bad": False,
                "flagged_note": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        ),
    )
    state = _State(_Conn())
    install_state(state)

    # uk-noisy-001 is a vendored line asking for phone-speaker-distance.
    await save_take(
        "uk-noisy-001",
        SaveTakeRequest(
            condition="headset",
            audio_wav_base64=base64.b64encode(_wav()).decode(),
            condition_confirmed=True,
        ),
        _claims(),
    )
    saved = [a for a in attempts if a["status"] == "saved"]
    assert saved and saved[0]["expected_condition"] == "phone-speaker-distance"
    assert saved[0]["condition"] == "headset"
    assert state.audit_writer.events[0]["payload"]["condition_mismatch"] is True


# ══ Epic D — paired conditions ═════════════════════════════════════════


def _scored(script_id, condition, wer, *, paired=True):
    return {
        "script_id": script_id,
        "condition": condition,
        "paired": paired,
        "wer": wer,
        "cer": wer / 2,
        "ref_words": 10,
        "ref_chars": 40,
        "wer_norm": wer,
        "cer_norm": wer / 2,
        "ref_words_norm": 10,
        "ref_chars_norm": 40,
        "dose_tokens": 0,
        "dose_exact": True,
        "flags": [],
        "hypothesis": "h",
        "subset": "numbers_doses_units",
        "language": "uk",
    }


def test_paired_table_compares_the_same_texts_and_nothing_else():
    """Epic D: непарні дублі в цю таблицю не потрапляють."""
    items = [
        _scored("a", "headset", 0.10),
        _scored("a", "phone-speaker-distance", 0.30),
        _scored("b", "headset", 0.05),
        _scored("b", "phone-speaker-distance", 0.25),
        # An unpaired line recorded in one condition — must not appear.
        _scored("c", "noisy", 0.90, paired=False),
    ]
    out = eval_stats.paired_conditions(items)
    assert out["conditions"] == {"headset": 2, "phone-speaker-distance": 2}
    assert len(out["comparisons"]) == 1
    row = out["comparisons"][0]
    assert row["baseline_condition"] == "headset"
    assert row["candidate_condition"] == "phone-speaker-distance"
    assert row["utterances"] == 2
    assert row["delta"] > 0  # the phone really is worse on the same texts
    assert row["delta_ci"] is not None


def test_a_paired_line_recorded_in_only_one_condition_contributes_nothing():
    """Half a pair is not a comparison — counting it would reintroduce the
    very bias the paired design removes."""
    out = eval_stats.paired_conditions(
        [_scored("a", "headset", 0.1), _scored("b", "phone-speaker-distance", 0.4)]
    )
    assert out["comparisons"] == []


def test_the_summary_says_the_plain_condition_table_is_not_comparable():
    summary = eval_stats.summarize([_scored("a", "headset", 0.1, paired=False)])
    assert summary["by_condition_comparable"] is False
    assert "paired_conditions" in summary


# ══ Epic E — the retake queue ══════════════════════════════════════════


def _candidate(script_id, **over):
    row = {
        "script_id": script_id,
        "condition": "headset",
        "updated_at": NOW,
        "duration_ms": 1500,
        "flagged_bad": False,
        "flagged_note": None,
        "condition_mismatch": False,
        "run_flags": [],
        "run_started_at": None,
    }
    row.update(over)
    return row


async def test_retake_queue_reports_every_reason_and_counts_them(monkeypatch):
    monkeypatch.setattr(
        eval_repo,
        "retake_candidates",
        _async(
            [
                _candidate(
                    "uk-general-a001",
                    run_flags=["mostly_silence"],
                    run_started_at=NOW + timedelta(hours=1),
                ),
                _candidate("uk-num-010", condition_mismatch=True),
                _candidate("uk-drugs-002", flagged_bad=True, flagged_note="заїкнувся"),
            ]
        ),
    )
    install_state(_State(_Conn()))
    out = await retake_queue(_claims())

    assert out.total == 3
    by_id = {i.script_id: i for i in out.items}
    # The acceptance criterion names this one: a silent take is on the list.
    assert by_id["uk-general-a001"].reasons == ["mostly_silence"]
    assert by_id["uk-num-010"].reasons == ["condition_mismatch"]
    assert by_id["uk-drugs-002"].reasons == ["manual_bad"]
    assert by_id["uk-drugs-002"].note == "заїкнувся"
    assert out.by_reason["mostly_silence"] == 1


async def test_a_re_recorded_line_leaves_the_queue(monkeypatch):
    """The queue is derived, so nothing has to be cleared: ``retake_candidates``
    only returns run flags NEWER than the take's audio, and re-recording
    moves the audio forward."""
    monkeypatch.setattr(eval_repo, "retake_candidates", _async([]))
    install_state(_State(_Conn()))
    out = await retake_queue(_claims())
    assert out.total == 0 and out.by_reason == {}


async def test_flagging_a_take_that_does_not_exist_is_a_404(monkeypatch):
    monkeypatch.setattr(eval_repo, "flag_take", _async(None))
    install_state(_State(_Conn()))
    with pytest.raises(HTTPException) as exc:
        await corpus_eval.flag_take(
            "uk-general-a001", FlagTakeRequest(condition="noisy"), _claims()
        )
    assert exc.value.status_code == 404


# ══ Epic F — the data register ═════════════════════════════════════════


async def test_a_take_is_refused_without_a_live_speaker_consent(monkeypatch):
    """A voice is personal data whatever the script says, and the refusal
    happens before the bytes are decoded — unconsented audio never exists
    server-side even briefly."""
    monkeypatch.setattr(eval_repo, "get_script_item", _async(None))
    install_state(_State(_Conn(consented=False)))
    with pytest.raises(HTTPException) as exc:
        await save_take(
            "uk-cardiology-101",
            SaveTakeRequest(
                condition="headset",
                audio_wav_base64=base64.b64encode(_wav()).decode(),
                condition_confirmed=True,
            ),
            _claims(),
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "speaker_consent_required"


def test_a_snapshot_registers_itself_as_personal_but_never_patient_data():
    entry = eval_register.snapshot_entry(
        version=3,
        manifest_sha256="b" * 64,
        snapshot_id=uuid4(),
        utterances=42,
        speakers=[uuid4(), uuid4()],
    )
    assert entry["version"] == "snapshot-v3"
    assert entry["data_origin"] == "synthetic_scripted"
    # The two adjacent fields people conflate.
    assert entry["contains_personal_data"] is True
    assert "contains_patient_data" not in entry  # the column's CHECK owns it
    assert entry["frozen"] is True


def test_a_csv_import_carries_no_voices():
    entry = eval_register.import_entry(
        filename="v3.csv", file_sha256="c" * 64, import_id=uuid4(), rows_added=86
    )
    assert entry["contains_personal_data"] is False
    assert entry["speakers"] == []
    assert entry["utterances"] == 86


def test_the_document_states_the_policy_and_the_caveat():
    """An auditor reading one page must see both: that the corpus contains
    no patient data, and that this is not a legal opinion."""
    document = eval_register.render_html(
        entries=[
            {
                "name": "Клінічний корпус виміру WER",
                "version": "snapshot-v1",
                "sha256": "d" * 64,
                "purpose": "Вимірювання якості розпізнавання.",
                "data_origin": "synthetic_scripted",
                "contains_patient_data": False,
                "contains_personal_data": True,
                "retention_period": "поки корпус лишається базою виміру",
                "frozen": True,
            }
        ],
        consents=[
            {
                "speaker_id": uuid4(),
                "scope": "corpus_voice",
                "granted_at": NOW,
                "revoked_at": None,
                "takes_recorded": 12,
            }
        ],
        generated_at=NOW,
        tenant_id=uuid4(),
    )
    assert "Дані пацієнтів заборонені" in document
    assert "не юридичний висновок" in document
    assert "snapshot-v1" in document
    assert "чинна" in document


async def test_pdf_export_refuses_clearly_instead_of_shipping_broken_bytes(monkeypatch):
    """A file that will not open is worse than a stated refusal — and the
    HTML export carries the same content."""
    def _boom(document: str) -> bytes:
        raise eval_register.PdfUnavailableError("libpango not found")

    monkeypatch.setattr(eval_register, "render_pdf", _boom)
    monkeypatch.setattr(eval_repo, "list_registry", _async([]))
    monkeypatch.setattr(eval_repo, "list_consents", _async([]))
    install_state(_State(_Conn()))

    with pytest.raises(HTTPException) as exc:
        await compliance.export_pdf(_claims())
    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "pdf_renderer_unavailable"
    assert exc.value.detail["alternative"].endswith("export.html")
