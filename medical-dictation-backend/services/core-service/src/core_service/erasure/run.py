"""Operator entry point for the erasure engine (S11 step 07).

    uv run --project services/core-service \
        python -m core_service.erasure.run --tenant <tenant_id> --request <request_id>

Same engine, same advisory lock as the scheduler — cron + manual can
never double-run. Prints the report_of_execution JSON on success; exits
non-zero with the refusal code otherwise (see docs/runbooks/erasure.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from uuid import UUID

from .engine import ErasureRefusedError, ErasureRuntime, execute_erasure


async def _main(tenant_id: UUID, request_id: UUID, operator: str) -> int:
    runtime = await ErasureRuntime.build()
    try:
        report = await execute_erasure(
            runtime, tenant_id=tenant_id, request_id=request_id, operator=operator
        )
    except ErasureRefusedError as exc:
        print(f"REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one approved erasure request.")
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--request", required=True, type=UUID)
    parser.add_argument("--operator", default="manual")
    args = parser.parse_args()
    return asyncio.run(_main(args.tenant, args.request, args.operator))


if __name__ == "__main__":
    sys.exit(main())
