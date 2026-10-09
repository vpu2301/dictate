# Sprint 21 — Explore (§0)

Date: 2026-08-12 · Author: backend engineer #1 · Status: **complete — gates the rest of the sprint**

This file records the §0 discovery the sprint plan mandates: the measured
telemetry gap, the licence decisions, the review-budget arithmetic, and the
revised corpus target. Everything below was measured against the live dev
stack (`make dev-up`, migrations through `0080`) on 2026-08-12.

---

## 1. Renumbering (the plan's slots were stale)

The sprint plan was written against an older tree. Actual free slots:

| Plan says | Actually is | Why |
| --- | --- | --- |
| ADR-0030 / ADR-0031 | **ADR-0043 / ADR-0044** | ADRs run through `0042-http2-post-fallback-closed.md` |
| migrations 0035–0038 | **0081–0084** | migrations run through `0080_marketing_contact_sales_notice.sql` |

All references to "ADR-0030/0031" in the plan map to ADR-0043 (corpus
governance) and ADR-0044 (LLM-assisted review).

## 2. `data-sources.md` does not exist

The plan's §0.1 says "read `data-sources.md` in this folder and close its §5
action items". **No such file exists anywhere on this machine** — the sprint
folder never landed in the repo; only the plan text was handed over. The
licence register below is reconstructed from the sources the plan itself
names (§3: ДРЛЗ, formulary, НК 025) and needs a countersign from the DPO /
tech lead before the importers' output ships in a release.

### Licence register (reconstructed)

| Source | What | Licence basis | Verdict |
| --- | --- | --- | --- |
| **ДРЛЗ** (Державний реєстр лікарських засобів) | drug names, forms, doses | Published as open data on data.gov.ua under the Ukrainian open-data regime (КМУ №835), CC BY 4.0-compatible: free reuse with attribution | **OK to import.** Record dataset version + SHA in `source_ref`; attribute in the release manifest. |
| **Державний формуляр лікарських засобів** (МОЗ) | formulary drug list | Public МОЗ normative publication | **OK to derive phrases from.** We ship derived phrases, not the document. |
| **НК 025** (МКХ-10-АМ-based classifier) | diagnosis terms + ICD codes | State classifier, public normative document | **OK to derive phrases from.** Do **not** redistribute the raw classifier dump in a release artifact — accepted phrases only. |
| Mined report n-grams | clinician phrasing | Internal PHI-derived data | Governed by ADR-0043/0044: k-anonymity ≥5 authors ≥2 tenants, PII drop, in-perimeter LLM only, DPO sign-off on the query text before first run. |

Importers ship this sprint; **first production import runs only after the
countersign** (`docs/signoffs/sprint-21-dpo.md`).

## 3. The measured telemetry gap — and what it can't tell us

Queried live (`autocomplete_telemetry`, all partitions, 2026-08-12):

| Metric | Value |
| --- | --- |
| Total telemetry events | **8** |
| Distinct scrubbed prefixes | **5** |
| Prefixes with zero accepts | 1 of 5 |
| Event split | 4 `shown_only`, 4 `accepted` |

**Conclusion: there is no pilot-scale telemetry.** Eight dev-session events
cannot define a corpus target, a coverage replay set, or a "% of real
prefixes covered" number. Pretending otherwise would be exactly the
"cache hit ratio: meaningful or theatre?" failure the sprint-05 retro warns
about (retro prompt #8). So:

- The **gap miner ships as standing infrastructure** (`corpus-forge gaps`),
  wired and tested, and becomes the work-queue generator the moment pilot
  telemetry accumulates.
- The **coverage@3 replay set** for the v1 release is the union of (a) the 5
  real prefixes we do have, and (b) a fixed synthetic prefix set derived from
  the accepted corpus itself (prefix-truncations of accepted phrases, which
  measures self-consistency of trie + ranking, not clinical coverage — the
  report labels it as such honestly).
- The corpus target is set from the **review budget and the quota matrix**,
  not from telemetry (§5 below).

## 4. Corpus + eval baseline (what exists today)

- `autocomplete_phrases`: **30 rows** (20 uk / 10 en), all `source='system'`
  seeds from migration 0026. Language CHECK is `uk|en` — German is out of
  scope for autocomplete (unchanged from sprint 20's decision).
- Specialties in seeds: cardiology, general, endocrinology, radiology.
- Template library: 21 templates (18 uk / 3 en); dominant sections:
  `diagnosis` (13), `plan` (12), `anamnesis` (11), `examination` (9),
  `investigations` (8) — these five, plus `findings`/`impression` for
  radiology, define the quota-matrix section axis.
- Report data available to the miner in dev: 353 report versions over 65
  reports, **4 distinct authors, 1 tenant** → the k-anonymity gate
  (≥5 authors ∧ ≥2 tenants) passes **nothing** in dev. That is correct
  behaviour, not a bug; miner correctness is proven by unit/integration
  fixtures, and real mining runs on the pilot DB.
- **Report text is plaintext in Postgres under RLS** (`report_versions.rendered_text`,
  with FTS over it; migration 0031 header documents the model — envelope
  encryption applies to object-storage blobs, not relational columns). The
  miner therefore reads via `tenant_connection()` SQL, decrypts nothing, and
  writes nothing to disk.
- Eval corpus v1: **8 placeholder utterances** of the planned 120, synthetic
  tone audio, WER 1.0 plumbing-only. `run_per_section_wer.py` exists but its
  fixtures dir (`scripts/eval/fixtures/sprint-06-cardiology`) is **missing**,
  and it duplicates WER math instead of importing `wer_lib` (tokenisers
  differ — numbers aren't comparable across the two scripts). Both gaps are
  in scope for §7/§8 of the plan.
- ASR prompts: global `medical_prompts` (language × specialty, 21 rows, no
  section column, no RLS by design) + per-section `asr_prompt` inside
  template JSONB (224-token budget enforced by `scripts/validate-templates.py`
  via tiktoken). Prompt derivation targets both stores.

## 5. Review budget → corpus target

Constraint (plan §0.3): **one part-time clinician, ~8 hours ≈ 1,900
keyboard decisions at 15 s/decision** — measured, not assumed, via
`corpus_reviews.latency_ms`.

Budget allocation for the sprint:

| Consumer | Decisions |
| --- | --- |
| Jury calibration (blind, stratified) | 200 |
| Tier-3 mandatory human review | ≤ 1,000 |
| Tier-1 spot audit (5%) + tier-2 spot audit (10%) | ≤ 500 |
| Reserve (re-review after jury version bumps, appeals) | ~200 |

At the plan's expected tier split (~65/25/10), a **10k-candidate intake**
saturates this budget. Working backwards with dev-realistic accept rates:

> **Corpus target for release v1.0.0: ~2,500–3,000 accepted phrases**
> (≈ 2,000 uk / 600 en), distributed per the quota matrix
> (`infra/seeds/corpus/quota.yaml`) over
> (uk, en) × (cardiology, radiology, endocrinology, internal/family medicine, general)
> × (diagnosis, plan, anamnesis, examination, investigations, findings/impression)
> × length buckets.

Not 10k. The plan's own principle applies: "if 400 phrases would cover 80%
of real prefixes, say so and ship 400" — we have no telemetry evidence that
10k is needed, and 3k is the largest corpus the human-review budget can
certify at the mandated quality bar (≥80% useful, **0 harmful**). Scaling
past that is a future sprint triggered by real gap-miner output, not a
round number.

## 6. Tooling reality check (deviations the ADRs must own)

- **kenlm-uk** (plan §4 fluency filter): not in the repo, and a Ukrainian LM
  binary is a model artifact we don't have pinned. Decision: the fluency
  gate is an **optional, pluggable stage** — enabled when `MDX_CORPUS_KENLM_MODEL`
  points at a model file, otherwise replaced by a deterministic heuristic
  pre-filter (token-count bounds, character-class sanity, repetition caps)
  that is unit-tested. ADR-0044 records this; the release manifest records
  which filter ran.
- **LanguageTool UK + dict_uk/VESUM** (plan §6 morphology): Java service +
  large dictionaries, not in the venv. Decision: validator v2 gains a
  `--morphology` stage that talks to a LanguageTool HTTP server when
  `MDX_LANGUAGETOOL_URL` is set (with the medical allowlist file); CI runs
  the stage in skip-with-warning mode until the server is added to the dev
  stack. The uk-apostrophe, Cyrillic-ratio and language-ID checks are pure
  Python and always on.
- **LLM jury, in-perimeter path**: sprint 15's generation-service fronts a
  local llama.cpp/Ollama backend (`MDX_GEN_BASE_URL`, gemma3:1b). The jury
  drives the **same local backend directly** (its own base-URL setting, same
  deployment) for PHI-derived candidates; the generation-service HTTP API
  itself is ghost-text-shaped (24-token cap) and unsuitable. External API
  (Anthropic) is permitted **only** for `source_kind ∈ {terminology, generated, authored}`
  — enforced in code by `source_kind`, with a must-raise test (ADR-0044).
- **PII scrubber reuse**: corpus-forge may not import `autocomplete_service`
  (service→service imports are forbidden). Following the existing
  `validate-autocomplete-corpus.py` precedent, the 6 patterns are
  re-declared in corpus-forge with a **drift-guard test** that reads the
  autocomplete-service source and fails if the pattern sets diverge.

## 7. Retro items this sprint answers

- sprint-05 #1 ("number normalizer embarrassed?") → adversarial eval subset
  *numbers/doses/units* + `risk_flags='dose'` tier-3 routing.
- sprint-05 #6 (abbreviation policy vs muscle memory) → *abbreviations*
  subset + `abbrev` risk flag.
- sprint-05 #8 (cache-hit theatre) → coverage@3 measured on a fixed replay
  set per release, labelled honestly (see §3).
- sprint-13 retro ("there is no sprint-05 TP/FP corpus") → *voice commands
  mid-dictation* subset.

## 8. Decisions locked by this explore

1. Corpus target v1.0.0: **~3k accepted phrases**, quota-matrixed; ceiling is
   the review budget, floor is quota coverage ≥70% per cell.
2. `corpus-forge` is a new uv-workspace member at `services/corpus-forge`
   (CLI-only, no HTTP surface; precedent: asr-worker is a non-HTTP service),
   registered in import-linter with a `cli → domain → adapters` contract.
3. Migrations 0081–0084; ADR-0043/0044; audit kinds `corpus.*` registered by
   corpus-forge.
4. Serving-path change is exactly one predicate: `fetch_corpus()` in
   autocomplete-service `repository.py` gains `AND review_state='accepted'`;
   rows written by the existing phrases API default to
   `source_kind='authored', review_state='accepted'` so user/tenant phrases
   keep working unchanged.
5. The v1 release replay set + its "synthetic, self-consistency-only" caveat
   goes in `docs/eval/sprint-21-coverage.md`; the ≥80%-useful / 0-harmful
   gate is judged by the clinician on that set.
