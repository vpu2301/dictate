from __future__ import annotations

import hmac
from typing import Any, SupportsIndex, cast

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

_MASK = "Secret(***)"


class Secret[T]:
    """A value wrapper that refuses to leak via the usual channels.

    .value() is the single intentional read path. repr/str/format/f-string all
    return the constant mask. Pickling and deep-copying raise. Equality is
    constant-time. Hashing is by type identity, not value, so secrets cannot
    end up as dict keys whose lookup leaks the value via timing.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def value(self) -> T:
        return self._value

    def __repr__(self) -> str:
        return _MASK

    def __str__(self) -> str:
        return _MASK

    def __format__(self, _: str) -> str:
        return _MASK

    def __reduce__(self) -> Any:
        raise TypeError("Secret cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        raise TypeError("Secret cannot be pickled")

    def __copy__(self) -> Secret[T]:
        raise TypeError("Secret cannot be copied (use Secret(s.value()) explicitly if needed)")

    def __deepcopy__(self, memo: dict[int, Any]) -> Secret[T]:
        raise TypeError("Secret cannot be deep-copied")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        a, b = self._value, other._value
        if isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)):
            return hmac.compare_digest(bytes(a), bytes(b))
        if isinstance(a, str) and isinstance(b, str):
            # ``a`` is read from this instance's own type parameter ``T``, which
            # mypy does not narrow through isinstance (it stays widened to
            # ``object``); the guard proves it is str.
            return hmac.compare_digest(cast(str, a), b)
        return bool(a == b)

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return NotImplemented
        return not result

    def __hash__(self) -> int:
        return hash((type(self).__name__, id(type(self))))

    def __bool__(self) -> bool:
        return bool(self._value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        def _validate(v: Any) -> Secret[Any]:
            return v if isinstance(v, Secret) else Secret(v)

        def _serialize(v: Secret[Any]) -> str:
            return "<redacted>"

        return core_schema.no_info_plain_validator_function(
            _validate,
            serialization=core_schema.plain_serializer_function_ser_schema(_serialize),
        )
