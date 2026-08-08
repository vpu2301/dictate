"""Shared content-safety screens (rule LM4).

Retrieved content — a corpus document at ingest time, a fetched web page at
query time — is **data, not instructions**. The same screen must judge both,
so it lives in a leaf lib rather than in whichever service happened to need
it first (`evidence-ingest` owned it through S02/S03; `evidence-websearch`
joined in S04, and rule E2 forbids one service importing another).

Leaf package: stdlib only.
"""

from .identifiers import identifier_shaped, is_identifier_shaped
from .injection_screen import InjectionHit, screen_text

__all__ = [
    "InjectionHit",
    "identifier_shaped",
    "is_identifier_shaped",
    "screen_text",
]
