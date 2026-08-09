"""DSAR export engine (S11 step 06) — GDPR Art. 15 / Law 2297-VI.

Walks the fan-out map (`enumerate_patient` — the SAME inventory the
erasure engine destroys along), assembles the complete readable package,
stores it as an envelope-encrypted ZIP under
``dsar/{tenant}/{request_id}.zip``, and records honest completion
metadata on the ``patient_privacy_requests`` row.

Async model: an in-service background task with DB-state recovery — DSAR
volume is per-request-tiny, so a queue would be over-engineering (noted
in docs/architecture/erasure.md). Recovery is ON-REQUEST, not on-startup:
``tenants`` is RLS-invisible to app_role, so a cross-tenant startup scan
is impossible by design — instead a POST or status GET that finds an
``executing`` DSAR older than ``DSAR_STALE_MINUTES`` takes it over and
re-runs (idempotent: the package is rebuilt, the old object replaced).

Delivery: the package is served by an AUTHENTICATED decrypt-and-stream
endpoint, not a presigned URL — architecture rule: presigned URLs serve
ciphertext (useless to the data subject), and an unauthenticated
plaintext link would be a strictly weaker control than the
``patient.dsar`` permission gate. Every download is audited.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from audit import Severity
from crypto import Envelope, TenantKekRepository, build_master_key_provider
from db import create_pool, tenant_connection
from storage import EncryptedObjectStore, S3Client

from .. import audit_kinds
from ..config import settings
from ..domain import privacy_repository
from .fanout import ExportItem, FanoutInventory, enumerate_patient

logger = logging.getLogger(__name__)

# S11 step 08 — observability. Labels: enums only, never ids.
from opentelemetry import metrics as _otel_metrics  # noqa: E402

_meter = _otel_metrics.get_meter("core-service.dsar")
_export_seconds = _meter.create_histogram(
    "mdx_dsar_export_seconds", unit="s", description="DSAR package build duration"
)
_package_bytes = _meter.create_histogram(
    "mdx_dsar_package_bytes", unit="By", description="DSAR ZIP size"
)

ENGINE_VERSION = "dsar-engine/1"
MANIFEST_FORMAT_VERSION = "1"


# ── Lazy runtime (crypto + stores + audit reader) ───────────────────


@dataclass
class DsarRuntime:
    audio_store: EncryptedObjectStore
    transcript_store: EncryptedObjectStore
    dsar_store: EncryptedObjectStore
    envelope: Envelope
    audit_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool


async def get_runtime(state: Any) -> DsarRuntime:
    """Build (once) and cache the object-store/crypto/audit wiring on the
    service state — the service starts fine without MinIO until the first
    export needs it."""
    runtime = getattr(state, "dsar_runtime", None)
    if runtime is not None:
        return runtime

    crypto_pool = await create_pool(
        settings.db_crypto_writer_dsn,
        application_name=f"{settings.service_name}/dsar-crypto",
        min_size=1,
        max_size=2,
    )
    master = build_master_key_provider(
        provider=settings.master_key_provider,
        file_path=settings.master_key_path,
        vault_addr=settings.vault_addr,
        vault_token=settings.vault_token,
        vault_transit_key=settings.vault_transit_key,
        vault_transit_mount=settings.vault_transit_mount,
    )
    await master.startup_self_check()
    kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
    envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    audit_pool = await create_pool(
        settings.db_audit_reader_dsn,
        application_name=f"{settings.service_name}/dsar-audit",
        min_size=1,
        max_size=2,
    )
    runtime = DsarRuntime(
        audio_store=EncryptedObjectStore(s3=s3, bucket=settings.s3_audio_bucket, envelope=envelope),
        transcript_store=EncryptedObjectStore(
            s3=s3, bucket=settings.s3_transcripts_bucket, envelope=envelope
        ),
        dsar_store=EncryptedObjectStore(s3=s3, bucket=settings.s3_dsar_bucket, envelope=envelope),
        envelope=envelope,
        audit_pool=audit_pool,
        crypto_pool=crypto_pool,
    )
    state.dsar_runtime = runtime
    return runtime


def package_key(tenant_id: UUID, request_id: UUID) -> str:
    return f"dsar/{tenant_id}/{request_id}.zip"


def _object_key(storage_uri: str) -> str:
    return storage_uri.split("://", 1)[1].split("/", 1)[1]


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


# ── Assembly ────────────────────────────────────────────────────────


@dataclass
class _Package:
    items: list[dict[str, Any]]
    excluded: list[dict[str, str]]
    root: Path

    def add(self, kind: str, item_id: Any, rel_path: str, content: bytes) -> None:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.items.append(
            {
                "kind": kind,
                "id": str(item_id),
                "path": rel_path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    def exclude(self, kind: str, reason: str) -> None:
        self.excluded.append({"kind": kind, "reason": reason})


def _transcript_text(transcript_jsonb: Any) -> str:
    if isinstance(transcript_jsonb, str):
        transcript_jsonb = json.loads(transcript_jsonb)
    parts = [seg.get("text", "") for seg in transcript_jsonb or [] if isinstance(seg, dict)]
    return "\n".join(p for p in parts if p)


async def _write_patient_json(
    pkg: _Package, runtime: DsarRuntime, tenant_id: UUID, items: list[ExportItem]
) -> None:
    item = items[0]
    payload = dict(item.payload or {})
    if settings.dsar_include_raw_ipn:
        # DPO-gated; only possible when raw retention captured a ciphertext.
        raw = await _decrypt_raw_ipn(runtime, tenant_id, item.id)
        if raw is not None:
            payload["ipn"] = raw
        else:
            pkg.exclude("patient.ipn", "no encrypted raw ІПН stored (hmac-only capture)")
    else:
        pkg.exclude("patient.ipn", "raw ІПН excluded by policy (DSAR_INCLUDE_RAW_IPN=false)")
    pkg.add("patient", item.id, "patient.json", _json_bytes(payload))


async def _decrypt_raw_ipn(
    runtime: DsarRuntime, tenant_id: UUID, patient_id: UUID
) -> str | None:
    from crypto import unpack_ipn_envelope

    async with runtime.crypto_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ipn_encrypted, ipn_dek FROM patients WHERE id = $1 AND tenant_id = $2",
            patient_id,
            tenant_id,
        )
    if row is None or row["ipn_encrypted"] is None:
        return None
    blob = unpack_ipn_envelope(
        ipn_encrypted=row["ipn_encrypted"], ipn_dek=row["ipn_dek"], tenant_id=tenant_id
    )
    plaintext = await runtime.envelope.decrypt(blob, tenant_id=tenant_id, aad=patient_id.bytes)
    return plaintext.decode("utf-8")


async def _audit_slice(
    runtime: DsarRuntime, tenant_id: UUID, inventory: FanoutInventory
) -> list[dict[str, Any]]:
    """Subject-accessible audit events: allowlisted kinds whose target is
    the patient or any of their artifacts. The allowlist is the DPO's
    knob (DSAR_AUDIT_KINDS)."""
    ids = {str(inventory.patient_id)}
    for items in inventory.items.values():
        ids.update(str(i.id) for i in items)
    # audit.events is tenant-GUC-scoped even for audit_reader — the read
    # runs under the sanctioned tenant_connection like every RLS query.
    async with tenant_connection(runtime.audit_pool, tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT created_at, kind, target_kind, target_id, payload_jcs
            FROM audit.events
            WHERE tenant_id = $1 AND kind = ANY($2::text[])
              AND target_id = ANY($3::text[])
            ORDER BY created_at
            """,
            tenant_id,
            settings.dsar_audit_kinds_list,
            list(ids),
        )
    return [
        {
            "at": r["created_at"].isoformat(),
            "kind": r["kind"],
            "target_kind": r["target_kind"],
            "target_id": r["target_id"],
            "detail": json.loads(r["payload_jcs"]) if r["payload_jcs"] else {},
        }
        for r in rows
    ]


_README = """\
Пакет даних пацієнта (запит на доступ до персональних даних)
=============================================================

Цей архів містить усі персональні дані, які заклад зберігає про вас
(Закон України «Про захист персональних даних» № 2297-VI, ст. 8;
GDPR, ст. 15).

Вміст:
- patient.json         — ваш обліковий запис
- encounters/          — візити
- consents/            — надані/відкликані згоди (з посиланням на
                         перевірку КЕП, якщо згоду підписано)
- reports/             — медичні звіти: чинна версія, повна історія
                         змін, підписаний PDF (якщо є)
- transcripts/         — текстові розшифровки диктувань
- recordings/          — метадані аудіозаписів (без самого аудіо —
                         аудіо надається за окремим запитом)
- audit/               — журнал дій з вашими даними (доступна пацієнту
                         частина)
- manifest.json        — машинозчитуваний перелік файлів із
                         контрольними сумами; розділ "excluded"
                         називає все, що НЕ включено, і причину

---

Patient data package (data subject access request)
==================================================

This archive contains the personal data the clinic holds about you
(Ukraine Law 2297-VI art. 8; GDPR art. 15). manifest.json is the
machine-readable index with per-file checksums; its "excluded" section
names anything not included and why — nothing is omitted silently.
"""


async def assemble_package(
    conn: asyncpg.Connection,
    runtime: DsarRuntime,
    *,
    tenant_id: UUID,
    request_id: UUID,
    patient_id: UUID,
    workdir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Build the ZIP in ``workdir`` → (zip_path, manifest)."""
    inventory = await enumerate_patient(conn, patient_id)
    pkg = _Package(items=[], excluded=[], root=workdir / "package")
    pkg.root.mkdir(parents=True)

    await _write_patient_json(pkg, runtime, tenant_id, inventory.items["patient"])

    for item in inventory.items["encounter"]:
        pkg.add("encounter", item.id, f"encounters/{item.id}.json", _json_bytes(item.payload))
    for item in inventory.items["clinical_note"]:
        pkg.add("clinical_note", item.id, f"notes/{item.id}.json", _json_bytes(item.payload))
    for item in inventory.items["anamnesis"]:
        pkg.add("anamnesis", item.id, "anamnesis.json", _json_bytes(item.payload))
    for item in inventory.items["privacy_request"]:
        pkg.add(
            "privacy_request", item.id, f"privacy-requests/{item.id}.json",
            _json_bytes(item.payload),
        )

    # Consents + their verification tokens when signed.
    envelopes_by_resource: dict[str, dict] = {
        str(e.payload["resource_id"]): e.payload
        for e in inventory.items["signed_envelope"]
        if e.payload
    }
    for item in inventory.items["consent"]:
        payload = dict(item.payload or {})
        env = envelopes_by_resource.get(str(item.id))
        if env:
            payload["signature"] = {
                "verification_token": env["verification_token"],
                "verify_path": f"/verify/{env['verification_token']}",
                "signed_at": str(env["signed_at"]),
                "signer_full_name": env["signer_full_name"],
            }
        pkg.add("consent", item.id, f"consents/{item.id}.json", _json_bytes(payload))

    # Reports: current version + full amendment history (+ stored PDF).
    versions_by_report: dict[str, list[ExportItem]] = {}
    for v in inventory.items["report_version"]:
        versions_by_report.setdefault(str((v.payload or {}).get("report_id")), []).append(v)
    for item in inventory.items["report"]:
        rid = str(item.id)
        payload = dict(item.payload or {})
        current_id = str(payload.get("current_version_id"))
        versions = sorted(
            versions_by_report.get(rid, []),
            key=lambda v: (v.payload or {}).get("version_number", 0),
        )
        pkg.add("report", item.id, f"reports/{rid}/report.json", _json_bytes(payload))
        for v in versions:
            n = (v.payload or {}).get("version_number", 0)
            rel = (
                f"reports/{rid}/current.json"
                if str(v.id) == current_id
                else f"reports/{rid}/versions/{n}.json"
            )
            pkg.add("report_version", v.id, rel, _json_bytes(v.payload))
        env = envelopes_by_resource.get(rid)
        if env:
            pkg.add(
                "signed_envelope", env["resource_id"],
                f"reports/{rid}/signature.json", _json_bytes(env),
            )

    # Transcripts: streaming (row-persisted) + batch (encrypted object).
    for item in inventory.items["dictation_session"]:
        text = _transcript_text((item.payload or {}).get("transcript_jsonb"))
        if text:
            pkg.add(
                "dictation_session", item.id,
                f"transcripts/{item.id}.txt", text.encode("utf-8"),
            )
    for item in inventory.items["transcription_job"]:
        if not item.object_ref:
            continue
        raw = await runtime.transcript_store.get(
            key=_object_key(item.object_ref), tenant_id=tenant_id, aad=item.id.bytes
        )
        try:
            text = _transcript_text(json.loads(raw).get("segments"))
        except Exception:  # noqa: BLE001 — export verbatim when shape unknown
            text = raw.decode("utf-8", errors="replace")
        pkg.add(
            "transcription_job", item.id,
            f"transcripts/{item.id}.txt", text.encode("utf-8"),
        )

    # Recordings: metadata always; raw audio only behind the DPO flag.
    for item in inventory.items["recording"]:
        meta = dict(item.payload or {})
        meta.pop("storage_uri", None)
        pkg.add(
            "recording", item.id,
            f"recordings/{item.id}.metadata.json", _json_bytes(meta),
        )
        if settings.dsar_include_raw_audio and item.object_ref:
            audio = await runtime.audio_store.get(
                key=_object_key(item.object_ref), tenant_id=tenant_id, aad=item.id.bytes
            )
            pkg.add("recording_audio", item.id, f"recordings/{item.id}.wav", audio)
    if not settings.dsar_include_raw_audio:
        pkg.exclude(
            "recording_audio",
            "raw audio excluded by policy (DSAR_INCLUDE_RAW_AUDIO=false) — "
            "available on request / аудіо надається за окремим запитом",
        )

    pkg.exclude(
        "audit.other_kinds",
        "only subject-accessible audit kinds are included (DSAR_AUDIT_KINDS); "
        "operator/security internals are excluded",
    )
    audit_events = await _audit_slice(runtime, tenant_id, inventory)
    pkg.add("audit_slice", patient_id, "audit/subject-accessible.json", _json_bytes(audit_events))

    (pkg.root / "README.txt").write_text(_README, encoding="utf-8")

    manifest = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "request_id": str(request_id),
        "patient_id": str(patient_id),
        "generated_at": datetime.now(UTC).isoformat(),
        "engine_version": ENGINE_VERSION,
        "items": pkg.items,
        "excluded": pkg.excluded,
        "inventory_counts": inventory.counts,
    }
    (pkg.root / "manifest.json").write_bytes(_json_bytes(manifest))

    zip_path = workdir / "package.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(pkg.root.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(pkg.root))
    return zip_path, manifest


# ── The task ────────────────────────────────────────────────────────


async def run_export(
    state: Any, *, tenant_id: UUID, request_id: UUID, patient_id: UUID, actor_sub: UUID
) -> None:
    """Background task: assemble → store → complete (or fail honestly)."""
    runtime = await get_runtime(state)
    workdir = Path(tempfile.mkdtemp(prefix="dsar-"))
    import time as _time

    started_monotonic = _time.monotonic()
    try:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            zip_path, manifest = await assemble_package(
                conn, runtime,
                tenant_id=tenant_id, request_id=request_id,
                patient_id=patient_id, workdir=workdir,
            )
            zip_bytes = zip_path.read_bytes()
            key = package_key(tenant_id, request_id)
            await runtime.dsar_store.put(
                key=key, plaintext=zip_bytes, tenant_id=tenant_id, aad=request_id.bytes
            )
            summary = {
                "engine_version": ENGINE_VERSION,
                "item_count": len(manifest["items"]),
                "excluded": manifest["excluded"],
                "inventory_counts": manifest["inventory_counts"],
                "package_sha256": hashlib.sha256(zip_bytes).hexdigest(),
                "package_bytes": len(zip_bytes),
            }
            await privacy_repository.set_package_key(conn, request_id=request_id, key=key)
            await privacy_repository.mark_completed(
                conn, request_id=request_id, report_of_execution=summary
            )
        await state.audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.DSAR_EXPORT_COMPLETED,
            actor_sub=actor_sub,
            target_kind="patient",
            target_id=str(patient_id),
            payload={
                "request_id": str(request_id),
                "item_count": summary["item_count"],
                "package_sha256": summary["package_sha256"],
            },
            severity=Severity.SEC,
        )
        _export_seconds.record(
            _time.monotonic() - started_monotonic, attributes={"status": "completed"}
        )
        _package_bytes.record(len(zip_bytes))
    except Exception as exc:  # noqa: BLE001 — the row must record the failure
        _export_seconds.record(
            _time.monotonic() - started_monotonic, attributes={"status": "failed"}
        )
        logger.exception("dsar.export_failed request=%s", request_id)
        try:
            async with tenant_connection(state.app_pool, tenant_id) as conn:
                await privacy_repository.mark_failed(conn, request_id=request_id)
            await state.audit_writer.write_event(
                tenant_id=tenant_id,
                kind=audit_kinds.DSAR_EXPORT_FAILED,
                actor_sub=actor_sub,
                target_kind="patient",
                target_id=str(patient_id),
                payload={"request_id": str(request_id), "error_class": type(exc).__name__},
                severity=Severity.SEC,
            )
        except Exception:  # noqa: BLE001
            logger.exception("dsar.failure_record_failed request=%s", request_id)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
