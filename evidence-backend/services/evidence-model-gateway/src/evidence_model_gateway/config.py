"""Config — the sole env surface of the gateway (rule E8)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVA_GATEWAY_", extra="ignore")

    # embed.dense backing model. Pin lives in docs/models/PINS.md; a local
    # directory (baked image / HF cache export) takes precedence over the hub
    # id so offline images never resolve at runtime (rule BE6).
    embed_model_id: str = "BAAI/bge-m3"
    embed_model_dir: str | None = None
    embed_dim: int = 1024
    device: str = "cpu"

    # rerank backing model (S03). Disable to run an embed-only gateway.
    rerank_enabled: bool = True
    rerank_model_id: str = "BAAI/bge-reranker-v2-m3"
    rerank_model_dir: str | None = None
    max_rerank_texts: int = Field(default=64, ge=1)

    # ── generation roles (S04) ──────────────────────────────────────────
    # `generator.fast` (triage classifier, intent extraction) and
    # `generator.heavy` (clinical synthesis) are served from a self-hosted
    # llama-server/Ollama process (rule LM1). Disable when running an
    # embed/rerank-only gateway — readyz asserts every ENABLED role's backend
    # (rule BE6), so a dead generator makes the gateway honestly unready.
    generation_enabled: bool = True
    gen_backend: str = "llamacpp"  # llamacpp | ollama
    gen_fast_base_url: str = "http://localhost:8081"
    gen_fast_model_id: str = "google/gemma-3-4b-it"
    gen_heavy_base_url: str = "http://localhost:8082"
    gen_heavy_model_id: str = "google/gemma-3-12b-it"
    gen_timeout_s: float = Field(default=120.0, gt=0)

    # Request caps (gateway-enforced budgets, rule LM2 discipline).
    max_texts_per_request: int = Field(default=256, ge=1)
    max_text_chars: int = Field(default=8192, ge=1)
    # LM2: clinical synthesis runs at temperature ≤ 0.2. The cap is enforced
    # here so no caller can raise it — a request above it is clamped, not
    # rejected, because refusing would turn a policy into an outage.
    max_temperature: float = Field(default=0.2, ge=0.0)
    max_prompt_chars: int = Field(default=120_000, ge=1)
    max_generate_tokens: int = Field(default=2048, ge=1)

    # Optional shared bearer token for service-to-service calls; None in
    # local dev (gateway is never exposed on a public interface).
    service_token: str | None = None

    testing: bool = False
