#!/usr/bin/env bash
# Submit a tiny synthetic WAV via asr-service and poll until done.
# Usage: bash scripts/dev/asr-smoke.sh
set -euo pipefail

: "${ASR_SERVICE_URL:=http://localhost:8001}"
: "${KEYCLOAK_URL:=http://localhost:8088}"
: "${REALM:=medical-dictation}"
: "${USERNAME:=clinician@a.test}"
: "${PASSWORD:=clinician}"
: "${PROMPT_ID:?set PROMPT_ID to a UUID from medical_prompts (uk/general default works)}"

# 1. Get a token via mdx-dev-cli direct grant.
TOKEN=$(curl -fsS -X POST \
  -d "client_id=mdx-dev-cli" \
  -d "username=$USERNAME" -d "password=$PASSWORD" \
  -d "grant_type=password" \
  "$KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])')

# 2. Generate a 1-second silent WAV with ffmpeg.
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT
ffmpeg -loglevel error -y -f lavfi -i "anullsrc=r=16000:cl=mono" \
  -t 1 -c:a pcm_s16le "$TMPDIR/silence.wav"

# 3. POST.
RESP=$(curl -fsS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "audio=@$TMPDIR/silence.wav;type=audio/wav" \
  -F "language=uk" \
  -F "prompt_id=$PROMPT_ID" \
  "$ASR_SERVICE_URL/asr/jobs")

JOB_ID=$(echo "$RESP" | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
echo "queued job_id=$JOB_ID"

# 4. Poll for completion.
for _ in $(seq 1 30); do
  STATUS=$(curl -fsS -H "Authorization: Bearer $TOKEN" \
    "$ASR_SERVICE_URL/asr/jobs/$JOB_ID" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["status"])')
  echo "$JOB_ID → $STATUS"
  case "$STATUS" in
    complete) echo "ok"; exit 0 ;;
    failed|cancelled) echo "fail"; exit 1 ;;
  esac
  sleep 2
done
echo "timeout waiting for $JOB_ID"
exit 1
