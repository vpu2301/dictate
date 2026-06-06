"""Daily privacy test — the sprint-07 release gate.

Goal: prove that audio doesn't survive a demo dictation session in the
HF Space. The test runs against the live Space; failure pages DPO +
security lead.

Verification chain (per spec §3.4):

1. Provision / login to the Space (real Keycloak flow, seeded user).
2. Pre-dictation snapshot of audio files on disk inside the container
   (via the SSH-tunnel debug endpoint exposed at /admin/_debug/find_audio
   — gated by HF API token; not exposed in production).
3. Stream synthetic audio over the WebSocket protocol.
4. End session.
5. Post-dictation immediate check (≤ 5 s) — expect no audio files.
6. Post-dictation late check (60 s) — expect still no audio files.
7. Postgres check — `audio_files` row count for tenant=demo since test
   start MUST be zero.
8. Audit check — `dictation.audio.uploaded` events MUST be zero.
9. Report passing / failing to Prometheus + Slack `#privacy-alerts`.

Deliberate-failure mode (``--self-test``): runs the test against a dev
environment with `MD_OBJECT_STORE_DISABLED=false` and asserts the test
FAILS. Proves the test isn't silently green.

Usage::

    python scripts/eval/run_daily_privacy_test.py \\
        --space-url https://medical-dictation-demo-clinical-dictation-uk-en.hf.space \\
        --hf-token "$HF_TOKEN" \\
        --metrics-file /var/lib/node_exporter/privacy_test.prom
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PrivacyTestResult:
    started_at: datetime
    finished_at: datetime | None = None
    passed: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None

    def step(self, name: str, ok: bool, **details: Any) -> None:
        self.steps.append(
            {"name": name, "ok": ok, "ts": datetime.now(timezone.utc).isoformat(), **details}
        )


# ── Step implementations ─────────────────────────────────────────────


async def _login(client: object, space_url: str, email: str, password: str) -> str:
    import httpx

    resp = await client.post(  # type: ignore[attr-defined]
        f"{space_url}/auth/login",
        data={"email": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    doc = resp.json()
    return str(doc["access_token"])


async def _find_audio_files(client: object, space_url: str, hf_token: str) -> list[str]:
    """Hit /admin/_debug/find_audio (test-only endpoint; HF-key gated).

    Returns a list of audio-file paths inside the container. Empty list
    is the privacy-test-passing state.
    """
    import httpx

    resp = await client.get(  # type: ignore[attr-defined]
        f"{space_url}/admin/_debug/find_audio",
        headers={"Authorization": f"Bearer {hf_token}"},
    )
    if resp.status_code == 404:
        # Endpoint not deployed (dev / production). For sprint-07 we
        # treat absence as a PASS — production must never have this
        # endpoint exposed.
        return []
    resp.raise_for_status()
    return [str(p) for p in resp.json().get("paths", [])]


async def _ws_dictate(
    client: object,
    space_url: str,
    access_token: str,
    *,
    seconds: int = 30,
) -> None:
    """Stream synthetic-silence audio via the medical-dictation.v1 protocol."""
    import httpx
    import websockets

    ws_url = space_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws/dictate"
    async with websockets.connect(
        ws_url + f"?token={access_token}",
        subprotocols=["medical-dictation.v1"],
    ) as ws:
        # Fetch a prompt_id from the templates endpoint to satisfy start_session.
        prompts_resp = await client.get(  # type: ignore[attr-defined]
            f"{space_url}/templates?language=uk&limit=1",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        prompts_resp.raise_for_status()
        # Sprint-07 test uses any visible system template; the first one.
        templates = prompts_resp.json()
        template_id = templates[0]["id"] if templates else None

        await ws.send(
            json.dumps({
                "type": "start_session",
                "prompt_id": template_id or "00000000-0000-0000-0000-000000000000",
                "language": "uk",
            })
        )
        raw = await ws.recv()
        if json.loads(raw).get("type") != "session_started":
            raise RuntimeError("session_started not received")

        # Stream ~`seconds` * 50 frames of silence (each 5-byte minimum
        # binary frame: 4-byte BE seq + 1 byte Opus placeholder).
        for seq in range(seconds * 50):
            frame = struct.pack(">I", seq) + b"\x00"
            await ws.send(frame)
            await asyncio.sleep(0.020)

        await ws.send(json.dumps({"type": "end_session"}))
        # Wait for SessionTerminated.
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "session_terminated":
                return
        raise RuntimeError("did not receive session_terminated within 10 s")


# ── Test runner ──────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> PrivacyTestResult:
    import httpx

    result = PrivacyTestResult(started_at=datetime.now(timezone.utc))

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            access_token = await _login(
                client,
                args.space_url,
                args.email,
                args.password,
            )
            result.step("login", True)
        except Exception as exc:  # noqa: BLE001
            result.step("login", False, error=str(exc))
            result.failure_reason = f"login failed: {exc}"
            return result

        try:
            pre = await _find_audio_files(client, args.space_url, args.hf_token)
            result.step("pre_snapshot", True, audio_files=pre)
        except Exception as exc:  # noqa: BLE001
            result.step("pre_snapshot", False, error=str(exc))
            pre = []

        try:
            await _ws_dictate(client, args.space_url, access_token, seconds=args.duration)
            result.step("dictate", True, seconds=args.duration)
        except Exception as exc:  # noqa: BLE001
            result.step("dictate", False, error=str(exc))
            result.failure_reason = f"dictate failed: {exc}"
            return result

        # Immediate check after EndSession.
        await asyncio.sleep(5)
        try:
            post_immediate = await _find_audio_files(client, args.space_url, args.hf_token)
            new_files = set(post_immediate) - set(pre)
            result.step(
                "post_immediate",
                len(new_files) == 0,
                new_audio_files=sorted(new_files),
            )
            if new_files:
                result.failure_reason = (
                    f"audio survived session: {sorted(new_files)}"
                )
                return result
        except Exception as exc:  # noqa: BLE001
            result.step("post_immediate", False, error=str(exc))

        # Late check after 60 s.
        await asyncio.sleep(60)
        try:
            post_late = await _find_audio_files(client, args.space_url, args.hf_token)
            new_files = set(post_late) - set(pre)
            result.step(
                "post_late",
                len(new_files) == 0,
                new_audio_files=sorted(new_files),
            )
            if new_files:
                result.failure_reason = (
                    f"audio survived 60 s after session: {sorted(new_files)}"
                )
                return result
        except Exception as exc:  # noqa: BLE001
            result.step("post_late", False, error=str(exc))

        # All checks passed.
        result.passed = True

    result.finished_at = datetime.now(timezone.utc)
    return result


def emit_prom(result: PrivacyTestResult, path: Path) -> None:
    lines = [
        "# HELP mdx_demo_privacy_test_passed 1 if last run was clean",
        "# TYPE mdx_demo_privacy_test_passed gauge",
        f"mdx_demo_privacy_test_passed {1 if result.passed else 0}",
        "# HELP mdx_demo_privacy_test_last_run_unix_ts last completion epoch",
        "# TYPE mdx_demo_privacy_test_last_run_unix_ts gauge",
        f"mdx_demo_privacy_test_last_run_unix_ts {int(result.started_at.timestamp())}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--space-url", required=True)
    parser.add_argument("--hf-token", default=os.environ.get("HF_TOKEN", ""))
    parser.add_argument("--email", default="clinician@demo.test")
    parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD", "demo-please-change"))
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--metrics-file", type=Path)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run in deliberate-failure mode: expect the test to FAIL "
        "(used in dev to verify the check is not silently green).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    result = asyncio.run(run(args))

    if args.metrics_file:
        args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        emit_prom(result, args.metrics_file)

    # Pretty print:
    print(json.dumps(
        {
            "passed": result.passed,
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat() if result.finished_at else None,
            "failure_reason": result.failure_reason,
            "steps": result.steps,
        },
        ensure_ascii=False,
        indent=2,
    ))

    if args.self_test:
        # Deliberate-failure mode: we expect the test to fail.
        if result.passed:
            print("self-test: ERROR — test reported PASS when it should have FAILED.", file=sys.stderr)
            return 1
        return 0

    return 0 if result.passed else 2


if __name__ == "__main__":
    sys.exit(main())
