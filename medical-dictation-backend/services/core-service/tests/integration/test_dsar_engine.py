"""S11 step 06 §8 integration — the DSAR engine, end to end on live infra.

Real Postgres + real MinIO + real envelope crypto (dev master key): build
the package for the everything-fixture and prove completeness (manifest
kinds vs the fan-out's exportable kinds), integrity (per-file sha256),
policy (raw audio absent by default / present with the flag; audit slice
filtered), and recovery (stale executing → takeover → exactly one final
object).

Skipped unless ``RUN_DB_INTEGRATION=1``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import asyncpg
import pytest

from audit import AuditWriter, Severity
from db import create_pool, tenant_connection

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
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
AUDIT_WRITER_DSN = (
    f"postgresql://audit_writer:audit_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)

AUDIO_PLAINTEXT = b"RIFF-fake-audio-bytes-" + b"\x01" * 64
TRANSCRIPT_JSON = {"segments": [{"text": "Пацієнт скаржиться на кашель."}]}


@pytest.fixture
def _dev_master_key(monkeypatch: pytest.MonkeyPatch):
    from core_service.config import settings

    key = Path("infra/dev/master.key")
    if not key.is_file():
        pytest.skip("needs infra/dev/master.key (make dev-up provisions it)")
    monkeypatch.setattr(settings, "master_key_path", str(key))
    return settings


async def _make_state(app_pool, audit_pool) -> SimpleNamespace:
    return SimpleNamespace(
        app_pool=app_pool,
        audit_writer=AuditWriter(audit_pool),
        dsar_runtime=None,
    )


async def _plant_objects(runtime, su, tenant_id: UUID, ids: dict) -> None:
    """Real ciphertext objects at the fixture's storage_uri keys."""
    await runtime.audio_store.put(
        key=f"{tenant_id}/{MARK}.enc",
        plaintext=AUDIO_PLAINTEXT,
        tenant_id=tenant_id,
        aad=ids["audio"].bytes,
    )
    await runtime.transcript_store.put(
        key=f"{tenant_id}/{MARK}.json.enc",
        plaintext=json.dumps(TRANSCRIPT_JSON, ensure_ascii=False).encode(),
        tenant_id=tenant_id,
        aad=ids["job"].bytes,
    )


async def test_dsar_engine_end_to_end(
    _dev_master_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import privacy_repository
    from core_service.erasure import dsar as engine
    from core_service.erasure.fanout import FANOUT

    su = await asyncpg.connect(SU_DSN)
    app_pool = await create_pool(APP_DSN, application_name="itest", min_size=1, max_size=2)
    audit_pool = await create_pool(
        AUDIT_WRITER_DSN, application_name="itest-audit", min_size=1, max_size=1
    )
    state = await _make_state(app_pool, audit_pool)
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, ids = await build_fixture_patient_with_everything(su)
        runtime = await engine.get_runtime(state)
        await _plant_objects(runtime, su, tenant_id, ids)

        # Plant audit events: one subject-accessible, one operator-internal.
        writer = AuditWriter(audit_pool)
        await writer.write_event(
            tenant_id=tenant_id, kind="consent.granted", actor_sub=ids["user"],
            target_kind="patient", target_id=str(ids["consent_signed"]),
            payload={"consent_id": str(ids["consent_signed"])}, severity=Severity.INFO,
        )
        await writer.write_event(
            tenant_id=tenant_id, kind="authz.denied", actor_sub=ids["user"],
            target_kind="patient", target_id=str(patient_id),
            payload={"action": "patient.read"}, severity=Severity.SEC,
        )

        request_id = await su.fetchval(
            "INSERT INTO patient_privacy_requests (tenant_id, patient_id, kind, "
            "reason, status, requested_by, executing_at) "
            "VALUES ($1, $2, 'dsar', $3, 'executing', $4, now()) RETURNING id",
            tenant_id, patient_id, MARK, ids["user"],
        )

        await engine.run_export(
            state, tenant_id=tenant_id, request_id=request_id,
            patient_id=patient_id, actor_sub=ids["user"],
        )

        row = await su.fetchrow(
            "SELECT status, package_object_key, report_of_execution "
            "FROM patient_privacy_requests WHERE id = $1", request_id,
        )
        assert row["status"] == "completed", row
        key = row["package_object_key"]
        assert key == f"dsar/{tenant_id}/{request_id}.zip"

        # The ZIP decrypts through the envelope path; hashes verify.
        zip_bytes = await runtime.dsar_store.get(
            key=key, tenant_id=tenant_id, aad=request_id.bytes
        )
        summary = json.loads(row["report_of_execution"])
        assert hashlib.sha256(zip_bytes).hexdigest() == summary["package_sha256"]

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        manifest = json.loads(zf.read("manifest.json"))
        by_path = {i["path"]: i for i in manifest["items"]}
        for item in manifest["items"]:
            assert hashlib.sha256(zf.read(item["path"])).hexdigest() == item["sha256"], item

        # Completeness: every exportable fan-out kind with fixture presence
        # appears in the manifest.
        manifest_kinds = {i["kind"] for i in manifest["items"]}
        expected_kinds = {
            a.kind for a in FANOUT
            if a.exportable and manifest["inventory_counts"].get(a.kind, 0) > 0
        }
        assert expected_kinds <= manifest_kinds, expected_kinds - manifest_kinds

        # Amendment history complete: version 1 + amendment version 2
        # (current) of the signed report.
        rid = str(ids["signed_report"])
        assert f"reports/{rid}/current.json" in by_path
        assert f"reports/{rid}/versions/1.json" in by_path
        current = json.loads(zf.read(f"reports/{rid}/current.json"))
        assert current["is_amendment"] is True and current["version_number"] == 2

        # Signed consent carries its verification token.
        consent = json.loads(zf.read(f"consents/{ids['consent_signed']}.json"))
        assert consent["signature"]["verification_token"]
        assert consent["signature"]["verify_path"].startswith("/verify/")

        # Transcripts: streaming (row) + batch (decrypted object).
        batch_txt = zf.read(f"transcripts/{ids['job']}.txt").decode()
        assert "кашель" in batch_txt

        # Raw audio ABSENT by default — named in exclusions, not silent.
        assert not any(n.endswith(".wav") for n in zf.namelist())
        assert any(e["kind"] == "recording_audio" for e in manifest["excluded"])

        # Audit slice: lifecycle kind in, operator-internal kind out.
        audit_slice = json.loads(zf.read("audit/subject-accessible.json"))
        kinds = {e["kind"] for e in audit_slice}
        assert "consent.granted" in kinds
        assert "authz.denied" not in kinds

        # README present, bilingual.
        readme = zf.read("README.txt").decode()
        assert "Пакет даних пацієнта" in readme and "Patient data package" in readme

        # ── Flag flip: raw audio included ────────────────────────────
        from core_service.config import settings

        monkeypatch.setattr(settings, "dsar_include_raw_audio", True)
        await su.execute(
            "UPDATE patient_privacy_requests SET status = 'executing', "
            "executing_at = now() WHERE id = $1", request_id,
        )
        await engine.run_export(
            state, tenant_id=tenant_id, request_id=request_id,
            patient_id=patient_id, actor_sub=ids["user"],
        )
        zip_bytes2 = await runtime.dsar_store.get(
            key=key, tenant_id=tenant_id, aad=request_id.bytes
        )
        zf2 = zipfile.ZipFile(io.BytesIO(zip_bytes2))
        wav = zf2.read(f"recordings/{ids['audio']}.wav")
        assert wav == AUDIO_PLAINTEXT
        monkeypatch.setattr(settings, "dsar_include_raw_audio", False)

        # ── Recovery: stale executing → takeover → re-run to done ────
        from core_service.config import settings as cfg

        await su.execute(
            "UPDATE patient_privacy_requests SET status = 'executing', "
            "executing_at = now() - interval '2 hours' WHERE id = $1", request_id,
        )
        stale_before = datetime.now(UTC) - timedelta(minutes=cfg.dsar_stale_minutes)
        async with tenant_connection(app_pool, tenant_id) as conn:
            taken = await privacy_repository.revert_stale_executing(
                conn, request_id=request_id, stale_before=stale_before
            )
            assert taken is not None and taken["status"] == "requested"
            restarted = await privacy_repository.mark_executing(conn, request_id=request_id)
            assert restarted is not None
        await engine.run_export(
            state, tenant_id=tenant_id, request_id=request_id,
            patient_id=patient_id, actor_sub=ids["user"],
        )
        final = await su.fetchrow(
            "SELECT status, package_object_key FROM patient_privacy_requests WHERE id = $1",
            request_id,
        )
        # Exactly one final object: same key, rebuilt in place.
        assert final["status"] == "completed" and final["package_object_key"] == key
    finally:
        try:
            runtime = getattr(state, "dsar_runtime", None)
            if runtime is not None:
                await runtime.dsar_store.delete(key=f"dsar/{tenant_id}/{request_id}.zip")
                await runtime.audio_store.delete(key=f"{tenant_id}/{MARK}.enc")
                await runtime.transcript_store.delete(key=f"{tenant_id}/{MARK}.json.enc")
                await runtime.audit_pool.close()
                await runtime.crypto_pool.close()
        except Exception:  # noqa: BLE001
            pass
        await cleanup_fixture(su)
        await app_pool.close()
        await audit_pool.close()
        await su.close()
