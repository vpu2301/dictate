"""S11 step 05 §8 integration — the everything-fixture is fully enumerated.

Builds a patient with one of EVERY artifact class (encounter, recording,
batch transcript job, dictation session, clinical note, draft report,
signed report + envelope, signed + unsigned consents, anamnesis, privacy
request, signing session) and proves ``enumerate_patient`` finds all of
it with correct counts — the inventory both the DSAR export (step 06)
and the erasure engine (step 07) run on.

Also unit-tests the CI gate's pure check logic, including the
soft-linked mutation case (§8: removing the signed_envelopes assertion
must fire) and the dead-entry case.

Skipped unless ``RUN_DB_INTEGRATION=1``.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from db import create_pool, tenant_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up && make seed`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

MARK = "itest-fanout-s11s05"


async def build_fixture_patient_with_everything(
    su: asyncpg.Connection,
) -> tuple[UUID, UUID, dict]:
    """(tenant_id, patient_id, ids) with one of every artifact class planted.

    Rows are SQL-planted (the S02/S03 live E2Es already proved the real
    creation paths); MinIO objects are step 07's crypto-shred proof and
    are not required for enumeration.
    """
    row = await su.fetchrow(
        "SELECT t.id AS tid, u.sub FROM tenants t JOIN users u "
        "ON u.tenant_id = t.id LIMIT 1"
    )
    if row is None:
        pytest.skip("needs a seeded tenant with a user (`make seed`)")
    tenant_id, user = row["tid"], row["sub"]

    # Contact details (0060) are populated so the erasure tombstone assertion
    # is non-vacuous — the columns must be cleared, not merely already blank.
    patient_id = await su.fetchval(
        "INSERT INTO patients (tenant_id, name_uk, created_by, ipn_hmac, "
        "phone, email, address_street, address_house, address_zip, "
        "address_city, address_country) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
        tenant_id, MARK, user, os.urandom(32),
        "+380671234567", f"{MARK}@example.com",
        f"вул. {MARK}", "1, кв. 2", "01001", "Київ", "Україна",
    )
    # `status` defaults to 'completed', and 0058 added the
    # encounters_ended_has_ts biconditional — a terminal row MUST carry
    # ended_at. The fixture predates that migration; set it explicitly.
    encounter_id = await su.fetchval(
        "INSERT INTO encounters (tenant_id, patient_id, created_by, reason, "
        "ended_at) "
        "VALUES ($1, $2, $3, $4, now()) RETURNING id",
        tenant_id, patient_id, user, MARK,
    )
    audio_id = await su.fetchval(
        "INSERT INTO audio_files (tenant_id, uploader_sub, mime_type, size_bytes, "
        "duration_ms, sha256, envelope_metadata, storage_uri, status, encounter_id) "
        "VALUES ($1, $2, 'audio/wav', 1, 1, $3, '{}'::jsonb, $4, 'stored', $5) RETURNING id",
        tenant_id, user, b"\x00" * 32, f"minio://mdx-audio/{tenant_id}/{MARK}.enc", encounter_id,
    )
    prompt_id = await su.fetchval("SELECT id FROM medical_prompts LIMIT 1")
    job_id = await su.fetchval(
        "INSERT INTO transcription_jobs (tenant_id, audio_id, requester_sub, "
        "prompt_id, language, result_storage_uri) VALUES ($1, $2, $3, $4, 'uk', $5) "
        "RETURNING id",
        tenant_id, audio_id, user, prompt_id,
        f"minio://mdx-transcripts/{tenant_id}/{MARK}.json.enc",
    )
    await su.execute(
        "INSERT INTO dictation_sessions (tenant_id, user_id, language, prompt_id, "
        "encounter_id, audio_file_id, transcript_jsonb) "
        "VALUES ($1, $2, 'uk', $3, $4, $5, '[{\"text\": \"transcript\"}]'::jsonb)",
        tenant_id, user, prompt_id, encounter_id, audio_id,
    )
    await su.execute(
        "INSERT INTO clinical_notes (tenant_id, patient_id, encounter_id, "
        "title, author_id) VALUES ($1, $2, $3, $4, $5)",
        tenant_id, patient_id, encounter_id, MARK, user,
    )
    await su.execute(
        "INSERT INTO patient_anamnesis (patient_id, tenant_id, record, updated_by) "
        "VALUES ($1, $2, '{}'::jsonb, $3)",
        patient_id, tenant_id, user,
    )
    await su.execute(
        "INSERT INTO patient_privacy_requests (tenant_id, patient_id, kind, "
        "reason, status, requested_by) VALUES ($1, $2, 'dsar', $3, 'requested', $4)",
        tenant_id, patient_id, MARK, user,
    )

    # Draft report (+version) and signed report (+version + envelope).
    async def _report(status: str) -> tuple[UUID, UUID]:
        rid = await su.fetchval(
            "INSERT INTO reports (tenant_id, code, primary_author_id, patient_id, "
            "status, title, signed_at, finalized_at) "
            "VALUES ($1, $2, $3, $4, $5::report_status, $6, "
            "CASE WHEN $5::text = 'signed' THEN now() END, "
            "CASE WHEN $5::text = 'signed' THEN now() END) RETURNING id",
            tenant_id, f"{MARK}-{status}-{uuid4().hex[:6]}", user, patient_id, status, MARK,
        )
        vid = await su.fetchval(
            "INSERT INTO report_versions (report_id, version_number, created_by, "
            "content_jsonb) VALUES ($1, 1, $2, '{}'::jsonb) RETURNING id",
            rid, user,
        )
        await su.execute(
            "UPDATE reports SET current_version_id = $2 WHERE id = $1", rid, vid
        )
        return rid, vid

    draft_report, _draft_version = await _report("draft")
    signed_report, signed_version = await _report("signed")
    # Amendment history: a second version on the signed report.
    amendment_version = await su.fetchval(
        "INSERT INTO report_versions (report_id, version_number, created_by, "
        "content_jsonb, parent_version_id, is_amendment, amendment_type, "
        "amendment_reason) "
        "VALUES ($1, 2, $2, '{\"amended\": true}'::jsonb, $3, true, "
        "'correction', 'itest amendment') RETURNING id",
        signed_report, user, signed_version,
    )
    await su.execute(
        "UPDATE reports SET current_version_id = $2 WHERE id = $1",
        signed_report, amendment_version,
    )

    async def _envelope(resource_type: str, resource_id: UUID, version_id: UUID) -> UUID:
        return await su.fetchval(
            """
            INSERT INTO signed_envelopes
                (tenant_id, signer_user_id, resource_type, resource_id,
                 resource_version_id, provider, provider_session_id,
                 provider_envelope_id, canonical_json, canonical_json_hash,
                 signed_at, signed_data, signature_algorithm,
                 verification_token, signer_full_name, certificate_serial,
                 certificate_issuer_cn, signature_level)
            VALUES ($1, $2, $3, $4, $5, 'mock', $6, $6, '{}'::jsonb, $7,
                    now(), $8, 'test', $9, $10, '', '', 'qualified')
            RETURNING id
            """,
            tenant_id, user, resource_type, resource_id, version_id,
            f"{MARK}-{uuid4().hex[:8]}", b"\x01" * 32, b"cms", uuid4().hex[:22], MARK,
        )

    await _envelope("report", signed_report, signed_version)

    consent_signed = await su.fetchval(
        "INSERT INTO patient_consents (tenant_id, patient_id, method, created_by, "
        "canonical_hash) VALUES ($1, $2, 'digital', $3, $4) RETURNING id",
        tenant_id, patient_id, user, b"\x02" * 32,
    )
    consent_envelope = await _envelope("consent", consent_signed, consent_signed)
    await su.execute(
        "UPDATE patient_consents SET signed_envelope_id = $2 WHERE id = $1",
        consent_signed, consent_envelope,
    )
    await su.execute(
        "INSERT INTO patient_consents (tenant_id, patient_id, method, created_by) "
        "VALUES ($1, $2, 'verbal', $3)",
        tenant_id, patient_id, user,
    )
    await su.execute(
        "INSERT INTO signing_sessions (tenant_id, initiated_by, resource_type, "
        "resource_id, resource_version_id, provider, provider_session_id, "
        "expires_at, canonical_json) "
        "VALUES ($1, $2, 'consent', $3, $3, 'mock', $4, now() + interval '1h', "
        "'{\"patient\": \"" + MARK + "\"}')",
        tenant_id, user, consent_signed, f"{MARK}-{uuid4().hex[:8]}",
    )
    return tenant_id, patient_id, {
        "user": user,
        "encounter": encounter_id,
        "audio": audio_id,
        "job": job_id,
        "draft_report": draft_report,
        "signed_report": signed_report,
        "signed_version": signed_version,
        "amendment_version": amendment_version,
        "consent_signed": consent_signed,
        "consent_envelope": consent_envelope,
    }


async def cleanup_fixture(su: asyncpg.Connection) -> None:
    # Fixture patients are found by marker name OR (post-erasure tombstones,
    # renamed to 'ERASED') via their marker privacy requests.
    pids = [
        r["id"] for r in await su.fetch(
            "SELECT id FROM patients WHERE name_uk = $1 "
            "UNION SELECT patient_id FROM patient_privacy_requests WHERE reason = $1",
            MARK,
        )
    ]
    await su.execute("DELETE FROM signing_sessions WHERE provider_session_id LIKE $1", f"{MARK}%")
    await su.execute(
        "UPDATE patient_consents SET signed_envelope_id = NULL "
        "WHERE signed_envelope_id IN (SELECT id FROM signed_envelopes WHERE signer_full_name = $1) "
        "OR patient_id = ANY($2::uuid[])",
        MARK, pids,
    )
    await su.execute("DELETE FROM signed_envelopes WHERE signer_full_name = $1", MARK)
    await su.execute("DELETE FROM patient_privacy_requests WHERE patient_id = ANY($1::uuid[])", pids)
    await su.execute("DELETE FROM patient_consents WHERE patient_id = ANY($1::uuid[])", pids)
    await su.execute(
        "DELETE FROM transcription_jobs WHERE audio_id IN (SELECT a.id FROM audio_files a "
        "JOIN encounters e ON e.id = a.encounter_id WHERE e.patient_id = ANY($1::uuid[]))",
        pids,
    )
    await su.execute(
        "DELETE FROM dictation_sessions WHERE encounter_id IN "
        "(SELECT id FROM encounters WHERE patient_id = ANY($1::uuid[]))", pids,
    )
    await su.execute(
        "DELETE FROM audio_files WHERE encounter_id IN "
        "(SELECT id FROM encounters WHERE patient_id = ANY($1::uuid[]))", pids,
    )
    await su.execute(
        "UPDATE reports SET current_version_id = NULL WHERE patient_id = ANY($1::uuid[])",
        pids,
    )
    await su.execute(
        "DELETE FROM report_versions WHERE report_id IN "
        "(SELECT id FROM reports WHERE patient_id = ANY($1::uuid[]))", pids,
    )
    await su.execute(
        "DELETE FROM reports WHERE patient_id = ANY($1::uuid[])", pids,
    )
    await su.execute(
        "DELETE FROM patients WHERE name_uk = $1 OR id = ANY($2::uuid[])", MARK, pids
    )


async def test_everything_fixture_is_fully_enumerated() -> None:
    from core_service.erasure import enumerate_patient

    su = await asyncpg.connect(SU_DSN)
    app_pool = await create_pool(APP_DSN, application_name="itest", min_size=1, max_size=1)
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, _ids = await build_fixture_patient_with_everything(su)

        async with tenant_connection(app_pool, tenant_id) as conn:
            inventory = await enumerate_patient(conn, patient_id)

        expected = {
            "patient": 1,
            "encounter": 1,
            "clinical_note": 1,
            "anamnesis": 1,
            "consent": 2,           # signed + unsigned
            "privacy_request": 1,
            "report": 2,            # draft + signed
            "report_version": 3,  # draft v1 + signed v1 + amendment v2
            "synthesis_job": 0,     # none planted (no live synthesis run)
            "recording": 1,
            "transcription_job": 1,
            "dictation_session": 1,
            "signed_envelope": 2,   # report + consent
            "signing_session": 1,
        }
        assert inventory.counts == expected, inventory.counts
        assert inventory.total == sum(expected.values())

        # Blob-backed artifacts expose their ciphertext refs.
        assert inventory.items["recording"][0].object_ref.startswith("minio://mdx-audio/")
        assert inventory.items["transcription_job"][0].object_ref.startswith(
            "minio://mdx-transcripts/"
        )
        # Sanitization: the export payload never carries ІПН material.
        patient_payload = inventory.items["patient"][0].payload
        for banned in ("ipn_hmac", "ipn_encrypted", "ipn_dek", "tenant_id"):
            assert banned not in patient_payload
    finally:
        await cleanup_fixture(su)
        await app_pool.close()
        await su.close()


# ── CI-gate pure-logic tests (incl. the §8 mutation cases) ───────────


def test_gate_flags_unregistered_dead_and_softlink() -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "check_gate",
        Path(__file__).resolve().parents[4] / "scripts/ci/check_erasure_fanout_coverage.py",
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    # Unregistered closure table.
    problems = gate.run_checks(
        closure_tables={"encounters", "new_patient_thing"},
        existing_map_tables={"encounters"},
        fanout_tables={"encounters", "signed_envelopes", "signing_sessions"},
        allowlist=set(),
        soft_linked={"signed_envelopes", "signing_sessions"},
    )
    assert any("new_patient_thing" in p and "UNREGISTERED" in p for p in problems)

    # Dead map entry.
    problems = gate.run_checks(
        closure_tables={"encounters"},
        existing_map_tables={"encounters"},
        fanout_tables={"encounters", "ghost_table", "signed_envelopes", "signing_sessions"},
        allowlist=set(),
        soft_linked=set(),
    )
    assert any("ghost_table" in p and "DEAD" in p for p in problems)

    # Mutation case: dropping signed_envelopes from the map MUST fire the
    # hardcoded soft-link assertion (FK scanning can never catch it).
    problems = gate.run_checks(
        closure_tables={"encounters"},
        existing_map_tables={"encounters"},
        fanout_tables={"encounters", "signing_sessions"},
        allowlist=set(),
        soft_linked={"signed_envelopes", "signing_sessions"},
    )
    assert any("signed_envelopes" in p and "SOFT-LINKED" in p for p in problems)

    # Clean input → no problems.
    assert (
        gate.run_checks(
            closure_tables={"encounters"},
            existing_map_tables={"encounters", "signed_envelopes", "signing_sessions"},
            fanout_tables={"encounters", "signed_envelopes", "signing_sessions"},
            allowlist=set(),
            soft_linked={"signed_envelopes", "signing_sessions"},
        )
        == []
    )
