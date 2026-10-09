"""Centralized, typed configuration — the only module that touches the env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    # Operator DSN. Mining is cross-tenant by construction (k-anonymity needs
    # ≥2 tenants), so this is NOT an app_role/tenant_connection DSN — dev uses
    # the local superuser; production uses a dedicated read-only miner role
    # (see docs/runbooks/corpus.md).
    corpus_dsn: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/medical_dictation",
        alias="MDX_CORPUS_DSN",
    )

    # Audit chain (libs/audit AuditWriter, audit_writer role). Optional in
    # dev — when unset, corpus events are logged but not chained.
    audit_dsn: str = Field(default="", alias="MDX_CORPUS_AUDIT_DSN")

    # ── LLM jury (ADR-0044) ──────────────────────────────────────────
    # In-perimeter backend: the same llama.cpp/Ollama deployment that backs
    # generation-service (sprint 15). The ONLY judge for PHI-derived text.
    llm_backend: str = Field(default="llamacpp", alias="MDX_CORPUS_LLM_BACKEND")
    llm_base_url: str = Field(default="http://localhost:8089", alias="MDX_CORPUS_LLM_BASE_URL")
    llm_model: str = Field(default="gemma3:1b", alias="MDX_CORPUS_LLM_MODEL")

    # External API — permitted only for public-data candidates, enforced by
    # source_kind in domain/jury.py (never by this flag alone).
    external_api_key: str = Field(default="", alias="MDX_CORPUS_EXTERNAL_API_KEY")
    external_model: str = Field(
        default="claude-sonnet-5", alias="MDX_CORPUS_EXTERNAL_MODEL"
    )

    jury_prompt_version: str = Field(default="v1", alias="MDX_CORPUS_JURY_PROMPT_VERSION")

    # ── Optional quality tooling (EXPLORE §6) ────────────────────────
    kenlm_model_path: str = Field(default="", alias="MDX_CORPUS_KENLM_MODEL")
    languagetool_url: str = Field(default="", alias="MDX_LANGUAGETOOL_URL")

    # ── Mining gates (ADR-0043 §7; changing these is an ADR amendment) ─
    k_min_authors: int = Field(default=5, alias="MDX_CORPUS_K_MIN_AUTHORS")
    k_min_tenants: int = Field(default=2, alias="MDX_CORPUS_K_MIN_TENANTS")
    min_frequency: int = Field(default=3, alias="MDX_CORPUS_MIN_FREQUENCY")


def load_settings() -> Settings:
    return Settings()
