# Egress proxy

The evidence stack's only route to the internet (ADR-0005). Everything that
leaves — the web fetcher and SearXNG's upstream engine calls — goes through
`squid` on `:3128` with a deny-by-default destination allowlist.

## Files

| File | Purpose |
|---|---|
| `squid.conf` | policy: deny all, allow `allowlist.txt`, block private ranges, log denials |
| `allowlist.txt` | the network allowlist (`dstdomain` syntax) |

## Two allowlists, on purpose

| | `web_domains` (migration 0070) | `allowlist.txt` (this dir) |
|---|---|---|
| Scope | product: which sources may be *cited* | network: which hosts may be *reached* |
| Changed by | `knowledge_admin` via `POST /web/domains` | an operator, via a deploy |
| Effect of adding | the fetcher will try the domain | the packet is allowed out |
| Effect of removing | the domain is skipped with a trace reason | the fetch fails at the proxy |

A domain enabled in the database but missing here simply fails to fetch. That
asymmetry is the point: a runtime product control can never widen the network
boundary.

## Changing the allowlist

1. Add the row to `docs/corpus/web-domains.md` with its trust tier and the
   clinical reviewer.
2. Add the domain to `allowlist.txt`.
3. Seed or `POST /web/domains` the `web_domains` row.
4. Restart the proxy (`docker compose -f infra/compose/evidence.yaml restart egress-proxy`).

Steps 1–2 are the reviewed ones. Step 3 is self-service for a tenant admin
*within* what steps 1–2 already permit.

## Denials

Every refused request logs `TCP_DENIED` to stdout. The deny counter is
alarmed (spec §10): a spike is either someone probing the fetcher with
crafted URLs or an allowlist that drifted from the product table. Both want a
human, not a retry.

```bash
docker compose -f infra/compose/evidence.yaml logs egress-proxy | grep TCP_DENIED
```

## Model vendors are absent, permanently

`api.openai.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`
and friends are not on the list and must never be added: rule LM1 says every
model is self-hosted, and this file is where that claim is verifiable rather
than merely stated. `tests/security/test_egress_policy.py` asserts it.
