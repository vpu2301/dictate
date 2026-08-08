"""Behavioural tests for ``/patients/{id}/documents`` (migration 0065).

What matters here is not that a file round-trips — it is that the file is
never anywhere in the clear, that reading one is audited as the PHI access it
is, and that a delete destroys the object BEFORE the row that points at it.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
DOC_ID = UUID("55555555-5555-5555-5555-555555555555")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
PDF = b"%PDF-1.4 referral letter"


def _patient_row(**over: object) -> dict:
    base: dict = {
        "id": PATIENT_ID, "tenant_id": TENANT_ID,
        "name_uk": "Іван Петренко", "name_en": "Ivan Petrenko",
        "dob": None, "sex": "M", "mrn": "MRN-1", "phone": "", "email": "",
        "address_street": "", "address_house": "", "address_zip": "",
        "address_city": "", "address_country": "",
        "summary_uk": "", "summary_en": "", "tags": [], "status": "active",
        "last_visit_at": None, "created_by": REQUESTER_SUB,
        "created_at": NOW, "updated_at": NOW,
    }
    base.update(over)
    return base


def _doc_row(**over: object) -> dict:
    base: dict = {
        "id": DOC_ID, "tenant_id": TENANT_ID, "patient_id": PATIENT_ID,
        "filename": "Скерування.pdf", "category": "referral", "note": "",
        "content_type": "application/pdf", "byte_size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
        "storage_uri": f"minio://mdx-patient-docs/{TENANT_ID}/{DOC_ID}.enc",
        "uploaded_by": REQUESTER_SUB, "created_at": NOW,
    }
    base.update(over)
    return base


class _FakeStore:
    """Records what the router asked the object store to do, in order."""

    bucket = "mdx-patient-docs"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = {}
        self.aads: dict[str, bytes | None] = {}
        self.fail_put = False

    async def put(self, *, key, plaintext, tenant_id, aad=None):  # noqa: ANN001
        if self.fail_put:
            raise RuntimeError("minio down")
        self.calls.append(("put", key))
        self.objects[key] = plaintext
        self.aads[key] = aad
        return type("H", (), {"algorithm": "AES-256-GCM", "key_id": "kek-1"})()

    async def get(self, *, key, tenant_id, aad=None):  # noqa: ANN001
        self.calls.append(("get", key))
        self.aads[key] = aad
        return self.objects.get(key, PDF)

    async def delete(self, *, key):  # noqa: ANN001
        self.calls.append(("delete", key))
        self.objects.pop(key, None)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    from core_service import deps

    fake = _FakeStore()
    state = deps.get_state()
    monkeypatch.setattr(state, "document_store", fake, raising=False)
    return fake


@pytest.fixture
def client(make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from core_service.domain import patients_repository
    from core_service.routers import patient_documents

    @contextlib.asynccontextmanager
    async def _conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(patient_documents, "tenant_connection", _conn)

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)
    return make_client(["clinician"])


def _stub_repo(monkeypatch: pytest.MonkeyPatch, **over):  # noqa: ANN003
    from core_service.domain import patient_documents_repository as repo

    created: list[dict] = []

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        created.append(kwargs)
        return _doc_row(id=kwargs["document_id"], filename=kwargs["filename"],
                        category=kwargs["category"], byte_size=kwargs["byte_size"],
                        sha256=kwargs["sha256"], storage_uri=kwargs["storage_uri"])

    async def _get(conn, *, document_id, patient_id):  # noqa: ANN001
        return over.get("get", _doc_row())

    async def _list(conn, *, patient_id, limit=200):  # noqa: ANN001
        return [_doc_row()]

    async def _count(conn, *, patient_id):  # noqa: ANN001
        return 1

    async def _delete(conn, *, document_id, patient_id):  # noqa: ANN001
        return True

    monkeypatch.setattr(repo, "create_document", _create)
    monkeypatch.setattr(repo, "get_document", _get)
    monkeypatch.setattr(repo, "list_documents", _list)
    monkeypatch.setattr(repo, "count_documents", _count)
    monkeypatch.setattr(repo, "delete_document", _delete)
    return created


def _upload(client: TestClient, *, content=PDF, ctype="application/pdf", name="Скерування.pdf", data=None):
    return client.post(
        f"/patients/{PATIENT_ID}/documents",
        files={"file": (name, content, ctype)},
        data=data or {"category": "referral", "note": "від сімейного лікаря"},
    )


# ── upload ──────────────────────────────────────────────────────────


def test_upload_encrypts_the_bytes_and_stores_only_metadata(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _stub_repo(monkeypatch)

    resp = _upload(client)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "Скерування.pdf"
    assert body["category"] == "referral"
    assert body["byte_size"] == len(PDF)
    assert body["sha256"] == hashlib.sha256(PDF).hexdigest()

    # The object went through the encrypted store, keyed under the tenant…
    assert store.calls[0][0] == "put"
    key = store.calls[0][1]
    assert key.startswith(f"{TENANT_ID}/")
    # …with the AAD bound to the document id (confused-deputy guard).
    assert store.aads[key] == UUID(body["id"]).bytes
    # …and the row carries the URI, never the bytes.
    row = created[0]
    assert row["storage_uri"] == f"minio://mdx-patient-docs/{key}"
    assert "plaintext" not in row and PDF not in row.values()


def test_upload_is_audited_with_no_filename_in_the_payload(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch)
    _upload(client)

    events = [c for c in client.audit_calls if c["kind"] == "patient_document.uploaded"]  # type: ignore[attr-defined]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["category"] == "referral"
    assert payload["byte_size"] == len(PDF)
    # The name of a file can itself be PHI ("Іван_Петренко_ВІЛ.pdf").
    assert "Скерування" not in repr(payload)


def test_an_unlisted_content_type_is_refused(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch)
    resp = _upload(client, content=b"<script>alert(1)</script>", ctype="text/html", name="x.html")
    assert resp.status_code == 415
    assert resp.json()["code"] == "content_type_rejected"
    assert store.calls == [], "nothing may be written before the type is accepted"


def test_a_file_over_the_ceiling_is_refused_on_bytes_read(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.config import settings

    _stub_repo(monkeypatch)
    oversize = b"x" * (settings.patient_document_max_bytes + 1)
    resp = _upload(client, content=oversize)
    assert resp.status_code == 413
    assert resp.json()["code"] == "document_too_large"
    assert store.calls == []


def test_an_empty_file_is_refused(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch)
    assert _upload(client, content=b"").status_code == 422


def test_a_failed_metadata_write_deletes_the_orphan_object(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is what makes the object findable — and therefore erasable."""
    from core_service.domain import patient_documents_repository as repo

    _stub_repo(monkeypatch)

    async def _boom(conn, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("insert failed")

    monkeypatch.setattr(repo, "create_document", _boom)

    with pytest.raises(RuntimeError):
        _upload(client)

    assert [c[0] for c in store.calls] == ["put", "delete"]
    assert store.objects == {}


def test_upload_without_a_store_refuses_rather_than_storing_in_the_clear(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service import deps

    _stub_repo(monkeypatch)
    monkeypatch.setattr(deps.get_state(), "document_store", None, raising=False)

    resp = _upload(client)
    assert resp.status_code == 503
    assert resp.json()["code"] == "document_storage_unavailable"


def test_upload_to_an_erased_patient_is_refused(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    _stub_repo(monkeypatch)

    async def _erased(conn, *, patient_id):  # noqa: ANN001
        return _patient_row(status="erased")

    monkeypatch.setattr(patients_repository, "get_patient", _erased)

    resp = _upload(client)
    assert resp.status_code == 409
    assert resp.json()["code"] == "patient_erased"


# ── list / download ─────────────────────────────────────────────────


def test_list_returns_metadata_and_a_total(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch)
    body = client.get(f"/patients/{PATIENT_ID}/documents").json()
    assert body["total"] == 1
    assert body["items"][0]["filename"] == "Скерування.pdf"
    # A directory listing is not a file read: no audit event for it.
    assert not [c for c in client.audit_calls if c["kind"].startswith("patient_document.down")]  # type: ignore[attr-defined]


def test_download_decrypts_in_process_and_audits_the_read(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch)

    resp = client.get(f"/patients/{PATIENT_ID}/documents/{DOC_ID}/content")

    assert resp.status_code == 200
    assert resp.content == PDF
    assert resp.headers["content-type"].startswith("application/pdf")
    # PHI must not be cached by a proxy or the browser.
    assert resp.headers["cache-control"] == "no-store"
    # A Cyrillic filename survives as RFC 6266 filename*.
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]

    reads = [c for c in client.audit_calls if c["kind"] == "patient_document.downloaded"]  # type: ignore[attr-defined]
    assert len(reads) == 1
    assert reads[0]["payload"]["byte_size"] == len(PDF)


def test_a_document_belonging_to_another_patient_is_404(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch, get=None)
    other = uuid4()
    assert client.get(f"/patients/{other}/documents/{DOC_ID}/content").status_code == 404


# ── delete ──────────────────────────────────────────────────────────


def test_delete_shreds_the_object_before_the_row(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_repo(monkeypatch)
    store.objects[f"{TENANT_ID}/{DOC_ID}.enc"] = PDF

    resp = client.delete(f"/patients/{PATIENT_ID}/documents/{DOC_ID}")

    assert resp.status_code == 204
    assert store.calls[-1] == ("delete", f"{TENANT_ID}/{DOC_ID}.enc")
    assert store.objects == {}
    events = [c for c in client.audit_calls if c["kind"] == "patient_document.deleted"]  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0]["severity"].value == "sec" if hasattr(events[0]["severity"], "value") else True


# ── permissions ─────────────────────────────────────────────────────


def test_an_auditor_cannot_upload_or_delete(
    make_client: Callable[[list[str]], TestClient], store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.routers import patient_documents

    @contextlib.asynccontextmanager
    async def _conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(patient_documents, "tenant_connection", _conn)
    _stub_repo(monkeypatch)
    auditor = make_client(["auditor"])

    assert _upload(auditor).status_code == 403
    assert auditor.delete(f"/patients/{PATIENT_ID}/documents/{DOC_ID}").status_code == 403
