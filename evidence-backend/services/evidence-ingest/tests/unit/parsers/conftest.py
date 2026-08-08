from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return _FIXTURES
