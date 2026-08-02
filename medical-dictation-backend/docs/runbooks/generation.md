# Runbook — generation-service (Layer C inline completion)

Service: `generation-service` (:8009). ADR-0036. The typing-path
posture: every degraded state answers **204** — the editor never shows
an error because ghost text failed. That means outages here are
*silent*; the metrics below are the only way to see them.

## inline-latency-high

`LayerCInlineLatencyHigh`: served-completion p95 > 400 ms for 10 min.

1. `curl :8009/readyz` — is the backend reachable at all? (`layer_c_enabled`,
   `model` in the body.)
2. Check the llama-server host: `curl $MDX_GEN_BASE_URL/health`; look at
   its slot saturation (`--parallel` should be ≥ `MDX_GEN_SLOTS`).
3. A long-generation neighbour hogging the model host? The 2-slot pool
   protects the service, not the backend — co-hosted batch work must be
   moved off or the backend given `--parallel` headroom.
4. Remember the dev/prod split: dev CPU/Metal hosts CANNOT meet 400 ms
   (ADR-0036 measurements) — this alert is meaningful on the GPU rig only.

## filter-rate-high

`LayerCFilterRateHigh`: >10% of completions dropped by the safety filter
over an hour (audited as `layer_c.completion.filtered`, warn).

The model is routinely inventing clinical values. This is the
kill-switch input:

1. Sample the audit events: `payload.reason` says which class fires
   (blood_pressure / dosage / icd_code / date_like / bare_number) and
   `payload.matched` the fragment.
2. Prompt or model regression? Compare `MDX_GEN_MODEL` against the pin
   in docs/models/PINS.md — an unpinned model swap is the usual suspect.
3. Mitigate: `MDX_LAYER_C_ENABLED=false` (pod stays ready, endpoint
   answers 204, FE silently loses ghost text) while investigating.

## backend-errors

`LayerCBackendErrors`: the backend accepts requests but fails them
(`outcome="backend_error"`).

1. llama-server logs — OOM after a context spike is the classic
   half-dead state.
2. Restart the backend; the service needs no restart (per-request client).

## Capacity / tenants

- `MDX_GEN_TENANT_ALLOWLIST` — empty = all tenants; non-listed tenants
  get silent 204s (`mdx_layer_c_completions_total{outcome="tenant_disabled"}`).
- Rate limits: burst 10/s + 30/10s per user, fail-open on Redis loss.
  A second, stricter **per-IP edge limit** (5 r/s + burst 8, nginx
  `edge_completion` zone) sits in front on `public-edge` — an
  unauthenticated flood 429s at the edge before spending app CPU;
  debounced typing (~1–2 r/s) never touches it. See
  `infra/edge/nginx.conf.template`.
- Acceptance rate (`mdx_layer_c_acceptance_rate`, refreshed by the
  autocomplete nightly roll-up) is the feature's quality metric: a
  falling rate means clinicians stopped accepting ghosts — review the
  filter rate and latency before assuming the model got worse.
