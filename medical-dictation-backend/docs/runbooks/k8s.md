# Runbook — Kubernetes deployment (sprint 16)

Chart: `infra/k8s/mdx` (staging defaults in `values.yaml`, prod in
`values-prod.yaml`). Gates: `make check-k8s-rendered` (render + prod
secret-clean + vendored-file drift) runs in `make ci` and the `k8s-render`
CI job. Companion docs: `docs/deploy/inventory.md`, `hosting-gap.md`,
`observability.md`, `resource-envelopes.md`.

## Staging (k3d) bring-up

```bash
docker compose build            # images (also used by k3d)
docker compose stop             # two full stacks don't share a 14 GiB VM
scripts/k8s/staging-up.sh       # cluster + secrets + configmaps + helm install
```

The script disables k3s's bundled Traefik (it squats :80/:443 ahead of
the `public-edge` LoadBalancer — found live). KEDA:
`helm install keda kedacore/keda -n keda --create-namespace`, then
`helm upgrade mdx ... --set keda.enabled=true`.

## Scale-in drain (dictation)

Scale-in NEVER kills a busy worker: the preStop hook POSTs
`/internal/drain` (loopback-only), the worker stops admitting (clients
get the gpu_full reconnect semantics and land elsewhere), `/readyz`
goes 503 (Service stops routing), and Kubernetes holds the pod until
`active_sessions == 0` (cap `terminationGracePeriodSeconds` = 1830 s).
Verified live: a pod deleted mid-session kept streaming, finalized
`reason=normal`, and only then exited. KEDA's `cooldownPeriod` +
`stabilizationWindowSeconds` (600 s) + one-pod-per-5-min policy keep
scale-in slow; the drain makes whatever it picks safe.

Manual drain of one pod: `kubectl exec <pod> -- python -c "import
urllib.request; urllib.request.urlopen(urllib.request.Request(
'http://127.0.0.1:8000/internal/drain', method='POST'))"` then delete it.

## Weighted autoscaling

`ScaledObject dictation-weighted-capacity` — Prometheus trigger on
`sum(capacity_weight_used)/sum(capacity_weight_max)` (the `_ratio`
names are the collector's gauge suffix), threshold 0.75. Verified live:
4 concurrent sessions on a weight-4 worker → utilisation 1.0 →
replicas 1→2 inside a minute.

## Edge

`public-edge` carries the S15 three-path allowlist + per-IP rate zones
that trip BEFORE app limits (verified: 11×200 then 429s on /verify at
30 r/s vs the app's 60/min), HSTS on every response, TLS terminated at
the pod (staging self-signed; prod cert-manager). A provider WAF fronts
the LoadBalancer per hosting-gap.md.

## Secrets

- Prod material: `scripts/k8s/gen-prod-secrets.py` → Vault;
  `scripts/k8s/gen-prod-realm.py --vault` → realm import with
  regenerated client secrets, no dev users, no `mdx-dev-cli`. Verify:
  `grep -c dev-secret-change-in-prod` on the realm output and on
  `helm template -f values-prod.yaml` must be 0 (CI-gated).
- Staging material is created by `staging-up.sh` (dev values, plain
  Secrets). The master key rides a root initContainer copy (owner/mode
  0400 for uid 65532 — see apps.yaml comment for why).

## Known staging quirks (all found live, all documented in-chart)

- postgres MUST be the pgvector image; the migrate hook's initContainer
  re-applies `init.sql` idempotently to self-heal half-init stores.
- If the postgres PVC is ever recreated, restart Keycloak (its schema
  lives in the same server and the pod caches broken state).
- Mock signing in-cluster needs the pinned test CA
  (Secret `mdx-mock-test-ca` mounted over the kep fixtures dir) +
  `TRUST_STORE_INCLUDE_TEST_CA=true` + the trust-store ConfigMap.
- NetworkPolicies are OFF in k3d (allow rules not honored by the
  embedded controller); MUST be on and verified on the prod CNI.
