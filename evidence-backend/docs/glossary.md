# Glossary (grows per sprint)

## EVA-S03

- **RRF (reciprocal-rank fusion)** — scale-free fusion of dense+lexical
  ranked lists (k=60); every passage keeps per-stage scores (dense, lexical,
  fused, rerank, final) for provenance and ablation.
- **EvidenceSource protocol** — the frozen connector interface (search /
  fetch_passage / health / search_many + descriptor); changes require an ADR
  and the freeze-fixture suite update.
- **Connector isolation** — per-connector timeout + exception capture; a
  broken source becomes `connector_meta.status='unavailable'`, never a 500.
- **Degraded response** — a served-but-diminished answer (lexical down,
  rerank over budget, requested connector unavailable); flagged, never cached.
- **Snapshot pinning** — retrieval restricted to a frozen corpus snapshot's
  member versions; the determinism contract's corpus axis.
- **BOOST_WEIGHT** — exponent damping authority/recency boosts so they nudge
  rather than override relevance (live S03 finding, ADR-0004).
- **Standing retrieval gate** — `make eval-retrieval` vs the adopted baseline
  in `evidence_eval.baseline` with protected thresholds (`eval/gates.yaml`).

## EVA-S02

- **Ingest job** — one source file's journey through the stage graph
  (`parsing → chunking → enriching → embedding → indexing`); the row's state
  names the NEXT stage, which is what makes a worker restart resumable.
- **Idempotence key** — sha256 over source URI + content hash; a re-submitted
  unchanged file is a no-op, a changed file becomes a new document version.
- **Quarantine** — holding pen for documents that trip the LM4 injection
  screen; only a knowledge_admin decision (approved/rejected) releases them.
- **Dead letter** — a job whose stage failed permanently (bad input, virus,
  retries exhausted): state `dead` + an `ingest_errors` row; the stream moves
  on (a poisoned file cannot block ingestion).
- **Global tenant** — the reserved nil uuid owning the shared corpus;
  readable by every tenant (migration 0068), writable only when explicitly
  scoped.
- **medical_icu** — the OpenSearch analyzer (icu_tokenizer + icu_folding +
  lowercase) serving uk/en lexical search in one field (ADR-0003).

## EVA-S01

- **Segment kind** — the closed six-value type of every answer statement
  (`evidence, patient_fact, interpretation, uncertainty, missing_info,
  next_step`); only `evidence` may read as established (rule P2, ADR-0002).
- **ET2** — the invariant that an `evidence` segment carries ≥1 citation,
  enforced twice: Pydantic validator (wire) + DB CHECK (storage).
- **Provenance chain** — the append-only `answer_provenance` record linking a
  question, the exact patient context consumed, retrieved passages, model/prompt
  pins and build versions (rule ET1); immutable via grants + trigger.
- **Snapshot hash** — canonical hash over a `PatientSnapshot`'s ordered facts;
  the value provenance stores instead of the PHI itself.
- **field_path** — a `PatientFact`'s address inside the snapshot
  (`labs.egfr`, `medications[0].dose`); the unit that `patient_fact_refs`
  and `consumed_fields` resolve against (grounding, rule CS3b).
- **Corpus snapshot** — an immutable labelled set of document versions; every
  answer records the snapshot it was computed against (rule ET3).
- **Additive-only policy** — released contract schemas may only gain optional
  properties/enum values/contracts; anything else needs a major bump + ADR
  (rule RC1; `lint_additive.py` vs the `evidence-contracts-v*` tag).
- **Dual-key RLS** — `questions`/`answers` policies require tenant AND owning
  user (`app.user_id` GUC): question history is user-private by schema.
- **knowledge_admin** — corpus-curation-only realm role; explicit `false` on
  every non-corpus action in the shared matrix.

## EVA-S04

- **QS1** — the hard invariant that patient data never enters a web query.
  Enforced in three layers: an import contract, a symbol-level gate
  (`make check-qs1`), a type wall (`build_query(intent: ClinicalIntent)`),
  and a runtime canary taint harness (`make qs1-taint`).
- **Type wall** — a boundary made of types rather than checks: the web query
  builder accepts only `ClinicalIntent`, so no snapshot-bearing value is
  *constructible* at the call site. The gate that cannot be forgotten.
- **Taint harness** — the runtime half of QS1: canary tokens planted in a
  fake snapshot, asserted absent from every outbound URL, header and body.
  Ships with a negative control so a harness that stops looking is visible.
- **Egress proxy** — the single, deny-by-default route out of the evidence
  stack (ADR-0005). Product allowlist (`web_domains`) and network allowlist
  (`allowlist.txt`) are separate on purpose: a runtime control can never
  widen the network boundary.
- **Trust tier** — how much a *source* is trusted (`international_organization`,
  `government`, `professional_society`, `guideline_registry`, `journal`,
  `other`). Orthogonal to `evidence_tier`, which grades the *study*.
- **Metadata-only source** — a domain whose pages may be cited (title, URL,
  access date) but never fetched for body text; paywalled/licensed
  publishers.
- **Ephemeral index** — the per-query, tenant-scoped, 30-minute Redis index of
  freshly fetched web chunks. Never written to the persistent vector index;
  what survives a query is the page snapshot, not the vectors.
- **Late source** — a web source that arrives after the corpus answer has
  already streamed. The header announces `late_sources_expected` so the SPA
  can render a placeholder before any content exists.
- **Segment grammar** — the one-segment-per-line output format
  (`[SUMMARY|kind] text [Sn]`) that lets the parser emit a `Segment` as each
  line completes, instead of after the whole generation.
- **Structural failure** — a dangling citation marker, an uncited `evidence`
  line, or unparseable output. One constrained retry, then
  `insufficient_basis`; never repaired by dropping the marker.
- **Constrained retry** — a retry that re-states only the violated rule. A
  retry that renegotiated the task would be a second chance at being
  differently wrong.
- **Reference phrasing** — question wording ("protocol for", "які
  рекомендації") that marks an emergency term as a lookup rather than a call
  for help. Suppresses the emergency rules — unless a strong immediacy
  marker ("негайно", "help") is also present.
- **Deflection** — a first-class answer state (`AnswerStatus.deflected`) with
  authored, never generated, safe messaging per reason code and locale.
