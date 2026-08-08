"""evidence-model-gateway — role-addressed self-hosted model serving (:8015)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from observability import PROBLEM_CONTENT_TYPE, register_exception_handlers
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .embedder import DenseEmbedder
from .generator import GenerationBackend, GenerationBackendError, build_backend
from .registry import RoleDescriptor, RoleKind, RoleRegistry
from .reranker import Reranker


def _problem(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_CONTENT_TYPE,
        content={
            "type": f"urn:evidentia:problem:{code}",
            "title": code,
            "status": status,
            "detail": detail,
            "code": code,
        },
    )


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    texts: list[str] = Field(min_length=1)


class EmbedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    model_id: str
    dim: int
    vectors: list[list[float]]


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    query: str
    texts: list[str] = Field(min_length=1)


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    model_id: str
    # Raw cross-encoder logits, aligned with the request's texts order.
    scores: list[float]


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    prompt: str = Field(min_length=1)
    max_tokens: int = Field(default=512, ge=1)
    # Clamped to settings.max_temperature (LM2) — callers cannot raise it.
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    stop: list[str] | None = None
    # JSON-constrained decode: the backend is told to emit only text matching
    # this schema (llama.cpp `json_schema` / Ollama `format`).
    json_schema: dict[str, Any] | None = None


class GenerateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    model_id: str
    text: str
    finish_reason: str
    # The temperature actually used after the LM2 clamp.
    temperature: float


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    registry = RoleRegistry()
    embedder = DenseEmbedder(
        model_source=settings.embed_model_dir or settings.embed_model_id,
        device=settings.device,
        expected_dim=settings.embed_dim,
    )
    registry.register(
        RoleDescriptor(
            name="embed.dense",
            kind=RoleKind.embedding,
            model_id=settings.embed_model_id,
            dim=settings.embed_dim,
        )
    )
    reranker: Reranker | None = None
    if settings.rerank_enabled:
        reranker = Reranker(
            model_source=settings.rerank_model_dir or settings.rerank_model_id,
            device=settings.device,
        )
        registry.register(
            RoleDescriptor(
                name="rerank",
                kind=RoleKind.rerank,
                model_id=settings.rerank_model_id,
            )
        )

    generators: dict[str, GenerationBackend] = {}
    if settings.generation_enabled:
        for role_name, base_url, model_id in (
            ("generator.fast", settings.gen_fast_base_url, settings.gen_fast_model_id),
            ("generator.heavy", settings.gen_heavy_base_url, settings.gen_heavy_model_id),
        ):
            generators[role_name] = build_backend(
                backend=settings.gen_backend,
                base_url=base_url,
                model_id=model_id,
                timeout_s=settings.gen_timeout_s,
            )
            registry.register(
                RoleDescriptor(name=role_name, kind=RoleKind.generation, model_id=model_id)
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not settings.testing:
            embedder.load()
            if reranker is not None:
                reranker.load()
        yield
        for backend in generators.values():
            await backend.aclose()

    app = FastAPI(title="evidence-model-gateway", lifespan=lifespan)
    register_exception_handlers(app)
    app.state.settings = settings
    app.state.registry = registry
    app.state.embedder = embedder

    async def require_service_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if settings.service_token is None:
            return
        if authorization != f"Bearer {settings.service_token}":
            raise HTTPException(status_code=401, detail="invalid service token")

    @app.exception_handler(HTTPException)
    async def http_problem(_: Request, exc: HTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "unknown_role", 422: "invalid_request"}.get(
            exc.status_code, "error"
        )
        return _problem(exc.status_code, code, str(exc.detail))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "evidence-model-gateway"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Readiness asserts the accelerator/models (rule BE6): not ready until
        # every registered role's pinned model is loaded.
        if not embedder.loaded:
            return _problem(503, "model_not_loaded", "embed.dense model is not loaded yet")
        if reranker is not None and not reranker.loaded:
            return _problem(503, "model_not_loaded", "rerank model is not loaded yet")
        # A registered generation role whose backend is down means the gateway
        # cannot serve what it advertises (rule BE6 — readiness asserts the
        # serving stack, it does not paper over it).
        for role_name, backend in generators.items():
            if not await backend.ready():
                return _problem(503, "model_not_loaded", f"{role_name} backend is not reachable")
        return JSONResponse(
            {
                "status": "ready",
                "roles": [d.name for d in registry.list()],
                "embed_model": embedder.model_source,
                "rerank_model": reranker.model_source if reranker else None,
                "device": settings.device,
            }
        )

    @app.get("/roles", dependencies=[Depends(require_service_token)])
    async def roles() -> list[dict[str, str | int | None]]:
        return [
            {"name": d.name, "kind": d.kind.value, "model_id": d.model_id, "dim": d.dim}
            for d in registry.list()
        ]

    @app.post("/v1/embed", dependencies=[Depends(require_service_token)])
    async def embed(body: EmbedRequest) -> EmbedResponse:
        descriptor = registry.get(body.role)
        if descriptor is None or descriptor.kind is not RoleKind.embedding:
            raise HTTPException(status_code=404, detail=f"unknown embedding role {body.role!r}")
        if len(body.texts) > settings.max_texts_per_request:
            raise HTTPException(
                status_code=422,
                detail=f"too many texts (max {settings.max_texts_per_request})",
            )
        for i, text in enumerate(body.texts):
            if len(text) > settings.max_text_chars:
                raise HTTPException(
                    status_code=422,
                    detail=f"texts[{i}] exceeds {settings.max_text_chars} chars",
                )
        if not embedder.loaded:
            raise HTTPException(status_code=503, detail="model not loaded")
        vectors = await embedder.embed(body.texts)
        return EmbedResponse(
            role=body.role,
            model_id=descriptor.model_id,
            dim=descriptor.dim or len(vectors[0]),
            vectors=vectors,
        )

    @app.post("/v1/rerank", dependencies=[Depends(require_service_token)])
    async def rerank(body: RerankRequest) -> RerankResponse:
        descriptor = registry.get(body.role)
        if descriptor is None or descriptor.kind is not RoleKind.rerank:
            raise HTTPException(status_code=404, detail=f"unknown rerank role {body.role!r}")
        if len(body.texts) > settings.max_rerank_texts:
            raise HTTPException(
                status_code=422, detail=f"too many texts (max {settings.max_rerank_texts})"
            )
        if reranker is None or not reranker.loaded:
            raise HTTPException(status_code=503, detail="rerank model not loaded")
        scores = await reranker.score(body.query, body.texts)
        return RerankResponse(role=body.role, model_id=descriptor.model_id, scores=scores)

    def _generation_target(
        body: GenerateRequest,
    ) -> tuple[RoleDescriptor, GenerationBackend, float]:
        descriptor = registry.get(body.role)
        if descriptor is None or descriptor.kind is not RoleKind.generation:
            raise HTTPException(status_code=404, detail=f"unknown generation role {body.role!r}")
        backend = generators.get(body.role)
        if backend is None:
            raise HTTPException(status_code=503, detail=f"role {body.role!r} is not served")
        if len(body.prompt) > settings.max_prompt_chars:
            raise HTTPException(
                status_code=422,
                detail=f"prompt exceeds {settings.max_prompt_chars} chars",
            )
        return descriptor, backend, min(body.temperature, settings.max_temperature)

    @app.post("/v1/generate", dependencies=[Depends(require_service_token)])
    async def generate(body: GenerateRequest) -> GenerateResponse:
        descriptor, backend, temperature = _generation_target(body)
        try:
            result = await backend.generate(
                prompt=body.prompt,
                max_tokens=min(body.max_tokens, settings.max_generate_tokens),
                temperature=temperature,
                stop=body.stop,
                json_schema=body.json_schema,
            )
        except GenerationBackendError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return GenerateResponse(
            role=body.role,
            model_id=result.model_id,
            text=result.text,
            finish_reason=result.finish_reason,
            temperature=temperature,
        )

    @app.post("/v1/generate/stream", dependencies=[Depends(require_service_token)])
    async def generate_stream(body: GenerateRequest) -> StreamingResponse:
        descriptor, backend, temperature = _generation_target(body)

        async def events() -> AsyncIterator[bytes]:
            # A backend failure mid-stream is reported as a terminal `error`
            # frame; the HTTP status is already 200 by then, so swallowing it
            # would look like a successful empty generation.
            try:
                async for token in backend.stream(
                    prompt=body.prompt,
                    max_tokens=min(body.max_tokens, settings.max_generate_tokens),
                    temperature=temperature,
                    stop=body.stop,
                ):
                    yield b"data: " + json.dumps({"token": token}).encode() + b"\n\n"
            except GenerationBackendError as exc:
                yield b"data: " + json.dumps({"error": str(exc)}).encode() + b"\n\n"
                return
            yield b"data: " + json.dumps({"done": True}).encode() + b"\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
