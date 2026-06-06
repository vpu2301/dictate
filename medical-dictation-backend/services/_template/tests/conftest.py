import os

# Disable OTel and auth for unit tests before any app import
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("ENVIRONMENT", "test")

import pytest
from fastapi.testclient import TestClient

from template_service.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app, raise_server_exceptions=True) as c:
        return c
