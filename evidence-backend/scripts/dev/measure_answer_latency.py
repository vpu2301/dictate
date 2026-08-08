#!/usr/bin/env python3
"""Measure the Quick Search first-token NFR against a live stack (AC-S04).

The S04 spec sets a latency NFR — **first token <= 3.5 s warm, <= 6 s cold** —
that has never been measured: everything so far is proven against fakes. This
drives the real `POST /answers` SSE stream end to end and reports the number.

    python scripts/dev/measure_answer_latency.py            # cold + 3 warm
    python scripts/dev/measure_answer_latency.py --runs 5
    python scripts/dev/measure_answer_latency.py --question "..." --locale uk

What "first token" means here, stated so the number is comparable run to run:
the wall clock from the request being sent to the FIRST `summary_segment`
event — the first thing a user can read. The `header` event arrives earlier and
is deliberately not the measurement: it is emitted before retrieval and
synthesis, so timing it would report the HTTP round trip and flatter the
pipeline by several seconds.

Cold vs warm is the generator's prompt cache and the OS page cache, so the
first run of a process is cold by definition and only the first one counts as
the cold number.

Reads no env of its own beyond the standard dev endpoints (rule E8 applies to
services, not to a dev harness, but the defaults match the platform's).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_QUESTION = "Empiric therapy for community-acquired pneumonia in adults?"

# The NFR under test.
WARM_BUDGET_S = 3.5
COLD_BUDGET_S = 6.0


_FORM = {"client_id": "mdx-dev-cli", "grant_type": "password"}


def get_token(keycloak: str, realm: str, username: str, password: str) -> str:
    """Direct-grant token from the host."""
    body = urllib.parse.urlencode({**_FORM, "username": username, "password": password}).encode()
    url = f"{keycloak}/realms/{realm}/protocol/openid-connect/token"
    try:
        with urllib.request.urlopen(url, data=body, timeout=30) as resp:  # nosec B310 — fixed http(s) dev URL  # noqa: S310
            return str(json.load(resp)["access_token"])
    except urllib.error.HTTPError as exc:
        sys.exit(f"token request failed ({exc.code}): {exc.read().decode()[:200]}")
    except OSError as exc:
        sys.exit(f"cannot reach Keycloak at {keycloak}: {exc}")


def get_token_via_docker(container: str, realm: str, username: str, password: str) -> str:
    """Direct-grant token fetched from INSIDE the compose network.

    Keycloak derives the `iss` claim from the request's Host header (no
    KC_HOSTNAME is set in dev). A token fetched from the host is stamped
    `http://localhost:8088/realms/...`, but services running in compose are
    configured with `AUTH_ISSUER=http://keycloak:8080/realms/...` and reject it
    with a flat `401 Invalid issuer` — nothing about the message points at the
    topology. Same credentials, same realm, different Host header.

    So the token is fetched through a container already on the network, using
    its own Python rather than pulling a curl image.
    """
    import subprocess  # nosec B404 — fixed docker argv below, never shell, never user input

    script = (
        "import json,urllib.parse,urllib.request;"
        f"b=urllib.parse.urlencode({{'client_id':'mdx-dev-cli','grant_type':'password',"
        f"'username':{username!r},'password':{password!r}}}).encode();"
        f"u='http://keycloak:8080/realms/{realm}/protocol/openid-connect/token';"
        "print(json.load(urllib.request.urlopen(u,data=b,timeout=30))['access_token'])"
    )
    proc = subprocess.run(  # nosec B603 B607 — fixed docker argv  # noqa: S603
        ["docker", "exec", container, "python", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        sys.exit(
            f"in-network token fetch via `docker exec {container}` failed:\n"
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout.strip()


def _iter_sse_lines(resp: object, started: float):  # noqa: ANN202
    """Yield (line, elapsed) as each line lands, without buffering ahead.

    Timestamping is the whole point of this harness, so lines must be timed
    when they arrive on the socket rather than when a buffer happens to be
    drained.
    """
    buf = b""
    while True:
        chunk = resp.read1(4096)  # type: ignore[attr-defined]
        if not chunk:
            break
        now = time.perf_counter() - started
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            yield raw.decode("utf-8", "replace").rstrip("\r"), now


class RunResult:
    def __init__(self) -> None:
        self.first_summary_s: float | None = None
        self.header_s: float | None = None
        self.done_s: float | None = None
        self.events: list[str] = []
        self.status: str | None = None
        self.error: str | None = None
        self.segments = 0
        self.sources = 0


def one_run(answer_url: str, token: str, question: str, locale: str, timeout: float) -> RunResult:
    """Drive one SSE stream, timing events as they arrive."""
    payload = json.dumps({"question": question, "mode": "quick_search", "locale": locale}).encode()
    req = urllib.request.Request(  # noqa: S310
        f"{answer_url}/answers",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )

    result = RunResult()
    started = time.perf_counter()
    event_kind: str | None = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — fixed http(s) dev URL  # noqa: S310
            # read1(), not `for line in resp`. Iterating the response goes
            # through a buffered reader that happily returns several SSE events
            # from one underlying read, which timestamps them all identically —
            # it reported the first summary_segment and `done` at the same
            # instant when curl showed 0.2 s between them. read1 returns as soon
            # as any bytes are available, so each event is timed when it lands.
            for line, now in _iter_sse_lines(resp, started):
                if line.startswith("event:"):
                    event_kind = line.split(":", 1)[1].strip()
                    result.events.append(event_kind)
                    if event_kind == "header" and result.header_s is None:
                        result.header_s = now
                    # The measurement: first readable content, not first byte.
                    elif event_kind == "summary_segment" and result.first_summary_s is None:
                        result.first_summary_s = now
                    elif event_kind == "done":
                        result.done_s = now
                    if event_kind in ("summary_segment", "detail_segment"):
                        result.segments += 1
                    elif event_kind in ("source", "late_source"):
                        result.sources += 1

                elif line.startswith("data:") and event_kind in ("done", "error"):
                    try:
                        data = json.loads(line.split(":", 1)[1].strip())
                    except json.JSONDecodeError:
                        continue
                    # The payload nests under the event name — the `done` frame
                    # is {"event":"done","done":{...,"status":...}}, not a flat
                    # object, so a top-level .get("status") silently reads "".
                    if event_kind == "done":
                        result.status = str(data.get("done", {}).get("status", ""))
                    else:
                        result.error = json.dumps(data.get("error", data))[:300]
    except urllib.error.HTTPError as exc:
        result.error = f"HTTP {exc.code}: {exc.read().decode()[:300]}"
    except OSError as exc:
        result.error = f"transport: {exc}"

    return result


def report(label: str, r: RunResult, budget: float) -> bool:
    if r.error:
        print(f"  {label:<6} FAILED — {r.error}")
        return False
    if r.first_summary_s is None:
        # A deflected or insufficient-basis answer legitimately emits no
        # summary_segment; that is a valid pipeline outcome but not a latency
        # sample, and averaging it in would quietly understate the number.
        print(
            f"  {label:<6} no summary_segment (status={r.status}) — "
            f"header {r.header_s:.2f}s, not a latency sample"
            if r.header_s is not None
            else f"  {label:<6} no events at all"
        )
        return False
    ok = r.first_summary_s <= budget
    print(
        f"  {label:<6} first token {r.first_summary_s:5.2f}s  "
        f"(budget {budget}s) {'PASS' if ok else 'FAIL'}   "
        f"header {r.header_s:.2f}s  done {r.done_s or float('nan'):.2f}s  "
        f"segments {r.segments}  sources {r.sources}  status {r.status}"
    )
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--answer-url", default="http://localhost:8013")
    p.add_argument("--keycloak", default="http://localhost:8088")
    p.add_argument("--realm", default="medical-dictation")
    p.add_argument("--username", default="clinician@tenant-a.example")
    p.add_argument("--password", default="dev-password")
    p.add_argument("--question", default=DEFAULT_QUESTION)
    p.add_argument("--locale", default="en")
    p.add_argument("--runs", type=int, default=3, help="warm runs after the cold one")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument(
        "--token-container",
        default="evidence-evidence-answer-1",
        help="container used to fetch an in-network token if the host token is rejected",
    )
    args = p.parse_args()

    print(f"token from {args.keycloak} as {args.username}")
    token = get_token(args.keycloak, args.realm, args.username, args.password)

    print(f"\nquestion: {args.question!r}  locale={args.locale}")
    print(f"target:   {args.answer_url}/answers\n")

    # One cheap probe before timing anything: if the target runs in compose it
    # wants an issuer only reachable from inside the network, and every timed
    # run would otherwise 401. Detected rather than configured, so the same
    # command works against a uvicorn stack and a compose stack.
    probe = one_run(args.answer_url, token, "probe", args.locale, 20.0)
    if probe.error and "Invalid issuer" in probe.error:
        print(
            f"host token rejected (issuer mismatch) — the stack is running in "
            f"compose; refetching via {args.token_container}\n"
        )
        token = get_token_via_docker(args.token_container, args.realm, args.username, args.password)

    cold = one_run(args.answer_url, token, args.question, args.locale, args.timeout)
    cold_ok = report("cold", cold, COLD_BUDGET_S)
    if cold.events:
        print(f"         event order: {' -> '.join(dedupe_runs(cold.events))}")

    warm: list[float] = []
    warm_ok = True
    for i in range(args.runs):
        r = one_run(args.answer_url, token, args.question, args.locale, args.timeout)
        ok = report(f"warm{i + 1}", r, WARM_BUDGET_S)
        warm_ok = warm_ok and ok
        if r.first_summary_s is not None:
            warm.append(r.first_summary_s)

    print()
    if warm:
        print(
            f"warm first token: median {statistics.median(warm):.2f}s  "
            f"min {min(warm):.2f}s  max {max(warm):.2f}s  n={len(warm)}"
        )
    verdict = cold_ok and warm_ok and bool(warm)
    print(
        f"NFR (<= {COLD_BUDGET_S}s cold, <= {WARM_BUDGET_S}s warm): "
        f"{'MET' if verdict else 'NOT MET'}"
    )
    return 0 if verdict else 1


def dedupe_runs(events: list[str]) -> list[str]:
    """`a a a b b c` -> `a*, b*, c` so the order is readable at a glance."""
    out: list[str] = []
    for e in events:
        if out and out[-1].rstrip("*") == e:
            if not out[-1].endswith("*"):
                out[-1] = f"{e}*"
        else:
            out.append(e)
    return out


if __name__ == "__main__":
    sys.exit(main())
