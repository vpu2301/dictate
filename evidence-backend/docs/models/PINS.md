# Evidence model pins

Every model the evidence stack serves is open-weight and self-hosted (rule
LM1) — no external model API, in any environment. Gateway roles resolve to
pins here; code never names a model (rule E12).

| Role | Model pin | Revision | Dim / params | Serving | Status |
|---|---|---|---|---|---|
| `embed.dense` | `BAAI/bge-m3` | `5617a9f` (HF main @ 2024-06) | 1024-dim, 568M params, multilingual (uk/en/de covered) | dev: sentence-transformers CPU via evidence-model-gateway; prod: vLLM/TEI on the GPU rig, weights baked into the offline image (BE6) | **active (EVA-S02)** |
| `generator.fast` | `google/gemma-3-4b-it` | HF main @ 2025-03 | 4B params, multilingual (uk/en/de) | dev: llama-server (GGUF Q4_K_M) on :8081; prod: vLLM on the rig | **active (EVA-S04)** — triage classifier, intent extraction |
| `generator.heavy` | `google/gemma-3-12b-it` (dev) / `google/gemma-3-27b-it` (rig) | HF main @ 2025-03 | 12B / 27B params | dev: llama-server on :8082; prod: vLLM on the rig | **active (EVA-S04)** — clinical synthesis, temp clamped ≤ 0.2 |
| `rerank` | `BAAI/bge-reranker-v2-m3` | HF main @ 2024-06 | 568M params, multilingual cross-encoder | dev: sentence-transformers CrossEncoder CPU via evidence-model-gateway; prod: rig | **active (EVA-S03)** |
| `verify.nli` | TBD (S06) | — | — | — | planned |
| `deid.ner` | TBD (S05) | — | — | — | planned |
| `drug.chat` / `drug.predict` | TxGemma (chat / predict variants) | — | — | rig | planned (S10) |

Rules:
- Changing a pin = PR to this file + standing-gate run (rule LM3 discipline).
- Offline images bake the weights directory; `MD`-style bare HF ids must never
  reach an offline container (the platform's ASR lesson — see platform
  CLAUDE.md gotchas).
- Dev Macs may resolve from the local HF cache; the readiness probe reports
  the loaded pin so drift is visible.
- Generator selection is **ADR-0006** (Gemma 3 family; Kimi K2 rejected for
  the pilot on serving budget, revisit trigger recorded there). The registry
  is role-addressed, so swapping the pin is a config change, not a refactor.
- The `generator.*` roles are served by a **separate process** (llama-server /
  vLLM), not in-process like the embedder — `EVA_GATEWAY_GENERATION_ENABLED=false`
  runs an embed/rerank-only gateway for retrieval-side work.
- Temperature is clamped gateway-side to `EVA_GATEWAY_MAX_TEMPERATURE`
  (default 0.2, rule LM2). The clamped value is echoed in the response and
  recorded in `answer_provenance.model_pins`.
