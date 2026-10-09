"""corpus-forge — the clinical corpus pipeline (sprint 21).

Sources → `corpus_candidates` → tier routing → review (jury/human) →
promote → `autocomplete_phrases` → immutable release. Governance is
ADR-0043; the LLM-review boundary is ADR-0044.
"""

__version__ = "0.1.0"
