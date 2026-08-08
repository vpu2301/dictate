# EVA-S04 — live end-to-end run (2026-08-07)

The S04 close-out item: real generators, real SearXNG, real allowlisted fetch,
against the real stack instead of fakes. **The pipeline works end to end. The
latency NFR is not met on developer hardware**, and this run establishes why.

Everything below was run on a 24 GiB Apple Silicon laptop with the platform
stack alongside. It is a dev record, not a gate result — the gate runs on the
rig (rule T4, ADR-0006).

## Verdict

| | |
|---|---|
| Pipeline end to end | **works** — `status: ok`, real citations |
| SSE contract | **confirmed** — `header → summary_segment* → source* → done` |
| Web agent | **works** — SearXNG → allowlist → fetch through the egress proxy |
| First-token NFR (≤ 3.5 s warm, ≤ 6 s cold) | **NOT MET** — 6.0–13.8 s warm, 9.0–11.4 s cold |

Reproduce with `make measure-latency`.

The warm figure is a range, not a number, and the spread is worth more than
either end: the same question on the same stack measured 11.9–13.8 s against
populated `questions`/`answers` tables and 6.0–6.1 s minutes later against
tables freshly recreated by the 0070 down/up cycle. Whatever that sensitivity
is, it is not the generator — and it means a single laptop measurement should
not be quoted as *the* number. Even the best of them misses the 3.5 s budget by
1.7×.

## Configuration

Off the ADR-0006 pins, deliberately and visibly:

| Role | ADR-0006 dev pin | This run |
|---|---|---|
| `generator.fast` | `gemma-3-4b-it` | same |
| `generator.heavy` | `gemma-3-12b-it` | **`gemma-3-4b-it`** |

`generator.heavy` is off-pin because the 12b does not fit. Measured: ~14 GiB
available against ~11.7 GiB of generators plus ~6 GiB of stack. Exceeding it
does not fail cleanly — the Docker engine OOM-killed `postgres`, `keycloak`,
`kafka` and the platform services while llama-server kept serving, so the stack
appears to collapse around a component that looks healthy. `make
run-generators-lite` is that configuration, and it prints an OFF-PIN banner so
no number measured this way is mistaken for the gate.

**Consequence: the numbers below are a floor, not the pin's number.** A 12B
heavy is slower than a 4B, so the real pin is worse on this hardware, not
better.

## Where the time goes

Stage trace of a successful answer (`answer_traces`, ms):

| Stage | ms | Note |
|---|---|---|
| `triage` | 1 460 | `generator.fast`, CPU |
| `intent` | 3 074 | `generator.fast`, CPU, 5 concepts |
| `plan` | 0 | |
| `retrieve_corpus` | 5 851 | 8 passages, `degraded=true` (rerank budget) |
| `synthesize` | 5 636 | 2 summary segments, 1 attempt |
| `retrieve_web` | 0 | 0 passages — see below |

First token (first `summary_segment` — the first thing a user can read) lands
at 11.9–13.8 s warm.

No single stage is the problem, which is the useful finding: ~4.5 s of
`generator.fast` calls, ~5.9 s of CPU embedding, ~5.6 s of synthesis. A 3.5 s
budget is not reachable by tuning any one of them on this hardware. The NFR is
a GPU-rig number and should be re-measured there, on the real pins.

## Two defects found by running it

Both were invisible to the unit suites because both are configuration
constants that only bite against a real CPU.

### 1. `local_connector_timeout_ms` = 600 ms starves retrieval on a dev CPU

The 600 ms default is the rig target. A dev-CPU BGE-M3 query embedding takes
~560–600 ms, so the corpus connector landed either side of its budget at
random — measured on the same corpus seconds apart:

```
562 ms → status ok,          21 candidates
604 ms → status unavailable,  0 candidates
```

Losing that race returns **no evidence**, so the answer pipeline correctly
deflects and every question comes back `insufficient_basis` with the canned
`missing_info` / `next_step` segments. It reads as an empty corpus or a broken
model. This is materially worse than the already-documented rerank-budget
degrade, whose fallback (RRF order, no reranking) is harmless.

Fixed by overriding to 8 000 ms in dev — in `make run-retrieval` and in the
compose entry, both with the rig value preserved in a comment. With the
override, retrieval reports `latency_ms: 4018`, i.e. corpus retrieval alone
exceeds the entire 3.5 s warm budget on this CPU.

### 2. `evidence-answer` could not be packaged

`pyproject.toml` force-included `src/evidence_answer/prompts` on top of
`packages = ["src/evidence_answer"]`, which already ships it. Hatchling refuses
the duplicate:

```
ValueError: A second file is being added to the wheel archive at the same
path: `evidence_answer/prompts/CHANGELOG.md`
```

Never seen because the service had only ever been installed editable
(`uv run --project`), which does not build a wheel. The first real packaging —
the Dockerfile — surfaced it. The redundant `force-include` is removed;
verified that all four prompt files still ship in the image.

## Web agent

Proven live against `/web/search` — SearXNG returned real results, the
allowlist filtered them, and the surviving URL was fetched through the squid
egress proxy:

```
pages:           pubmed.ncbi.nlm.nih.gov → paywalled / metadata_only_source
not_allowlisted: jamanetwork.com, uspreventiveservicestaskforce.org, openai.com, …
```

Zero web passages, and correctly so: the only allowlisted hit was PubMed, which
is a metadata-only source.

**Worth a look before the clinical countersign:** `jamanetwork.com` and
`uspreventiveservicestaskforce.org` are exactly the kind of source this product
should cite, and both are off the shipped 26-domain allowlist. That is a
coverage question for the trust-tier review, not a bug.

## Also verified in the same session

- **Migration 0070 cycles cleanly** on a live DB: `migrate-down` → `pending` →
  `migrate-up` → `applied`, and the platform's `make check-rls` passes
  afterwards (53 tables, RLS + FORCE).
- **DB-gated suite green against the live DB**, 26 passed / 7 skipped, before
  and after the cycle — including the dual-key RLS proofs
  (`test_question_history_is_user_private`, the tenant-isolation class) and the
  `web_domains` tenant-shadowing tests.
- **Both workspaces' gates green**: evidence `make ci` all green;
  platform `check-rls` green.

## Still open

- Re-measure on the rig with the real pins (`gemma-3-12b-it` dev /
  `gemma-3-27b-it` rig) and a GPU. **That is the NFR gate**; this run is not.
- `retrieve_web` returned 0 passages in 0 ms inside the answer pipeline while
  the same query against `/web/search` directly did reach the network. The
  late-web join is worth a closer look; not chased here.
- Retrieval quality remains plumbing-grade: 32 chunks indexed. The ≥100k bulk
  load (SPRINT-TODO S02) has to land before answer quality means anything.
- `answer_traces` shows `partial_reason: uncited_evidence_segment` on a normal
  run — the citation guard dropping an uncited segment. Expected behaviour;
  worth confirming the rate once the corpus is real.
