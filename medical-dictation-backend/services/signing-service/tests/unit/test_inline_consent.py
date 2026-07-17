"""S11 step 03 — the inline route accepts resource_type='consent' and the
strict consent link aborts cleanly.

Reuses the test_inline_sign harness (faked signer + monkeypatched repo).
"""

from __future__ import annotations

from medical_kep import ProviderName
from signing_service import repository as real_repo

from tests.unit.test_inline_sign import (
    _body,
    _dev_envelope,
    _FakeSigner,
    harness,  # noqa: F401  (fixture re-export)
)


def test_inline_consent_marks_consent_resource(harness) -> None:  # noqa: F811
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())
    body = _body()
    body["resource_type"] = "consent"
    body["resource_version_id"] = body["resource_id"]  # consents: version == id

    resp = harness.client.post("/signing/inline", json=body)
    assert resp.status_code == 200, resp.text
    assert harness.calls["mark"]["resource_type"] == "consent"
    assert str(harness.calls["mark"]["resource_id"]) == body["resource_id"]
    # Consents have no report status; the response field stays honest.
    # (The fake mark returns "signed"; the real one returns the consent
    # status — either way the field is pass-through.)
    assert harness.calls["insert"]["resource_type"] == "consent"


def test_inline_consent_link_error_maps_to_conflict(harness, monkeypatch) -> None:  # noqa: F811
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())

    async def _mark_raises(conn, **kwargs):
        raise real_repo.ResourceLinkError("consent_already_signed")

    monkeypatch.setattr(harness.inline.repo, "mark_resource_signed", _mark_raises)

    body = _body()
    body["resource_type"] = "consent"
    body["resource_version_id"] = body["resource_id"]
    resp = harness.client.post("/signing/inline", json=body)
    assert resp.status_code == 409, resp.text
    assert "consent_already_signed" in resp.text


def test_inline_consent_not_found_maps_to_404(harness, monkeypatch) -> None:  # noqa: F811
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())

    async def _mark_raises(conn, **kwargs):
        raise real_repo.ResourceLinkError("consent_not_found")

    monkeypatch.setattr(harness.inline.repo, "mark_resource_signed", _mark_raises)

    body = _body()
    body["resource_type"] = "consent"
    body["resource_version_id"] = body["resource_id"]
    resp = harness.client.post("/signing/inline", json=body)
    assert resp.status_code == 404, resp.text


def test_report_path_unchanged(harness) -> None:  # noqa: F811
    """Guard: the consent branch must not disturb the report flow."""
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())
    resp = harness.client.post("/signing/inline", json=_body())
    assert resp.status_code == 200, resp.text
    assert resp.json()["report_status"] == "signed"
    assert harness.calls["mark"]["resource_type"] == "report"
