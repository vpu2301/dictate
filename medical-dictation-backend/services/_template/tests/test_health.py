"""Tests for liveness/readiness endpoints and core middleware."""

from fastapi.testclient import TestClient


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_ready(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_openapi_spec_is_3_1(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert data["openapi"].startswith("3.1"), f"Expected OpenAPI 3.1.x, got {data['openapi']}"


def test_request_id_header_propagated(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "test-id-123"})
    assert response.headers.get("X-Request-ID") == "test-id-123"


def test_request_id_generated_when_absent(client: TestClient) -> None:
    response = client.get("/healthz")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) > 0


def test_404_returns_problem_detail(client: TestClient) -> None:
    response = client.get("/nonexistent-route")
    assert response.status_code == 404
    assert response.headers.get("content-type") == "application/problem+json"
    body = response.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert "type" in body
    assert body["instance"].startswith("urn:uuid:")


def test_create_app_returns_independent_instance() -> None:
    """The factory must produce a fresh app each call so tests don't share state."""
    from template_service.main import create_app

    a, b = create_app(), create_app()
    assert a is not b
