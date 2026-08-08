# Runbook — evidence-websearch (:8014)

The Quick-Search web agent: metasearch → allowlist → proxy fetch → extract →
injection screen → ephemeral index → passages. Reached only by
evidence-retrieval's `web` connector; never by a browser.

## The egress rule

**This service has no direct network path and must never acquire one**
(ADR-0005). Every outbound request leaves through the squid proxy at
`EVA_EGRESS_PROXY_URL`. With no proxy configured the HTTP client is pointed at
a discard port, so fetches fail fast instead of quietly opening a socket.

`EVA_ALLOW_DIRECT_EGRESS=true` exists for the SSRF fixture suite and local
development only. It logs a startup WARNING and is forbidden outside
development (rule BE7). If you see that warning in a deployed environment,
that is the incident.

## Start locally

```bash
(cd ../medical-dictation-backend && make dev-up && make migrate-up)
make evidence-up          # includes egress-proxy + searxng
make run-model-gateway    # embed.dense is needed for the ephemeral index
make run-websearch
```

`make run-websearch` runs uvicorn on the host, which cannot reach
`egress-proxy` on the compose network. For anything that actually fetches,
run the service in compose (`--profile services`) or publish the proxy port
and set `EVA_EGRESS_PROXY_URL=http://localhost:3128`.

## Health

`/readyz` is 503 only for Postgres and Redis. SearXNG down, the embed role
down, or the proxy unconfigured are **warnings**, not unreadiness: the answer
pipeline is designed to fall back to corpus-only, and taking this service out
of rotation would turn a soft degrade into a hard one.

```bash
curl -s localhost:8014/readyz | jq
# {"status":"ready","warnings":["metasearch_unreachable"]}
```

## Symptoms → causes

**Every query returns zero passages, `degrade_reason=metasearch_unavailable`.**
SearXNG is down or its upstream engines are blocking us.
```bash
docker compose -f infra/compose/evidence.yaml logs searxng | tail -50
curl -s 'http://localhost:8888/search?q=test&format=json' | jq '.results | length'
```
Upstream rate-limiting shows as empty results with HTTP 200 — that is the
cost of self-hosting metasearch (ADR-0005, accepted).

Distinguish the two HTTP-200-but-empty cases by `unresponsive_engines`:

| Entry | Meaning |
|---|---|
| `["duckduckgo", "proxy error"]` | The engine's own backend is missing from `egress-proxy/allowlist.txt`. Engine backends are infrastructure, not citable sources — they live in the network allowlist only |
| `["startpage", "CAPTCHA"]` | Upstream is bot-blocking this egress IP. Expected for some engines from datacenter ranges; the remaining engines carry the query |

**Queries reach SearXNG but nothing is ever fetched.**
Look at the `pages` array in the `/web/search` response — every page carries a
status and a reason:

| status | reason | Meaning |
|---|---|---|
| — | (in `not_allowlisted`) | Good source, nobody has allowlisted it. Admin can add it |
| `error` | `not_allowlisted` | A redirect went off-list |
| `error` | `transport:*` | Proxy unreachable, or the discard-port fallback is active |
| `error` | `content_type:*` | Served a PDF/binary where HTML was expected |
| `error` | `too_large` | Over the 3 MB cap |
| `robots_skip` | `robots_disallow` | Honoured robots.txt |
| `paywalled` | `metadata_only_source` | Journal tier: cited, never fetched |
| `garbage` | `extract_*` | Extraction rejected the page (see below) |
| `quarantined` | `injection_screen` | The page carried instruction-shaped payloads |

**Everything is `transport:ConnectError`.** The proxy is down or misconfigured:
```bash
docker compose -f infra/compose/evidence.yaml logs egress-proxy | grep TCP_DENIED
```

**A domain is enabled in `web_domains` but every fetch fails.**
It is missing from `infra/compose/egress-proxy/allowlist.txt`. The two lists
are separate on purpose; `make security-web` cross-checks the seeded set.

**Extraction rejects good pages** (`extract_too_short` / `extract_link_density`).
The heuristics are deliberately conservative — skipping beats ingesting
nav-bar soup. Reproduce against the fixture set in
`tests/unit/test_extract.py` before tuning `EVA_EXTRACT_MIN_CHARS` or
`EVA_EXTRACT_MAX_LINK_DENSITY`; a laxer threshold shows up later as fluent
citations of nothing.

## Egress-deny alarm

Every refused request logs `TCP_DENIED`. A spike means either someone is
probing the fetcher with crafted URLs or the allowlists have drifted. Both
want a human.

```bash
docker compose -f infra/compose/evidence.yaml logs --since 1h egress-proxy \
  | grep -c TCP_DENIED
```

## Allowlist administration

```bash
# list (tenant view: shipped GLOBAL rows are marked `shipped: true`)
curl -H "Authorization: Bearer $TOKEN" localhost:8014/web/domains | jq

# add (lands as `pending`; still needs the network allowlist)
curl -XPOST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domain":"aafp.org","trust_tier":"professional_society","notes":"AAFP"}' \
  localhost:8014/web/domains

# disable a shipped default for this tenant: insert a shadowing row
curl -XPOST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domain":"nejm.org","trust_tier":"journal","status":"disabled"}' \
  localhost:8014/web/domains
```

Shipped GLOBAL rows are visible but not updatable: `PATCH` on one returns 404
(RLS filters it out of the UPDATE — cross-scope reads as not-found, never
forbidden, per PD3).

## Cache and reopen

`web_pages` + two objects per page (`web/snapshot/<hash>`,
`web/extract/<hash>.txt`) through `EncryptedObjectStore`. A fresh cached page
is reused with **no network call at all**.

Reopening an answer must never re-fetch (AC-S04-B-6): the answer renders from
its stored envelope, and the web passages inside it came from the snapshot
captured at answer time. If a page has since changed or 404'd, the answer
still renders — that is the point.

To force a refetch of one page, delete its `web_pages` row (the objects are
inert without it).

## QS1

This service must never be able to name a patient snapshot type. Three gates
protect that (`make lint-imports`, `make check-qs1`, `make qs1-taint`). If you
are adding a field to `WebSearchRequest`, stop: the request accepting only
`ClinicalIntent` is the type wall, and widening it is the leak.
