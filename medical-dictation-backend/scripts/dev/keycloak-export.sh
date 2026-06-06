#!/usr/bin/env bash
# Re-extract the realm definition from the running Keycloak container.
# Use this after making changes through the Keycloak admin UI to capture
# them back into infra/keycloak/realm-export.json (the source of truth).
#
# Usage: scripts/dev/keycloak-export.sh
#
# The exported file overwrites infra/keycloak/realm-export.json. Diff before
# committing — Keycloak's export adds many auto-generated IDs that we'd
# rather not churn unless intentional.
set -euo pipefail

REALM="${REALM:-medical-dictation}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
OUT="${OUT:-infra/keycloak/realm-export.json}"

# Container path that Keycloak uses for one-off exports.
TMP_IN_CONTAINER="/tmp/realm-export.json"

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

echo "Exporting realm '${REALM}' from running Keycloak container..."
docker compose -f "${COMPOSE_FILE}" exec -T keycloak \
    /opt/keycloak/bin/kc.sh export \
        --dir /tmp/export \
        --realm "${REALM}" \
        --users realm_file >/dev/null

docker compose -f "${COMPOSE_FILE}" exec -T keycloak \
    sh -c "cat /tmp/export/${REALM}-realm.json" > "${OUT}.tmp"

mv "${OUT}.tmp" "${OUT}"
echo "Realm exported to ${OUT}"
echo "Review the diff carefully — Keycloak includes generated IDs that will churn the file."
