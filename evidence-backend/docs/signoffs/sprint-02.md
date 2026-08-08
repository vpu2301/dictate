# EVA-S02 — Sign-off register

## Tech lead

| Artifact | Verification | Status |
|---|---|---|
| evidence-ingest pipeline (5 stages, resumable) | live E2E: 6 docs done, corrupt dead-lettered, interrupted job resumed; 41 unit tests; `make ci` green | ⏳ |
| evidence-model-gateway v0 + libs/models | 12 tests; live BGE-M3 embed (1024-dim, uk text); readyz asserts model | ⏳ |
| Migration 0068 | up→down→up ×3; check-rls 51 tables | ⏳ |
| CLI + runbook | live transcript in sprint-02.md; runbook committed | ⏳ |

## Security

| Artifact | Verification | Status |
|---|---|---|
| Injection screen (LM4) | 12/12 probes quarantined, 0 false positives; live quarantine→reject audited | ⏳ |
| Global-tenant read amendment | writes stay tenant-strict (policy WITH CHECK unchanged); operator-only global scope | ⏳ |
| Virus scan fail-closed | clamd INSTREAM exercised live; disabled ⇒ startup WARNING (BE7) | ⏳ |
| E12/E8 gates | negative proofs (planted torch import / os.getenv) | ⏳ |
| XXE safety | lxml hardened parser; entity-leak probe test | ⏳ |

## Clinical advisor

| Artifact | Verification | Status |
|---|---|---|
| Chunking review (50-doc checklist, spec D9) | ⏳ pending the real corpus bulk load — fixtures reviewed by construction | ⏳ open |

## SRE

| Artifact | Verification | Status |
|---|---|---|
| Compose (opensearch+icu, clamav arm64, bucket init) | containers healthy; icu installed on boot | ⏳ |
| Capacity numbers | sprint-02-loadtest.md (100k-scale index path) | ⏳ |
