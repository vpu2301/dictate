# Evidence-backend ADR index

ADRs record decisions whose reversal would be expensive (rule A5). Template:
Date / Status / Deciders / Context / Decision / Consequences / Alternatives
considered / Trigger conditions for revisiting. ≤ 2 pages. Numbers are
monotonic and never reused. Platform-wide decisions stay in
`../medical-dictation-backend/docs/adr/`.

| # | Title | Status |
|---|---|---|
| 0001 | [Sibling workspace with path-level platform inheritance](0001-workspace-placement-and-platform-inheritance.md) | Accepted |
| 0002 | [Segment taxonomy and the frozen contract enums](0002-segment-taxonomy-and-contract-enums.md) | Accepted (clinical countersign pending) |
| 0003 | [Index technology: pgvector + OpenSearch](0003-index-technology-pgvector-plus-opensearch.md) | Accepted |
| 0004 | [Hybrid retrieval: RRF + reranking](0004-hybrid-retrieval-rrf-and-reranking.md) | Accepted (scoring constants countersign pending) |
| 0005 | [Self-hosted web agent behind a single egress proxy](0005-web-agent-and-egress-proxy.md) | Accepted |
| 0006 | [Generator selection: the Gemma 3 family](0006-generator-model-selection.md) | Accepted (synthesis-quality countersign pending) |
