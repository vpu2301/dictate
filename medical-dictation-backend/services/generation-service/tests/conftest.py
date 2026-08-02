"""generation-service test scaffolding."""

import os

# Disable OTel before any app import.
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("ENVIRONMENT", "test")
