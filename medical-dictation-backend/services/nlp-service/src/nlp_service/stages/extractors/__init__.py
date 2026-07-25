"""Focused extractors behind the single ``field_extraction`` stage.

One stage, one module per field-type family: sprint-13 step 04 ships
``choice``; step 05 adds numeric/date/diagnosis.
"""

from .choice import extract_choice, extract_multi_choice

__all__ = ["extract_choice", "extract_multi_choice"]
