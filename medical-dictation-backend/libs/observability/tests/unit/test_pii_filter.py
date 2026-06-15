"""Adversarial test corpus for the PII filter.

We exercise every drop-list and mask-list entry, plus nested structures, to
prove that no live PII or secret material survives the filter on the path to
a log sink.
"""

from __future__ import annotations

import logging
from io import StringIO

import pytest

from observability.pii_filter import (
    _DROP_NAMES,
    _MASK_NAMES,
    PIISafeFilter,
    scrub,
)

LIVE_VALUE = "live-value-must-not-appear-in-output"


# ──────────────────────────────────────────────────────────────────────
# Pure scrub() — drop list
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", sorted(_DROP_NAMES))
def test_drop_list_removes_value(field: str) -> None:
    out = scrub({field: LIVE_VALUE, "keep_me": "ok"})
    assert field not in out
    assert out.get("keep_me") == "ok"


@pytest.mark.parametrize("field", sorted(_DROP_NAMES))
def test_drop_list_case_insensitive(field: str) -> None:
    upper = field.upper()
    out = scrub({upper: LIVE_VALUE})
    assert upper not in out
    assert LIVE_VALUE not in str(out)


# ──────────────────────────────────────────────────────────────────────
# Pure scrub() — mask list
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", sorted(_MASK_NAMES))
def test_mask_list_replaces_value(field: str) -> None:
    out = scrub({field: LIVE_VALUE})
    assert out[field] == "<redacted>"


def test_patient_prefix_is_masked() -> None:
    out = scrub({"patient_id": LIVE_VALUE, "patient_email": LIVE_VALUE})
    assert out["patient_id"] == "<redacted>"
    assert out["patient_email"] == "<redacted>"


# ──────────────────────────────────────────────────────────────────────
# Nested structures
# ──────────────────────────────────────────────────────────────────────


def test_nested_dict_is_scrubbed() -> None:
    payload = {
        "request": {
            "headers": {"authorization": "Bearer xyz", "user-agent": "ok"},
            "body": LIVE_VALUE,
        },
        "user": {"email": LIVE_VALUE, "id": "123"},
    }
    out = scrub(payload)
    assert "authorization" not in out["request"]["headers"]
    assert out["request"]["headers"]["user-agent"] == "ok"
    assert "body" not in out["request"]
    assert out["user"]["email"] == "<redacted>"
    assert out["user"]["id"] == "123"


def test_list_of_dicts_is_scrubbed() -> None:
    payload = [
        {"email": LIVE_VALUE, "id": 1},
        {"phone": LIVE_VALUE, "id": 2},
    ]
    out = scrub(payload)
    assert out[0]["email"] == "<redacted>"
    assert out[0]["id"] == 1
    assert out[1]["phone"] == "<redacted>"
    assert out[1]["id"] == 2


def test_deeply_nested_at_leaf_5_levels() -> None:
    payload = {"a": {"b": {"c": {"d": {"e": {"password": LIVE_VALUE}}}}}}
    out = scrub(payload)
    assert "password" not in out["a"]["b"]["c"]["d"]["e"]


def test_no_live_value_appears_anywhere() -> None:
    payload = {
        "password": LIVE_VALUE,
        "patient_name": LIVE_VALUE,
        "headers": {"cookie": LIVE_VALUE, "authorization": LIVE_VALUE},
        "history": [{"transcript": LIVE_VALUE}, {"audio": b"raw-bytes"}],
    }
    rendered = repr(scrub(payload))
    assert LIVE_VALUE not in rendered
    assert "raw-bytes" not in rendered


def test_scrub_is_idempotent() -> None:
    payload = {"password": LIVE_VALUE, "email": LIVE_VALUE, "ok": "value"}
    once = scrub(payload)
    twice = scrub(once)
    assert once == twice


def test_max_depth_does_not_crash() -> None:
    payload: dict = {"x": LIVE_VALUE}
    for _ in range(20):
        payload = {"down": payload}
    # No exception; deeply nested values past the cap are simply not scrubbed.
    scrub(payload)


# ──────────────────────────────────────────────────────────────────────
# stdlib logging integration
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def captured_logger() -> tuple[logging.Logger, StringIO]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(PIISafeFilter())
    handler.setFormatter(logging.Formatter("%(message)s | %(password)s | %(email)s"))
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream


def test_log_record_drops_password_extra(captured_logger: tuple[logging.Logger, StringIO]) -> None:
    logger, stream = captured_logger
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.info("login attempt", extra={"password": LIVE_VALUE, "user": "alice"})
    out = stream.getvalue()
    assert LIVE_VALUE not in out


def test_log_record_masks_email_extra(captured_logger: tuple[logging.Logger, StringIO]) -> None:
    logger, stream = captured_logger
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s|%(email)s"))
    logger.info("hi", extra={"email": LIVE_VALUE})
    out = stream.getvalue()
    assert LIVE_VALUE not in out
    assert "<redacted>" in out


def test_log_record_scrubs_dict_extras(captured_logger: tuple[logging.Logger, StringIO]) -> None:
    logger, stream = captured_logger
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s|%(headers)s"))
    logger.info(
        "incoming",
        extra={"headers": {"authorization": "Bearer xyz", "x-trace-id": "abc"}},
    )
    out = stream.getvalue()
    assert "Bearer xyz" not in out
    assert "abc" in out


def test_log_message_string_with_embedded_json_is_masked(
    captured_logger: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured_logger
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.info('{"password": "%s", "ok": true}', LIVE_VALUE)
    out = stream.getvalue()
    assert LIVE_VALUE not in out


# ──────────────────────────────────────────────────────────────────────
# False-positive guard: stdlib record attributes pass through.
# ──────────────────────────────────────────────────────────────────────


def test_logger_module_name_not_dropped() -> None:
    """``name`` is a stdlib record attribute; the filter must not break it."""
    record = logging.LogRecord(
        name="myapp.module",
        level=logging.INFO,
        pathname="/x.py",
        lineno=1,
        msg="hello",
        args=None,
        exc_info=None,
    )
    PIISafeFilter().filter(record)
    assert record.name == "myapp.module"
