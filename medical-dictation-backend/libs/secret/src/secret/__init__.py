"""Typed Secret[T] wrapper.

A Secret never participates in repr, str, format, f-string interpolation, JSON
serialisation, pickling, or copying. Reading the underlying value is explicit
via .value(). See ADR-0003 for design rationale and KMS migration plan.
"""

from .secret import Secret

__all__ = ["Secret"]
