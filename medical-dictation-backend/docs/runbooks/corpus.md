# Runbook — clinical corpus pipeline (corpus-forge, sprint 21)

Governance: ADR-0043 (provenance/tiers/releases), ADR-0044 (LLM review +
PHI boundary). Sign-off state: `docs/signoffs/sprint-21-dpo.md`.

## What this is

An operator CLI (`services/corpus-forge`, entrypoint `corpus-forge`), not a
service. Pipeline: sources → `corpus_candidates` (staging) → tier routing →
review (jury/human) → `promote` → `autocomplete_phrases` → `release`
(immutable artifact + `corpus_releases` row). The serving trie reads only
`review_state = 'accepted'` rows — one predicate in autocomplete-service's
`fetch_corpus()`; there is no other serving path.

**Two ingest paths, one router.** Besides the CLI there is
`POST /corpus/candidates` (autocomplete-service, `corpus.contribute`) — the
console fill worksheet. Both run `libs/corpus_risk`: the same lexicon-backed
flagger and the same `route_tier`, so any risk flag means tier 3 whichever
door the phrase came through. It was NOT always so: migration 0088 filed
every console submission as tier 2 with no flags, which kept authored dose
and drug phrases out of the tier-3 human queue entirely and left that queue
looking permanently empty. Migration 0098 moved the routing to the caller and
made the SQL function refuse a tier/flags pair that contradicts the table.
The reviewed wordlists live in `libs/corpus_risk/src/corpus_risk/data/`
(they used to be under `infra/seeds/corpus/risk/`, which the service image
does not carry); `medical_allowlist.txt`, read only by `validate`, stayed
behind.

## Environment

| Variable | Meaning |
| --- | --- |
| `MDX_CORPUS_DSN` | operator DB DSN. Dev: local superuser. **Prod: a dedicated read-write role limited to `corpus_*` + `autocomplete_phrases`, plus read-only on `reports`/`report_versions`/`patients` for mining. Mining is cross-tenant by design (k-anonymity needs ≥2 tenants) — never a tenant-scoped app DSN.** |
| `MDX_CORPUS_AUDIT_DSN` | audit_writer DSN; unset ⇒ events logged, not chained (dev only) |
| `MDX_CORPUS_LLM_BACKEND` / `_BASE_URL` / `_MODEL` | in-perimeter jury backend (the sprint-15 llama.cpp/Ollama deployment) |
| `MDX_CORPUS_EXTERNAL_API_KEY` / `_MODEL` | optional external API — public-data candidates only; the PHI boundary is enforced in code regardless of this setting |
| `MDX_CORPUS_KENLM_MODEL` | optional kenlm-uk binary; unset ⇒ heuristic fluency filter (recorded in the release manifest) |
| `MDX_LANGUAGETOOL_URL` | optional morphology stage in `validate`; unset ⇒ stage skipped with a warning |
| `MDX_CORPUS_K_MIN_AUTHORS` / `_K_MIN_TENANTS` / `_MIN_FREQUENCY` | mining gates (5 / 2 / 3). Loosening is an ADR amendment. |

## Standard cycle (run from `medical-dictation-backend/`)

```bash
UV="uv run --project services/corpus-forge corpus-forge"

# 1. Sources → staging
$UV mine --language uk                      # needs DPO sign-off before prod
$UV gaps                                    # the work queue (zero-accept prefixes)
                                            # same rows over HTTP: GET /corpus/gaps
                                            # (autocomplete-service, corpus.review;
                                            #  shared query pinned by migration 0090)
$UV import --dataset drlz --dataset-version 2026-08-09 --file drlz.csv \
           --language uk --specialty general
$UV generate --language uk --specialty cardiology --section plan --count 50

# 2. Review
$UV jury --tier 2                           # local model; majority accept, split → tier 3
$UV jury --tier 1 --calibrated              # ONLY after the ≤2% calibration gate
$UV review --candidate-id <uuid> --reviewer <sub> --decision accept --latency-ms 12000

# 3. Ship
$UV promote                                 # staging → live corpus (idempotent)
$UV release --version v1.0.0 --notes "…"    # artifact + corpus_releases row
$UV validate --release-dir infra/seeds/corpus/releases/v1.0.0

# Deploy a published artifact into ANOTHER environment (idempotent seed
# job — releases are never migrations):
$UV release --version v1.0.0 --apply
# Rollback (rows stop serving; register stays immutable):
$UV release --version v1.0.0 --retire
# NOTE: --apply never un-retires rows — an incident-retired harmful phrase
# must not resurrect via a routine re-apply. Restoring a mistakenly-retired
# release is a deliberate operator UPDATE:
#   UPDATE autocomplete_phrases SET review_state='accepted', updated_at=now()
#   WHERE corpus_release='vX' AND review_state='retired' AND source='system';

# 4. Measure (gate: usefulness ≥80%, harmful = 0)
uv run --project services/autocomplete-service python scripts/eval/corpus_coverage.py \
    --release-dir infra/seeds/corpus/releases/v1.0.0 \
    --replay-set eval/replay/replay-set-v1.json \
    --marks eval/replay/marks-v1.0.0.csv \
    --history docs/eval/sprint-21-coverage.md
```

After `promote`, bump the trie cache: the rollup job's version-tag bump
covers it nightly; for immediate effect, `redis-cli DEL` the
`autocomplete:trie:*` keys or bump `autocomplete:tenant_phrase_version:*`.

## The WER eval corpus — record → publish → score (migrations 0089/0091)

This is a different corpus from the phrase corpus above: audio + gold text,
used to measure ASR, not to serve autocomplete. It is worked from the
console (`/company/corpus/record`), and no step below needs a terminal.

```
record a take ──► publish a snapshot ──► score it ──► WER on screen
   (0089)          (0091, PII-swept,      (asr-service,   per line,
                    immutable, hashed)     real model)     per subset
```

| step | route | permission |
|---|---|---|
| add a line (category + labels) | `POST /corpus/eval/script` | `corpus.contribute` |
| record it / re-record it | `PUT /corpus/eval/takes/{script_id}` | `corpus.review` |
| capture ad-hoc audio + write its gold text | `POST /corpus/eval/adhoc` | `corpus.contribute` |
| freeze the set | `POST /corpus/eval/publish` | `corpus.review` |
| score it | `POST /corpus/eval/runs` then `…/advance` until done | `corpus.review` **and** `asr.read`/`asr.write` |
| take it to git | `GET /corpus/eval/export?snapshot_id=…` | `corpus.review` |

Things that will otherwise surprise you:

* **Scoring needs a clinician.** `docs/auth/permissions.csv` withholds
  `asr.*` from tenant_admin (a PHI boundary, not an oversight), and the run
  transcribes through asr-service with the caller's own bearer. A
  tenant_admin can record, author and publish, and gets a single 403
  (`asr_permission_required`) on the scoring step. Nothing here mints a
  service credential to work around that.
* **The model is in every run row.** On a laptop asr-worker falls back to
  `tiny` on CPU; those numbers are plumbing-only, exactly as in
  `docs/eval/wer-methodology.md`. The release gate is still `large-v3` on
  the Linux/GPU rig via `make wer-eval-corpus`. Never compare runs whose
  `model` differs.
* **A run scores a snapshot, not the live takes.** Re-recording a line
  after publishing does not change what any existing run measured, and the
  snapshot's export refuses to substitute the new audio (409
  `take_drifted`) — publish a new version instead.
* **Publishing sweeps the whole set for PII**, not just what changed, and a
  finding refuses the publication outright. Same patterns as
  `scripts/eval/check_corpus_pii.py`, plus the telemetry scrubber's, so the
  CI gate cannot reject a set this accepted.
* **Ad-hoc capture demands an attestation.** It is the one path where the
  words are not known before the microphone opens; `no_patient_data` is
  refused when false and recorded in the audit trail
  (`corpus.eval_adhoc_captured`). Its utterances carry `capture: adhoc` in
  the corpus metadata so a reader knows the gold text was reconstructed.
* **The git corpus still exists.** Scoring from the console does not
  replace `eval/corpus/v1/` — export a snapshot and commit it when you want
  the nightly standing gate to see these utterances.

```bash
# After committing an exported snapshot into eval/corpus/v1/:
uv run python scripts/eval/build_corpus_manifest.py     # now recurses into subsets/
uv run python scripts/eval/check_corpus_pii.py
make wer-eval-corpus                                    # the real gate (rig)
```

### corpus-v2: dev/test sets, CSV import, comparable runs (migration 0092)

The pipeline above produces a WER. Corpus-v2 is what makes that WER
evidence: a normalised second score, confidence intervals, quarantined
hallucinations, and a record of how each run was measured. The methodology
is `docs/eval/wer-methodology.md` §The corpus-v2 scoring protocol; this is
the operating side of it.

| step | route | permission |
|---|---|---|
| preview a CSV of replicas | `POST /corpus/eval/import` (`dry_run: true`) | `corpus.contribute` |
| commit it | same, `dry_run: false` | `corpus.contribute` |
| every import, previews included | `GET /corpus/eval/imports` | `corpus.review` |
| the recording journal | `GET /corpus/eval/attempts` | `corpus.review` |
| log a discarded take | `POST /corpus/eval/takes/{id}/discard` | `corpus.review` |
| compare two runs | `GET /corpus/eval/compare?baseline=…&candidate=…` | `corpus.review` |

* **v1 is the holdout.** 0092 backfills every pre-existing line to
  `dataset='test'`; new lines default to `dev`. A run scores ONE set
  (`POST /corpus/eval/runs` takes `dataset`, defaulting to `test` so old
  clients keep measuring what they measured). Tune on dev; publish numbers
  from test.
* **Importing into `test` needs `allow_test: true`.** Without it every
  `set=test` row is rejected with `test_requires_confirmation`. This is the
  only guard between a routine import and a contaminated holdout, and the
  flag is recorded in the audit event.
* **Import previews by default.** `dry_run` is true unless you say
  otherwise; the preview walks the identical path and stops short of the
  inserts, so it cannot disagree with the commit about what is refused. The
  commit is one transaction — 86 rows or none — and is idempotent by id, so
  re-importing a corrected file adds only what is new.
* **Comparison refuses more than it accepts, on purpose.** Different sets,
  different snapshots or different `normalizer_version`s each give a 409
  naming what differs. Δ WER comes with a paired bootstrap CI; **if the CI
  straddles zero the change is not distinguishable from noise**, whatever
  the point estimate says. Quarantined utterances are excluded from both
  sides.
* **Flagged takes are a work queue, not a footnote.** `summary.flagged`
  lists every quarantined utterance with its reason. `known_hallucination`
  and `speech_too_short` mean re-record; they do not mean the model got
  worse.
* **A rules change invalidates comparisons.** Editing
  `autocomplete_service/data/eval_normalization_v1.json` without bumping
  `version` silently changes what every future normalised number means. Bump
  the version, and expect old runs to become incomparable on that metric —
  which is the honest outcome, not a bug.

```bash
# Import the 86 corpus-v2 replicas (preview, then commit) — the console's
# «Імпорт CSV» button does exactly this.
CSV=$(base64 < eval/corpus/v2/corpus-v2-replicas.csv)
curl -sX POST localhost:8007/corpus/eval/import -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"filename\":\"corpus-v2-replicas.csv\",\"csv_base64\":\"$CSV\",\"dry_run\":true}"
```

## ASR prompts

```bash
$UV prompts --language uk --specialty cardiology          # derive candidate (tf-idf, ≤224 tokens)
# record A/B fixtures → scripts/eval/fixtures/sprint-06-cardiology/
# run scripts/eval/run_per_section_wer.py for incumbent and candidate
$UV prompts --promote --incumbent-report a.json --candidate-report b.json
# exit 0 = delta_pp > 0, ship it; exit 1 = incumbent stays
```

## Rollout order (deployment plan)

1. Migrations 0081–0085 + trie predicate → dev; existing corpus still serves.
2. corpus-forge + review UI (`corpus.review`) → internal tenant only.
3. Release v1.0.0 → dev; coverage harness vs pre-release baseline.
4. Staging, 24 h "clinic day" k6 soak; autocomplete p99 ≤ 80 ms unchanged.
5. Production off-peak, one tenant first; watch
   `mdx_autocomplete_trie_size_bytes_histogram` (10k phrases ≈ 20× current corpus).

## Incidents

- **A harmful phrase is serving.** `UPDATE autocomplete_phrases SET
  review_state = 'retired', updated_at = now() WHERE id = …` (as operator),
  bump the trie cache version, then file the retro item — a harmful accept
  means the tier router or the jury version failed; find which via
  `review_engine` and `jury_votes` on the source candidate.
- **A jury version turns out bad.** Its accepts are identifiable:
  `SELECT … FROM autocomplete_phrases WHERE review_engine = 'jury:<model>:<ver>'`.
  Retire them, re-queue the source candidates
  (`UPDATE corpus_candidates SET review_state = 'candidate' WHERE …`), fix
  prompts under a NEW version directory (`infra/seeds/corpus/jury/v2/`).
- **The review queue is empty although phrases were just authored.** First
  check the tier they were filed at:
  `SELECT tier, risk_flags, count(*) FROM corpus_candidates WHERE
  source_kind = 'authored' AND review_state = 'candidate' GROUP BY 1, 2;`
  Tier 2 with `{}` on a phrase that plainly carries a dose or a drug means
  the router did not run — the service predates migration 0098, or was
  deployed without `libs/corpus_risk`. Tier 2 on genuinely clean phrases is
  correct and means the JURY is what has not run (`corpus-forge jury
  --tier 2`); those rows are decidable by hand at
  `/company/corpus/pipeline?state=candidate`, which is where the empty queue
  now points.
- **Mining audit shows an unexpected `query_sha256`.** The query text
  drifted from the DPO-signed version — stop mining, diff
  `adapters/mining.py` against the signed SHA, re-sign before the next run.
- **`corpus_reviews` insert fails with insufficient_privilege.** Someone
  attempted UPDATE/DELETE — that's the immutability trigger working, not a
  bug. The chain is append-only; corrections are new rows.

## Metrics to watch

`mdx_corpus_coverage_at_3` / `mdx_corpus_usefulness_at_3` /
`mdx_corpus_harmful_suggestions` (coverage harness textfile), and the
review latency distribution (`corpus_reviews.latency_ms`) — if the median
drifts far above 15 s/decision the review-budget arithmetic in
docs/sprint-21/EXPLORE.md §5 no longer holds and the tier thresholds need
re-cutting.
