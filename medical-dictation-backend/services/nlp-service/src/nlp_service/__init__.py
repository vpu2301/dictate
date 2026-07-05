"""nlp-service — Sprint-05 NLP post-processing pipeline."""

# Bump on any change that alters pipeline output — participates in the
# idempotence cache key, so a bump invalidates stale cached responses.
# v1.1.0: added the spoken-punctuation normalization stage (Stage 2b).
PIPELINE_VERSION: str = "nlp-v1.1.0"
