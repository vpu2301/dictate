"""Unit tests for the ``check-no-direct-asyncpg`` gate.

Proves both directions: the gate REJECTS a raw ``asyncpg.connect`` /
``create_pool`` in a service module (exit 1, file named) and PERMITS it inside
``libs/db/`` (the sanctioned driver home) or under an explicit
``# noqa: DB001``.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

CONNECT = 'import asyncpg\nasync def go():\n    return await asyncpg.connect("postgres://...")\n'
CREATE_POOL = (
    'import asyncpg\nasync def go():\n    return await asyncpg.create_pool("postgres://...")\n'
)


def _write(tmp_path: Path, rel: str, body: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return str(p)


# ── reject ───────────────────────────────────────────────────────────────


def test_rejects_connect_in_service(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "services/svc/src/svc/repo.py", CONNECT)
    assert asyncpg_gate.main([target]) == 1


def test_rejects_create_pool_in_service(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "services/svc/src/svc/repo.py", CREATE_POOL)
    assert asyncpg_gate.main([target]) == 1


def test_reports_offending_path(asyncpg_gate: ModuleType, tmp_path: Path, capsys) -> None:
    target = _write(tmp_path, "services/svc/src/svc/repo.py", CONNECT)
    asyncpg_gate.main([target])
    err = capsys.readouterr().err
    assert "repo.py" in err
    assert "DB001" in err


# ── allow ────────────────────────────────────────────────────────────────


def test_allows_in_libs_db(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "libs/db/src/db/pool.py", CONNECT)
    assert asyncpg_gate.main([target]) == 0


def test_allows_with_noqa(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    body = 'import asyncpg\nasync def go():\n    return await asyncpg.connect("x")  # noqa: DB001\n'
    target = _write(tmp_path, "services/svc/src/svc/repo.py", body)
    assert asyncpg_gate.main([target]) == 0


def test_allows_clean_file(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "services/svc/src/svc/repo.py", "x = 1\n")
    assert asyncpg_gate.main([target]) == 0


def test_allows_in_integration_tests(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "libs/audit/tests/integration/test_x.py", CONNECT)
    assert asyncpg_gate.main([target]) == 0


def test_allows_in_ops_scripts(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    target = _write(tmp_path, "scripts/db/migrate.py", CONNECT)
    assert asyncpg_gate.main([target]) == 0


def test_does_not_flag_tenant_connection(asyncpg_gate: ModuleType, tmp_path: Path) -> None:
    body = (
        "from db import tenant_connection\n"
        "async def go(pool, tid):\n"
        "    async with tenant_connection(pool, tid) as conn:\n"
        "        return conn\n"
    )
    target = _write(tmp_path, "services/svc/src/svc/repo.py", body)
    assert asyncpg_gate.main([target]) == 0
