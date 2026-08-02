"""S11 step 07 §4.2 — the crypto-shred VERIFY battery, on live infra.

(a) two-person rule re-asserted; (b) destruction proven (objects gone,
rows gone, identity overwritten); (c) the in-window signed report
REMAINS with its envelope, named in retained[]; (d) direct-SQL proof
that no wrapped-DEK material of destroyed objects survives anywhere;
(e) the audit chain verifies end-to-end AFTER erasure.

Plus: idempotent mid-crash re-run, retention boundary (out-of-window
signed report destroyed WITH its envelope), grace refusal, advisory
lock.

Skipped unless ``RUN_DB_INTEGRATION=1`` (needs dev stack + MinIO +
dev master key).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
import pytest

from audit import AuditVerifier
from db import create_pool

from .test_dsar_engine import AUDIO_PLAINTEXT, TRANSCRIPT_JSON, _dev_master_key  # noqa: F401
from .test_fanout import (
    MARK,
    SU_DSN,
    build_fixture_patient_with_everything,
    cleanup_fixture,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs dev-up + migrate-up + seed + MinIO",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
AUDIT_READER_DSN = (
    f"postgresql://audit_reader:audit_reader@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)


async def _second_user(su: asyncpg.Connection, tenant_id: UUID, not_sub: UUID) -> UUID:
    sub = await su.fetchval(
        "SELECT sub FROM users WHERE tenant_id = $1 AND sub <> $2 LIMIT 1",
        tenant_id, not_sub,
    )
    if sub is None:
        pytest.skip("needs two users in the tenant (`make seed`)")
    return sub


async def _approved_erasure(
    su: asyncpg.Connection, tenant_id: UUID, patient_id: UUID,
    requester: UUID, reviewer: UUID, *, scheduled_for: datetime | None = None,
) -> UUID:
    return await su.fetchval(
        """
        INSERT INTO patient_privacy_requests
            (tenant_id, patient_id, kind, reason, status,
             requested_by, reviewed_by, reviewed_at, scheduled_for)
        VALUES ($1, $2, 'erasure', $3, 'approved', $4, $5, now(), $6)
        RETURNING id
        """,
        tenant_id, patient_id, MARK, requester, reviewer,
        scheduled_for or (datetime.now(UTC) - timedelta(hours=1)),
    )


async def _plant_real_objects(runtime, su, tenant_id: UUID, ids: dict) -> str:
    """Real ciphertext objects + REAL wrapped-DEK metadata on the audio row.
    Returns a base64 fragment of the audio object's wrapped DEK."""
    from storage.object_store import header_metadata_for_row

    header = await runtime.ctx.audio_store.put(
        key=f"{tenant_id}/{MARK}.enc",
        plaintext=AUDIO_PLAINTEXT,
        tenant_id=tenant_id,
        aad=ids["audio"].bytes,
    )
    meta = header_metadata_for_row(header)
    await su.execute(
        "UPDATE audio_files SET envelope_metadata = $2::jsonb WHERE id = $1",
        ids["audio"], json.dumps(meta),
    )
    await runtime.ctx.transcript_store.put(
        key=f"{tenant_id}/{MARK}.json.enc",
        plaintext=json.dumps(TRANSCRIPT_JSON, ensure_ascii=False).encode(),
        tenant_id=tenant_id,
        aad=ids["job"].bytes,
    )
    return base64.b64encode(header.wrapped_dek).decode()[:24]


async def _cleanup_erased(su: asyncpg.Connection, patient_id: UUID | None) -> None:
    await cleanup_fixture(su)
    if patient_id is None:
        return
    await su.execute(
        "DELETE FROM patient_privacy_requests WHERE patient_id = $1", patient_id
    )
    await su.execute(
        "UPDATE patient_consents SET signed_envelope_id = NULL WHERE patient_id = $1",
        patient_id,
    )
    await su.execute(
        "DELETE FROM signed_envelopes WHERE resource_id IN "
        "(SELECT id FROM patient_consents WHERE patient_id = $1) "
        "OR resource_id IN (SELECT id FROM reports WHERE patient_id = $1)",
        patient_id,
    )
    await su.execute("DELETE FROM patient_consents WHERE patient_id = $1", patient_id)
    await su.execute(
        "UPDATE reports SET current_version_id = NULL WHERE patient_id = $1", patient_id
    )
    await su.execute(
        "DELETE FROM report_versions WHERE report_id IN "
        "(SELECT id FROM reports WHERE patient_id = $1)", patient_id,
    )
    await su.execute("DELETE FROM reports WHERE patient_id = $1", patient_id)
    await su.execute("DELETE FROM patients WHERE id = $1", patient_id)


@pytest.fixture
async def runtime(_dev_master_key):  # noqa: F811
    from core_service.erasure.engine import ErasureRuntime

    rt = await ErasureRuntime.build()
    yield rt
    await rt.app_pool.close()
    await rt.erasure_pool.close()


async def test_battery_a_to_e(runtime) -> None:
    from core_service.erasure.engine import execute_erasure

    su = await asyncpg.connect(SU_DSN)
    audit_pool = await create_pool(
        AUDIT_READER_DSN, application_name="itest-verify", min_size=1, max_size=1
    )
    patient_id = None
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, ids = await build_fixture_patient_with_everything(su)
        dek_fragment = await _plant_real_objects(runtime, su, tenant_id, ids)
        reviewer = await _second_user(su, tenant_id, ids["user"])

        # ── (a) two-person rule holds at the DB layer ─────────────────
        with pytest.raises(asyncpg.CheckViolationError):
            await su.execute(
                "INSERT INTO patient_privacy_requests (tenant_id, patient_id, kind, "
                "reason, status, requested_by, reviewed_by, reviewed_at, scheduled_for) "
                "VALUES ($1, $2, 'erasure', $3, 'approved', $4, $4, now(), now())",
                tenant_id, patient_id, MARK, ids["user"],
            )
        print("\n(a) two-person: self-approval violates privacy_two_person ✓")

        request_id = await _approved_erasure(
            su, tenant_id, patient_id, ids["user"], reviewer
        )
        report = await execute_erasure(
            runtime, tenant_id=tenant_id, request_id=request_id, operator="battery"
        )

        # ── (b) destruction proven ────────────────────────────────────
        for table, ref in [
            ("audio_files", ids["audio"]),
            ("transcription_jobs", ids["job"]),
            ("reports", ids["draft_report"]),
            ("encounters", ids["encounter"]),
        ]:
            count = await su.fetchval(
                f"SELECT count(*) FROM {table} WHERE id = $1", ref
            )
            assert count == 0, f"{table} row survived"
        assert await su.fetchval(
            "SELECT count(*) FROM clinical_notes WHERE patient_id = $1", patient_id
        ) == 0
        assert await su.fetchval(
            "SELECT count(*) FROM patient_anamnesis WHERE patient_id = $1", patient_id
        ) == 0
        assert await su.fetchval(
            "SELECT count(*) FROM dictation_sessions WHERE audio_file_id = $1",
            ids["audio"],
        ) == 0
        assert await su.fetchval(
            "SELECT count(*) FROM signing_sessions WHERE resource_id = $1",
            ids["consent_signed"],
        ) == 0
        with pytest.raises(Exception):  # noqa: B017 — any fetch error proves absence
            await runtime.ctx.audio_store.get(
                key=f"{tenant_id}/{MARK}.enc", tenant_id=tenant_id, aad=ids["audio"].bytes
            )
        with pytest.raises(Exception):  # noqa: B017 — any fetch error proves absence
            await runtime.ctx.transcript_store.get(
                key=f"{tenant_id}/{MARK}.json.enc", tenant_id=tenant_id, aad=ids["job"].bytes
            )
        patient = await su.fetchrow("SELECT * FROM patients WHERE id = $1", patient_id)
        assert patient["status"] == "erased" and patient["erased_at"] is not None
        assert patient["name_uk"] == "ERASED" and patient["name_en"] == "ERASED"
        assert patient["ipn_hmac"] is None and patient["ipn_encrypted"] is None
        assert patient["dob"] is None and patient["mrn"] == "" and patient["tags"] == []
        # Contact details (0060) are identity too — the tombstone clears the
        # phone, the e-mail, and every address component.
        assert patient["phone"] == "" and patient["email"] == ""
        assert all(
            patient[c] == ""
            for c in (
                "address_street",
                "address_house",
                "address_zip",
                "address_city",
                "address_country",
            )
        )
        print("(b) destruction: objects gone, rows gone, identity overwritten ✓")

        # ── (c) in-window signed report retained WITH its envelope ────
        assert await su.fetchval(
            "SELECT count(*) FROM reports WHERE id = $1", ids["signed_report"]
        ) == 1
        assert await su.fetchval(
            "SELECT count(*) FROM signed_envelopes WHERE resource_id = $1",
            ids["signed_report"],
        ) == 1
        retained = {(r["kind"], r["id"]): r["legal_basis"] for r in report["retained"]}
        assert retained[("report", str(ids["signed_report"]))] == "retention:clinical_record_signed"
        assert retained[("consent", str(ids["consent_signed"]))] == "retention:consent_record"
        assert any(k == "privacy_request" for k, _ in retained)
        assert any(k == "signed_envelope" for k, _ in retained)
        print("(c) retention: signed report + envelope + consents named with bases ✓")

        # ── (d) crypto-shred proof: no wrapped-DEK material survives ──
        dek_hits = await su.fetchval(
            "SELECT count(*) FROM audio_files WHERE envelope_metadata::text LIKE $1",
            f"%{dek_fragment}%",
        )
        assert dek_hits == 0
        print(f"(d) crypto-shred: wrapped-DEK fragment {dek_fragment!r} in 0 rows; objects 404 ✓")

        # ── (e) audit chain verifies end-to-end AFTER erasure ─────────
        verifier = AuditVerifier(audit_pool)
        chain = await verifier.verify_chain(tenant_id)
        assert chain.ok, chain
        executed = await su.fetchval(
            "SELECT count(*) FROM audit.events WHERE kind = 'erasure.executed' "
            "AND payload_jcs::text LIKE $1", f"%{request_id}%",
        )
        assert executed == 1
        audio_deleted = await su.fetchval(
            "SELECT count(*) FROM audit.events WHERE kind = 'asr.audio_deleted' "
            "AND target_id = $1", str(ids["audio"]),
        )
        assert audio_deleted >= 1
        print(f"(e) audit chain ok=True through seq {chain.last_seq}; "
              f"exactly 1 erasure.executed; asr.audio_deleted emitted ✓")

        row = await su.fetchrow(
            "SELECT status, report_of_execution FROM patient_privacy_requests WHERE id = $1",
            request_id,
        )
        assert row["status"] == "completed"
        recorded = json.loads(row["report_of_execution"])
        assert recorded["counts"]["destroyed"] == len(report["destroyed"]) > 0
    finally:
        await _cleanup_erased(su, patient_id)
        await audit_pool.close()
        await su.close()


async def test_idempotent_rerun_after_mid_crash(runtime, monkeypatch) -> None:
    from core_service.erasure import engine as engine_mod
    from core_service.erasure.erasers import ERASERS_IN_ORDER, erase_recordings

    su = await asyncpg.connect(SU_DSN)
    patient_id = None
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, ids = await build_fixture_patient_with_everything(su)
        await _plant_real_objects(runtime, su, tenant_id, ids)
        reviewer = await _second_user(su, tenant_id, ids["user"])
        request_id = await _approved_erasure(
            su, tenant_id, patient_id, ids["user"], reviewer
        )

        # Crash hook: die right AFTER the recordings eraser (object already
        # deleted, later artifacts untouched).
        async def _boom(conn, pid, ctx):  # noqa: ANN001
            raise RuntimeError("simulated crash after object deletion")

        crash_order = []
        for eraser in ERASERS_IN_ORDER:
            crash_order.append(eraser)
            if eraser is erase_recordings:
                crash_order.append(_boom)
        monkeypatch.setattr(engine_mod, "ERASERS_IN_ORDER", tuple(crash_order))

        with pytest.raises(RuntimeError, match="simulated crash"):
            await engine_mod.execute_erasure(
                runtime, tenant_id=tenant_id, request_id=request_id, operator="crash-test"
            )
        row = await su.fetchrow(
            "SELECT status, last_error FROM patient_privacy_requests WHERE id = $1",
            request_id,
        )
        assert row["status"] == "executing"
        assert "simulated crash" in row["last_error"]

        # Re-run with the real order → completes; exactly one executed event.
        monkeypatch.setattr(engine_mod, "ERASERS_IN_ORDER", ERASERS_IN_ORDER)
        report = await engine_mod.execute_erasure(
            runtime, tenant_id=tenant_id, request_id=request_id, operator="crash-test"
        )
        final = await su.fetchrow(
            "SELECT status, last_error FROM patient_privacy_requests WHERE id = $1",
            request_id,
        )
        assert final["status"] == "completed" and final["last_error"] is None
        assert await su.fetchval(
            "SELECT status FROM patients WHERE id = $1", patient_id
        ) == "erased"
        executed = await su.fetchval(
            "SELECT count(*) FROM audit.events WHERE kind = 'erasure.executed' "
            "AND payload_jcs::text LIKE $1", f"%{request_id}%",
        )
        assert executed == 1
        assert report["counts"]["destroyed"] > 0
        print("\nidempotency: crash after object-delete → re-run completed; "
              "1× erasure.executed ✓")
    finally:
        await _cleanup_erased(su, patient_id)
        await su.close()


async def test_retention_boundary_out_of_window_destroyed(runtime) -> None:
    from core_service.erasure.engine import execute_erasure

    su = await asyncpg.connect(SU_DSN)
    patient_id = None
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, ids = await build_fixture_patient_with_everything(su)
        await _plant_real_objects(runtime, su, tenant_id, ids)
        # Backdate the signed report OUTSIDE the retention window.
        await su.execute(
            "UPDATE reports SET signed_at = now() - interval '26 years', "
            "finalized_at = now() - interval '26 years' WHERE id = $1",
            ids["signed_report"],
        )
        reviewer = await _second_user(su, tenant_id, ids["user"])
        request_id = await _approved_erasure(
            su, tenant_id, patient_id, ids["user"], reviewer
        )
        report = await execute_erasure(
            runtime, tenant_id=tenant_id, request_id=request_id, operator="boundary"
        )

        assert await su.fetchval(
            "SELECT count(*) FROM reports WHERE id = $1", ids["signed_report"]
        ) == 0
        assert await su.fetchval(
            "SELECT count(*) FROM signed_envelopes WHERE resource_id = $1",
            ids["signed_report"],
        ) == 0
        destroyed_kinds = {(d["kind"], d["id"]) for d in report["destroyed"]}
        assert ("report", str(ids["signed_report"])) in destroyed_kinds
        assert any(k == "signed_envelope" for k, _ in destroyed_kinds)
        # No report retention entries; consents still retained.
        assert not any(r["kind"] == "report" for r in report["retained"])
        print("\nretention boundary: 26-year-old signed report destroyed "
              "WITH its envelope ✓")
    finally:
        await _cleanup_erased(su, patient_id)
        await su.close()


async def test_grace_period_refused(runtime) -> None:
    from core_service.erasure.engine import ErasureRefusedError, execute_erasure

    su = await asyncpg.connect(SU_DSN)
    patient_id = None
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, ids = await build_fixture_patient_with_everything(su)
        reviewer = await _second_user(su, tenant_id, ids["user"])
        request_id = await _approved_erasure(
            su, tenant_id, patient_id, ids["user"], reviewer,
            scheduled_for=datetime.now(UTC) + timedelta(days=6),
        )
        with pytest.raises(ErasureRefusedError) as exc_info:
            await execute_erasure(
                runtime, tenant_id=tenant_id, request_id=request_id, operator="early"
            )
        assert exc_info.value.code == "grace_period_active"
        assert await su.fetchval(
            "SELECT status FROM patients WHERE id = $1", patient_id
        ) != "erased"
    finally:
        await _cleanup_erased(su, patient_id)
        await su.close()


async def test_advisory_lock_prevents_double_run(runtime) -> None:
    from core_service.erasure.engine import ErasureRefusedError, execute_erasure

    su = await asyncpg.connect(SU_DSN)
    patient_id = None
    holder = await asyncpg.connect(SU_DSN)
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, ids = await build_fixture_patient_with_everything(su)
        reviewer = await _second_user(su, tenant_id, ids["user"])
        request_id = await _approved_erasure(
            su, tenant_id, patient_id, ids["user"], reviewer
        )
        # Another session holds the lock (simulating a concurrent run).
        assert await holder.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1::text, 0))",
            str(request_id),
        )
        with pytest.raises(ErasureRefusedError) as exc_info:
            await execute_erasure(
                runtime, tenant_id=tenant_id, request_id=request_id, operator="dup"
            )
        assert exc_info.value.code == "already_running"
        await holder.fetchval(
            "SELECT pg_advisory_unlock(hashtextextended($1::text, 0))", str(request_id)
        )
        # Lock released → the run proceeds.
        report = await execute_erasure(
            runtime, tenant_id=tenant_id, request_id=request_id, operator="dup"
        )
        assert report["counts"]["destroyed"] > 0
        print("\nadvisory lock: concurrent holder → already_running; "
              "after release → executed ✓")
    finally:
        await _cleanup_erased(su, patient_id)
        await holder.close()
        await su.close()
