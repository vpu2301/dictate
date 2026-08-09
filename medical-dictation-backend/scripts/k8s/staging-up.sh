#!/usr/bin/env bash
# ── Sprint-16 deployment — bring up the k3d STAGING cluster ───────────
#
# Stands up the full product on a local k3d cluster from the locally
# built compose images (the spec's fallback posture while the UA-hosting
# decision is pending — docs/deploy/hosting-gap.md). Same chart, same
# gates, laptop-sized cluster. Verified live 2026-08-08 (E2E smoke:
# login → dictate(WS) → report → sign(mock) → /verify at the edge).
#
#   scripts/k8s/staging-up.sh            # create + deploy everything
#   scripts/k8s/staging-up.sh --delete   # tear the cluster down
#
# Prereqs: k3d, helm, kubectl, docker, openssl; compose images built.
# RAM: stop the compose app stack first (docker compose stop).

set -euo pipefail
cd "$(dirname "$0")/../.."

CLUSTER=mdx-staging
NS=mdx-staging

if [[ "${1:-}" == "--delete" ]]; then
  k3d cluster delete "$CLUSTER"
  exit 0
fi

# 1. Cluster. Traefik is DISABLED — it would squat :80/:443 ahead of the
#    public-edge LoadBalancer (found live; docs/runbooks/k8s.md).
if ! k3d cluster list | grep -q "^$CLUSTER"; then
  k3d cluster create "$CLUSTER" \
    --agents 1 \
    -p "8443:443@loadbalancer" \
    -p "8080:80@loadbalancer" \
    --k3s-arg "--disable=traefik@server:0" \
    --wait
fi

# 2. Import the locally built images (no registry in staging) + the
#    stateful/utility images the chart pins.
docker pull pgvector/pgvector:pg16 >/dev/null || true
docker pull busybox:1.36 >/dev/null || true
IMAGES=(
  medical-dictation-auth-service:latest
  medical-dictation-asr-service:latest
  medical-dictation-asr-worker:latest
  medical-dictation-dictation-service:latest
  medical-dictation-nlp-service:latest
  medical-dictation-report-service:latest
  medical-dictation-core-service:latest
  medical-dictation-autocomplete-service:latest
  medical-dictation-generation-service:latest
  medical-dictation-notification-service:latest
  medical-dictation-signing-service:latest
  medical-dictation-migrate:latest
  medical-dictation-seed:latest
  pgvector/pgvector:pg16
  busybox:1.36
)
k3d image import -c "$CLUSTER" "${IMAGES[@]}"

# 3. Namespace + out-of-band material (secret VALUES never live in the chart).
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic mdx-postgres \
  --from-literal=password=postgres --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic mdx-minio \
  --from-literal=user=minioadmin --from-literal=password=minioadmin \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic mdx-keycloak-admin \
  --from-literal=user=admin --from-literal=password=admin \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic mdx-keycloak-clients \
  --from-literal=KEYCLOAK_LOGIN_CLIENT_SECRET=dev-secret-change-in-prod-mdx-backend \
  --from-literal=KEYCLOAK_ADMIN_CLIENT_SECRET=dev-secret-change-in-prod-mdx-admin \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic mdx-master-key \
  --from-file=master.key=infra/dev/master.key \
  --dry-run=client -o yaml | kubectl apply -f -

# Self-signed edge TLS (prod: cert-manager Certificate).
if ! kubectl -n "$NS" get secret mdx-edge-tls >/dev/null 2>&1; then
  TMP=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
    -keyout "$TMP/tls.key" -out "$TMP/tls.crt" -subj "/CN=mdx-staging.local" \
    >/dev/null 2>&1
  kubectl -n "$NS" create secret tls mdx-edge-tls \
    --cert="$TMP/tls.crt" --key="$TMP/tls.key"
  rm -rf "$TMP"
fi

# Pinned mock test CA: every signing pod signs under the SAME chain
# instead of a per-pod ephemeral CA (found live: per-pod CAs can never
# be trusted by the store). Generated once with the mock's own code.
if ! kubectl -n "$NS" get secret mdx-mock-test-ca >/dev/null 2>&1; then
  uv run --project services/signing-service python -c "
from medical_kep.mock_provider import _ensure_test_ca
from pathlib import Path
_ensure_test_ca(Path('/tmp/mdx-test-ca'))"
  kubectl -n "$NS" create secret generic mdx-mock-test-ca --from-file=/tmp/mdx-test-ca
fi

# Realm, consents, trust store (candidate bundle excluded — >256 KiB
# annotation limit, and it is a review artefact, not a runtime input).
kubectl -n "$NS" create configmap mdx-keycloak-realm \
  --from-file=realm-export.json=infra/keycloak/realm-export.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap mdx-consent-texts \
  --from-file=infra/seeds/consents \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap mdx-trust-store \
  --from-file=ca-bundle.pem=infra/trust-store/ca-bundle.pem \
  --from-file=czo-cert.pem=infra/trust-store/czo-cert.pem \
  --from-file=test-ca-bundle.pem=/tmp/mdx-test-ca/ca.cert.pem \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

# 4. Deploy. The post-install hooks run migrate (with the idempotent
#    init.sql initContainer) → minio-init → seed.
helm upgrade --install mdx infra/k8s/mdx -n "$NS" --timeout 15m "$@"

echo "staging-up: deployed. kubectl -n $NS get pods"
echo "KEDA (optional): helm install keda kedacore/keda -n keda --create-namespace"
echo "                 helm upgrade mdx infra/k8s/mdx -n $NS --set keda.enabled=true"
