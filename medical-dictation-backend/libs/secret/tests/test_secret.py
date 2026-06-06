"""Exhaustive leak-channel coverage for Secret[T]."""

from __future__ import annotations

import copy
import json
import pickle

import pytest
from pydantic import BaseModel

from secret import Secret


def test_value_is_readable() -> None:
    s = Secret("hunter2")
    assert s.value() == "hunter2"


def test_repr_is_masked() -> None:
    assert repr(Secret("hunter2")) == "Secret(***)"


def test_str_is_masked() -> None:
    assert str(Secret("hunter2")) == "Secret(***)"


def test_format_is_masked() -> None:
    s = Secret("hunter2")
    assert f"{s}" == "Secret(***)"
    assert f"{s:>20}" == "Secret(***)"
    assert format(s, "") == "Secret(***)"


def test_str_repr_do_not_contain_value() -> None:
    s = Secret("supersecretvalue")
    for rendered in (str(s), repr(s), format(s, "")):
        assert "supersecretvalue" not in rendered


def test_pickle_is_refused() -> None:
    with pytest.raises(TypeError):
        pickle.dumps(Secret("hunter2"))


def test_copy_is_refused() -> None:
    with pytest.raises(TypeError):
        copy.copy(Secret("hunter2"))


def test_deepcopy_is_refused() -> None:
    with pytest.raises(TypeError):
        copy.deepcopy(Secret("hunter2"))


def test_equality_same_value() -> None:
    assert Secret("a") == Secret("a")


def test_equality_different_value() -> None:
    assert Secret("a") != Secret("b")


def test_not_equal_to_raw_value() -> None:
    assert (Secret("a") == "a") is False
    assert (Secret(b"a") == b"a") is False


def test_bytes_equality_constant_time() -> None:
    assert Secret(b"\x00\x01") == Secret(b"\x00\x01")
    assert Secret(b"\x00\x01") != Secret(b"\x00\x02")


def test_hash_is_value_independent() -> None:
    assert hash(Secret("a")) == hash(Secret("b"))


def test_default_json_dumps_emits_mask_via_str() -> None:
    rendered = json.dumps({"token": Secret("hunter2")}, default=str)
    assert "hunter2" not in rendered
    assert "Secret(***)" in rendered


def test_pydantic_model_dump_emits_redacted() -> None:
    class M(BaseModel):
        api_key: Secret[str]

    m = M(api_key=Secret("hunter2"))
    dumped = m.model_dump()
    assert dumped == {"api_key": "<redacted>"}
    assert "hunter2" not in m.model_dump_json()


def test_pydantic_validates_raw_string_into_secret() -> None:
    class M(BaseModel):
        api_key: Secret[str]

    m = M(api_key="hunter2")  # type: ignore[arg-type]
    assert isinstance(m.api_key, Secret)
    assert m.api_key.value() == "hunter2"


def test_truthiness() -> None:
    assert bool(Secret("x")) is True
    assert bool(Secret("")) is False


def test_no_value_in_traceback_when_repr_consumed() -> None:
    s = Secret("topsecret")
    try:
        raise ValueError(repr(s))
    except ValueError as e:  # noqa: BLE001
        assert "topsecret" not in str(e)
        assert "Secret(***)" in str(e)


def test_secret_inside_collection_str_safe() -> None:
    secrets = {"k": Secret("topsecret")}
    rendered = str(secrets)
    assert "topsecret" not in rendered
    assert "Secret(***)" in rendered
