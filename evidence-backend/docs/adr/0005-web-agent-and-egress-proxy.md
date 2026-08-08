# ADR-0005 — Self-hosted web agent behind a single egress proxy

- **Date:** 2026-08-06
- **Status:** Accepted
- **Deciders:** tech lead, security lead (evidence-backend), EVA-S04

## Context

Quick Search must answer from evidence that is *current*, which the corpus is
not between refresh cycles. That means reading the live web, which means the
clinical path acquires network egress — the one thing the platform has never
had. Three questions had to be answered together, because answering them
separately produces an incoherent design:

1. **How do we find candidate pages?** (search)
2. **How do we fetch them?** (egress)
3. **How do we keep patient data out of both?** (QS1)

Constraints already fixed by the rule set: no external model APIs, ever (LM1);
no external search APIs (P7 self-hosting); patient data never enters a web
query (QS1); on-prem deployments must have **no egress in the clinical path**
at all (DP2), so whatever we build has to degrade cleanly to corpus-only.

## Decision

**Search:** self-hosted SearXNG. It aggregates public engines without an API
key, runs in our own container, and returns JSON. `evidence-websearch` uses
only its JSON API.

**Egress: exactly one proxy, deny by default.** A squid instance
(`infra/compose/egress-proxy/`) is the sole route out of the stack. The
websearch service is configured with `EVA_EGRESS_PROXY_URL` and — this is the
load-bearing part — **fails closed without it**: with no proxy configured the
HTTP client is pointed at a discard port, so a coding mistake produces failed
fetches rather than a quiet direct connection. SearXNG's own upstream calls go
through the same proxy.

**Two allowlists, deliberately.** `web_domains` (migration 0070) is the
*product* allowlist a `knowledge_admin` edits at runtime: which sources may be
cited. `allowlist.txt` is the *network* allowlist an operator changes in a
deploy: which hosts may be reached. The product control can never widen the
network boundary. A CI test asserts the seeded product domains are all
network-reachable, and that no model-vendor host appears in either.

**QS1 in three layers**, because one layer is a promise and three are a
property:

| Layer | Mechanism | Fails how |
|---|---|---|
| 1a | import-linter contract: `evidence_websearch` may not import `evidence_models.snapshot` | CI |
| 1b | `make check-qs1`: snapshot type *names* are unnameable in the web path (catches `from evidence_models import PatientSnapshot`, which 1a cannot) | CI |
| 2 | type wall: `metasearch.build_query(intent: ClinicalIntent)` is the only query source; `evidence-retrieval`'s web connector refuses to search without an explicit `ClinicalIntent` | compile/runtime |
| 3 | `make qs1-taint`: canary tokens in a fake snapshot, asserted absent from every outbound URL, header and body | CI |

**Fetching is hardened in-process too** (allowlist, robots.txt, 3 MB cap, 10 s,
content-type allowlist, manual redirect re-validation, private-address
refusal). The proxy is the boundary; the fetcher is the second layer, because
a proxy misconfiguration must not become an SSRF.

**Pages are cached per tenant** (`web_pages` + two objects through
`EncryptedObjectStore`). Reopening an answer renders from the snapshot and
never re-fetches — which is both a correctness property (the citation still
says what it said) and a privacy one (reopening does not tell the publisher).

## Consequences

- The platform now has an egress path, and it is one auditable component with
  a deny log. Denials are alarmed: a spike is probing or drift.
- On-prem/no-egress deployments set `EVA_ANSWER_WEB_ENABLED=false` and get
  corpus-only answers with a `web_unavailable` flag — the same degrade path
  the failure tests exercise, not a separate build.
- SearXNG is ours to operate: engine breakage is our on-call problem, and
  upstream engines may rate-limit us. Accepted; the alternative violates P7.
- Two allowlists mean two edits when adding a domain. Deliberate friction:
  the reviewed table is the one in `docs/corpus/web-domains.md`.
- Web passages are never written to the persistent index, so retrieval
  determinism (S03) is unaffected: `web` results are excluded from the
  response cache because `accessed_at` is per-request evidence.

## Alternatives considered

- **Direct fetch from the service, no proxy.** Rejected: egress policy would
  live in application code, in every service that ever grows a fetch, and be
  unauditable as a unit. It also makes the on-prem "no egress" claim
  unverifiable.
- **Commercial search API** (Bing/Google/Brave/Tavily). Rejected by P7/LM1:
  it sends the query to a third party — a query built from clinical concepts
  that, in later sprints, are derived from a patient's chart. That is the
  exact shape of the leak QS1 exists to prevent, and no contract makes it
  acceptable in a HIPAA/GDPR posture.
- **Headless browser rendering** (Playwright) for JS-heavy pages. Rejected for
  v1: a browser is a much larger attack surface pointed at untrusted content,
  and the medical sources that matter (WHO, CDC, NICE, МОЗ) serve static HTML.
  Revisit if extraction failure rates show otherwise.
- **One allowlist** (DB only, proxy allows all). Rejected: a runtime product
  control would then define the network boundary.

## Trigger conditions for revisiting

- SearXNG maintenance burden exceeds the value (engine breakage more than
  monthly, or sustained upstream blocking).
- Extraction quality data shows a material share of allowlisted sources
  require JS rendering.
- A deployment needs egress *without* a proxy (e.g. an enterprise forward
  proxy already in place) — the client seam supports it, the policy does not
  yet.
- QS1 layer 3 (taint harness) starts failing once patient context lands in
  S05: that is the harness doing its job, and the design of the de-id gate
  between snapshot and intent becomes the ADR's live question.
