# Sprint-16 deployment — the hosting gap (what awaits the human's decision)

The chart (`infra/k8s/mdx`) was built and LIVE-VERIFIED against a local
k3d cluster (the spec's fallback while the UA-resident hosting decision
is pending — ADR-0006 residency posture). Everything below is what
changes per the concrete choice; each row names the artefact that
absorbs it. Nothing else in the chart depends on the choice.

| Gap | Cluster artefact | What the decision picks |
|---|---|---|
| Cluster itself | — | on-prem k3s/RKE2, UA provider, or residency-compliant cloud region |
| GPU node pool | `values-prod.yaml` (`mdx-pool: gpu` selector, `gpu=true:NoSchedule` taint, `nvidia.com/gpu` resources) | node provisioning + NVIDIA device plugin install |
| Stateful stores | `stateful.inCluster=false` + DSN/hosts in values | CloudNativePG / Redis operator / MinIO operator, or managed equivalents |
| WAF/CDN brand | edge Deployment carries rate zones/allowlist; provider WAF fronts the LoadBalancer | Cloudflare (signing runbook's suggestion) or the provider's WAF: bot filtering on /verify, geo-anomaly alerting |
| Public domain + TLS | `certManager.domain`, ClusterIssuer | domain purchase + DNS + Let's Encrypt or org CA (Дія will not call self-signed endpoints) |
| Vault endpoint | `MDX_VAULT_ADDR`, ExternalSecrets ClusterSecretStore | the production Vault (same trust root as ADR-0011 KMS) |
| Registry | `global.imageRegistry` | a registry close to the cluster (multi-GB model-baked images) |
| NetworkPolicy CNI | `networkPolicies.enabled=true` (prod) | **named gap, found live**: k3d/k3s v1.35's embedded netpol controller enforces default-deny but drops the allow rules — the policies are standard v1 semantics and must be verified on the real CNI (Calico/Cilium) at cluster bring-up |
| Ingress controller | k3s Traefik squats :80/:443 in k3d — staging bypasses it (found live); prod either disables the bundled ingress or routes it to `public-edge` | per distribution |
| llama-server (Layer C) | `stateful.llamaServer` disabled in staging | GPU-pool deployment + model blob provisioning |
| Mock test CA pinning | staging-only Secret `mdx-mock-test-ca` (per-pod ephemeral CA otherwise) | n/a in prod — mock provider refuses production |

## Staging↔prod parity notes (found during the live bring-up)

- `pgvector/pgvector:pg16` is REQUIRED (init.sql `CREATE EXTENSION
  vector`); plain postgres aborts first boot and leaves a half-init
  store. The migrate hook re-applies init.sql idempotently as an
  initContainer, so the store self-heals either way.
- Secret-mounted master key: fsGroup would give 0440 (provider refuses),
  no fsGroup gives root-owned 0400 (unreadable) — the root initContainer
  copy is the pattern (apps.yaml comment).
- Sprig `default` treats `false` as empty — boolean opt-outs in values
  must be checked with `hasKey`, never `| default true`.
