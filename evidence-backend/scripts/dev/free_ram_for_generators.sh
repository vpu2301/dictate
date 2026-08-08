#!/usr/bin/env bash
# Stop the platform containers the evidence stack does not use, so the two
# generators have room to stay resident.
#
# Why this exists: `generator.heavy` (12B, ~7.3 GiB) plus `generator.fast`
# (4B, ~2.5 GiB) plus KV cache do not fit beside a full platform stack on a
# 24 GiB laptop. When they do not fit they swap rather than fail, and swapping
# silently invalidates the one thing the S04 live run is for — the first-token
# latency NFR. A slow number and a wrong number look identical.
#
# Evidence inherits only auth/users, the DB, Redis and object storage from the
# platform (ADR-0001), so everything else can go. Nothing here is destructive:
# `docker start` brings them all back, and no volume is touched.
set -euo pipefail

# Keep: postgres, redis, minio, keycloak (token issuer), auth-service (token
# endpoint the live run uses), and the observability stack (cheap, and the
# stage traces are the point of the run).
STOP=(
  medical-dictation-asr-worker-1          # whisper large-v3   ~2.7 GiB
  medical-dictation-dictation-service-1   # whisper + ECAPA    ~3.0 GiB
  medical-dictation-asr-service-1
  medical-dictation-nlp-service-1
  medical-dictation-report-service-1
  medical-dictation-signing-service-1
  medical-dictation-autocomplete-service-1
  medical-dictation-notification-service-1
  medical-dictation-generation-service-1
  medical-dictation-llama-server-1        # the :8089 gemma3:1b — not ours
  medical-dictation-core-service-1
)

running=()
for c in "${STOP[@]}"; do
  docker ps --format '{{.Names}}' | grep -qx "${c}" && running+=("${c}")
done

if (( ${#running[@]} == 0 )); then
  echo "nothing to stop — the platform's non-evidence services are already down"
else
  printf 'stopping %d platform containers not used by evidence:\n' "${#running[@]}"
  printf '  %s\n' "${running[@]}"
  docker stop "${running[@]}" >/dev/null
  echo
  echo "restore later with:"
  echo "  docker start ${running[*]}"
fi

echo
echo "still up (evidence depends on these):"
docker ps --format '  {{.Names}}' | grep -E 'postgres|redis|minio|keycloak|auth-service|evidence-' || true
