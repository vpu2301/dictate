# ADR-0044: LLM-assisted corpus review — jury, calibration, and the PHI boundary

Date: 2026-08-12
Status: Accepted
Sprint: 21

## Context

The review budget is ~8 clinician-hours for the whole sprint (≈1,900
keyboard decisions at 15 s each). A ~10k-candidate intake needs machine
review for the bulk, but LLM review of PHI-derived text must not leave the
perimeter, and machine acceptance of clinical text must be earned by
measurement, not assumed. ADR-0043 defines the tiers this ADR operates on.

## Decision

### 1. The PHI boundary is enforced by `source_kind`, in code

> **Mined and telemetry-derived candidates are PHI-derived and are judged
> only by a self-hosted model inside our perimeter.** Only
> terminology-derived, template-generated and authored candidates — which
> originate from public open data — may touch an external LLM API.

- In-perimeter path: the same local llama.cpp/Ollama backend that serves
  sprint 15's generation-service (`MDX_CORPUS_LLM_BASE_URL`, default the
  gen backend's host). corpus-forge drives it directly; the
  generation-service HTTP API is ghost-text-shaped (24-token cap) and is
  not reused.
- External path (optional, Anthropic API): permitted **only** for
  `source_kind ∈ {terminology, generated, authored}`.
- The router raises `PHIBoundaryViolation` — it does not warn, log-and-
  continue, or consult a config flag — when a `mined` or `telemetry_gap`
  candidate is routed to an external client. A unit test asserts the
  raise. A config flag is not enough; someone will flip it at 2 a.m.

### 2. Jury mechanics

- Three independent votes per candidate: distinct prompt variants (and/or
  temperatures) on one model, each returning strict JSON
  `{verdict: accept|reject, reason, suggested_edit}`; malformed output
  counts as `reject` (fail-closed).
- Jury prompts are versioned files under `infra/seeds/corpus/jury/v<N>/`;
  `review_engine` records `jury:<model>:<prompt_version>` so a bad jury
  version can be identified and its accepts re-queued wholesale.
- Tier 1: unanimous accept → auto-accept (post-calibration only), 5%
  random sample to the human spot-audit queue. Tier 2: majority accept →
  accept, split → escalate to tier 3, 10% spot-audit. Tier 3: the jury
  may annotate but **never decides** — human decision mandatory.
- Every jury disagreement (non-unanimous vote) emits
  `corpus.jury_disagreement` audit.

### 3. Calibration gates auto-accept

Before any auto-accept runs: the clinician blind-reviews 200 stratified
candidates; the jury reviews the same 200. Auto-accept is enabled only if
the jury's **false-accept rate on clinician-rejected, tier-1-shaped items
is ≤ 2%**. Otherwise tier 1 collapses into tier 2 (human-sampled) and the
corpus target shrinks — 3k good phrases beat 10k with 200 bad ones, and
"0 harmful" is not negotiable. Results:
`docs/eval/sprint-21-jury-calibration.md`.

### 4. Generation is boxed

- Input: `(language, specialty, section, seed terms, accepted-phrase
  avoid-list, target count)`; output: strict JSON array, schema-validated
  with `extra="forbid"`; anything malformed is dropped, never repaired.
- All generated rows are `source_kind='generated'`, minimum tier 2;
  Ukrainian output minimum tier 2 unconditionally (case agreement, aspect,
  the `’` apostrophe are exactly what generation gets subtly wrong).
- Fluency pre-filter before any reviewer sees a row: kenlm-uk
  (bottom-decile perplexity dropped silently) **when**
  `MDX_CORPUS_KENLM_MODEL` is configured; otherwise a deterministic
  heuristic filter (length/character-class/repetition caps). The release
  manifest records which filter ran — the fallback is honest, not silent
  (EXPLORE §6).
- Batch dedupe at ingest against the accepted corpus and within-batch,
  using the same rapidfuzz Levenshtein-≤3 rule as the serve-time
  diversity guard.

## Consequences

- `review_engine` provenance makes machine acceptance auditable and
  revocable per jury version.
- The human stays the only authority on tier 3 and the only source of the
  calibration ground truth; `corpus_reviews.latency_ms` tells us by day 7
  whether the 15 s/decision assumption held.
- Running without a local LLM backend disables jury review of mined
  candidates entirely (they queue) — it never falls back to an external
  API.
- External-API usage is limited to public-data candidates and is optional;
  the pipeline is fully functional air-gapped.

## Alternatives rejected

- **One jury vote instead of three** — a single sample from the same
  model at the quality bar we need has no error estimate; three votes give
  a cheap unanimity/majority signal and a disagreement audit stream.
- **Config-flag PHI routing** — rejected per the boundary note above.
- **Auto-repairing malformed generator output** — repair invents text
  nobody reviewed upstream of a clinical cursor.
