"""Unit tests for the ``check-no-os-environ`` gate.

Proves both directions: the gate REJECTS an ``os.environ`` read in an ordinary
service module (exit 1, file named) and PERMITS it in the sanctioned surfaces
(``config.py``, ``tests/``, ``libs/secret/``, explicit ``# noqa: ENV001``).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

ENV_READ = 'import os\nVALUE = os.environ["X"]\n'


def _write(tmp_path: Path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


# ── reject ───────────────────────────────────────────────────────────────


def test_rejects_os_environ_in_service(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "svc/handler.py", ENV_READ)
    assert env_gate.main([target]) == 1


def test_rejects_os_getenv_too(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "svc/handler.py", 'import os\nx = os.getenv("X")\n')
    assert env_gate.main([target]) == 1


def test_reports_offending_path(env_gate: ModuleType, tmp_path: Path, capsys) -> None:
    target = _write(tmp_path, "svc/handler.py", ENV_READ)
    env_gate.main([target])
    err = capsys.readouterr().err
    assert "handler.py" in err
    assert "ENV001" in err


# ── allow ────────────────────────────────────────────────────────────────


def test_allows_in_config_py(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "svc/config.py", ENV_READ)
    assert env_gate.main([target]) == 0


def test_allows_in_tests_dir(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "svc/tests/test_thing.py", ENV_READ)
    assert env_gate.main([target]) == 0


def test_allows_in_libs_secret(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "libs/secret/src/secret/x.py", ENV_READ)
    assert env_gate.main([target]) == 0


def test_allows_with_noqa(env_gate: ModuleType, tmp_path: Path) -> None:
    body = 'import os\nx = os.environ["X"]  # noqa: ENV001\n'
    target = _write(tmp_path, "svc/handler.py", body)
    assert env_gate.main([target]) == 0


def test_allows_clean_file(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "svc/handler.py", "x = 1\n")
    assert env_gate.main([target]) == 0


def test_ignores_non_python(env_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "svc/notes.txt", ENV_READ)
    assert env_gate.main([target]) == 0
