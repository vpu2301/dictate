"""nlp-service — Sprint-05 NLP post-processing pipeline."""

# Bumped in sprint 13 (ADR-0028) when the ``field_extraction`` stage was
# inserted between ``abbreviation`` and ``confidence``. The version is
# part of the idempotence key, so a bump invalidates cached results;
# historical sessions replay under the version they were processed with.
PIPELINE_VERSION: str = "nlp-v1.1.0"
