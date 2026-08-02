#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# S11 deployment (ADR-0028) — encrypted Postgres backup → mdx-backups.
#
# Posture (docs/runbooks/erasure.md "Backups vs the right to erasure"):
#   * pg_dump custom format, AES-256 encrypted BEFORE it leaves the
#     postgres container, uploaded to the mdx-backups bucket.
#   * mdx-backups carries a 35-day ILM expiry rule (minio-init) — the
#     MAXIMUM retention window. Erasure is complete against backups
#     after one full rotation; the engine stamps that horizon into
#     every report_of_execution (backups_purged_by).
#   * The passphrase lives in deploy/secrets/backup.passphrase
#     (gitignored), generated on first run. Losing it makes every
#     backup unreadable — escrow it like the master key.
#   * SCOPE — the backup set is the Postgres dump ONLY. No object-
#     storage bucket is ever mirrored into a backup. In particular
#     mdx-audio-clips (S15 replay derivatives, ADR-0037) is EXCLUDED
#     BY POLICY, not by omission: clips are regenerable from source
#     audio, live 5 minutes (Redis registry; 1-day bucket ILM is the
#     ciphertext backstop), and must never outlive their source in a
#     backup. If bucket mirroring is ever added here, mdx-audio-clips
#     stays out of the include-list.
#
# Usage (from the repo root, full stack running):
#   deploy/scripts/backup.sh
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/../.."   # repo root

MC_IMAGE="minio/mc:RELEASE.2024-04-18T16-45-29Z"
NETWORK="medical-dictation_default"
BUCKET="mdx-backups"
DB="medical_dictation"
PASSFILE="deploy/secrets/backup.passphrase"
VARDIR="deploy/var/backups"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ID="mdx-${DB}-${TS}"

mkdir -p "$VARDIR" deploy/secrets

if [[ ! -f "$PASSFILE" ]]; then
  echo "generating backup passphrase at $PASSFILE — ESCROW THIS FILE"
  umask 077
  docker compose exec -T postgres openssl rand -hex 32 > "$PASSFILE"
fi
PASS="$(cat "$PASSFILE")"

echo "── dump + encrypt: $DB → $BACKUP_ID.dump.enc"
docker compose exec -T postgres pg_dump -U postgres -Fc "$DB" \
  | docker compose exec -T -e BACKUP_PASSPHRASE="$PASS" postgres \
      openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
      -pass env:BACKUP_PASSPHRASE \
  > "$VARDIR/$BACKUP_ID.dump.enc"

SHA256="$( (shasum -a 256 "$VARDIR/$BACKUP_ID.dump.enc" 2>/dev/null \
           || sha256sum "$VARDIR/$BACKUP_ID.dump.enc") | awk '{print $1}')"

cat > "$VARDIR/$BACKUP_ID.manifest.json" <<EOF
{
  "backup_id": "$BACKUP_ID",
  "database": "$DB",
  "taken_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "format": "pg_dump-custom",
  "cipher": "aes-256-cbc/pbkdf2-200000",
  "retention_days": 35,
  "sha256": "$SHA256"
}
EOF

echo "── upload → $BUCKET (35-day ILM expiry)"
docker run --rm --network "$NETWORK" --entrypoint sh \
  -v "$(pwd)/$VARDIR:/backup:ro" "$MC_IMAGE" -c "
    mc alias set local http://minio:9000 \
      \${MINIO_ROOT_USER:-minioadmin} \${MINIO_ROOT_PASSWORD:-minioadmin} >/dev/null
    mc cp /backup/$BACKUP_ID.dump.enc local/$BUCKET/
    mc cp /backup/$BACKUP_ID.manifest.json local/$BUCKET/
  "

rm -f "$VARDIR/$BACKUP_ID.dump.enc" "$VARDIR/$BACKUP_ID.manifest.json"
echo "ok: $BACKUP_ID uploaded (sha256=$SHA256); purged from $BUCKET by ILM after 35 days"
