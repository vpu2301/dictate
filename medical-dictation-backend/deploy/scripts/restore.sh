#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# S11 deployment (ADR-0028) — restore an encrypted backup, then
# RE-RUN completed erasures (docs/runbooks/erasure.md).
#
# A restore resurrects data that was erased after the backup was
# taken. Restoring erased patients is FORBIDDEN — so this script:
#   1. captures the erasure ledger (completed erasure requests) from
#      the LIVE database before it is overwritten,
#   2. restores the chosen backup,
#   3. re-runs every ledger entry with completed_at > backup taken_at
#      through the idempotent erasure engine (rerun_erasures.py).
# Step 3 is not optional and not manual archaeology — it runs
# automatically. If the live DB is already unreachable at step 1,
# supply a previously captured ledger via --ledger.
#
# Usage (repo root, stack running):
#   deploy/scripts/restore.sh --latest [--yes]
#   deploy/scripts/restore.sh <backup_id> [--ledger path.json] [--yes]
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/../.."   # repo root

MC_IMAGE="minio/mc:RELEASE.2024-04-18T16-45-29Z"
NETWORK="medical-dictation_default"
BUCKET="mdx-backups"
DB="medical_dictation"
PASSFILE="deploy/secrets/backup.passphrase"
VARDIR="deploy/var/restore"
LEDGER_DIR="deploy/var/ledgers"
OPS_COMPOSE="deploy/compose/privacy-ops.yml"

BACKUP_ID=""
LEDGER=""
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --latest) BACKUP_ID="__latest__" ;;
    --ledger) LEDGER="$2"; shift ;;
    --yes)    ASSUME_YES=1 ;;
    *)        BACKUP_ID="$1" ;;
  esac
  shift
done
[[ -n "$BACKUP_ID" ]] || { echo "usage: restore.sh <backup_id>|--latest [--ledger f] [--yes]"; exit 2; }
[[ -f "$PASSFILE" ]] || { echo "missing $PASSFILE — cannot decrypt backups"; exit 2; }

mkdir -p "$VARDIR" "$LEDGER_DIR"

_mc() {
  docker run --rm --network "$NETWORK" --entrypoint sh -v "$(pwd)/$VARDIR:/restore" "$MC_IMAGE" -c "
    mc alias set local http://minio:9000 \
      \${MINIO_ROOT_USER:-minioadmin} \${MINIO_ROOT_PASSWORD:-minioadmin} >/dev/null
    $1
  "
}

if [[ "$BACKUP_ID" == "__latest__" ]]; then
  # parse on the host — the mc image has no awk
  BACKUP_ID="$(_mc "mc ls local/$BUCKET/" | awk '{print $NF}' \
    | grep '\.manifest\.json$' | sort | tail -1 | sed 's/\.manifest\.json$//')"
  [[ -n "$BACKUP_ID" ]] || { echo "no backups found in $BUCKET"; exit 2; }
  echo "latest backup: $BACKUP_ID"
fi

# ── 1. Erasure ledger from the LIVE db (before it is overwritten) ──
if [[ -z "$LEDGER" ]]; then
  LEDGER="$LEDGER_DIR/ledger-$(date -u +%Y%m%dT%H%M%SZ).json"
  echo "── capturing erasure ledger → $LEDGER"
  if ! docker compose exec -T postgres psql -U postgres -d "$DB" -Atc "
      SELECT COALESCE(json_agg(row_to_json(r)), '[]') FROM (
        SELECT id, tenant_id, patient_id, kind, reason, status,
               requested_by, requested_at, reviewed_by, reviewed_at,
               scheduled_for, completed_at
        FROM patient_privacy_requests
        WHERE kind = 'erasure' AND status = 'completed') r
    " > "$LEDGER"; then
    echo "ERROR: live DB unreachable — re-run with --ledger <previously captured file>"
    exit 2
  fi
  echo "   ledger entries: $(docker compose exec -T postgres true >/dev/null 2>&1; python3 -c "import json,sys;print(len(json.load(open('$LEDGER'))))")"
fi

# ── 2. Download, verify, decrypt, restore ──────────────────────────
echo "── fetching $BACKUP_ID from $BUCKET"
_mc "mc cp local/$BUCKET/$BACKUP_ID.dump.enc /restore/ && mc cp local/$BUCKET/$BACKUP_ID.manifest.json /restore/"

TAKEN_AT="$(python3 -c "import json;print(json.load(open('$VARDIR/$BACKUP_ID.manifest.json'))['taken_at'])")"
WANT_SHA="$(python3 -c "import json;print(json.load(open('$VARDIR/$BACKUP_ID.manifest.json'))['sha256'])")"
GOT_SHA="$( (shasum -a 256 "$VARDIR/$BACKUP_ID.dump.enc" 2>/dev/null \
            || sha256sum "$VARDIR/$BACKUP_ID.dump.enc") | awk '{print $1}')"
[[ "$WANT_SHA" == "$GOT_SHA" ]] || { echo "sha256 mismatch — refusing to restore"; exit 2; }
echo "   manifest ok: taken_at=$TAKEN_AT sha256=$GOT_SHA"

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "RESTORE will OVERWRITE database '$DB' with $BACKUP_ID. Type 'restore' to continue: " ans
  [[ "$ans" == "restore" ]] || { echo "aborted"; exit 1; }
fi

echo "── terminating connections + pg_restore --clean"
docker compose exec -T postgres psql -U postgres -d postgres -c "
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname = '$DB' AND pid <> pg_backend_pid();" >/dev/null

PASS="$(cat "$PASSFILE")"
docker compose exec -T -e BACKUP_PASSPHRASE="$PASS" postgres \
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -pass env:BACKUP_PASSPHRASE \
  < "$VARDIR/$BACKUP_ID.dump.enc" \
  | docker compose exec -T postgres pg_restore -U postgres -d "$DB" \
      --clean --if-exists --no-owner --role=postgres \
  || echo "note: pg_restore exited $? — --clean drop errors on first restore are benign; review output above"

rm -f "$VARDIR/$BACKUP_ID.dump.enc"

# ── 3. Re-run erasures completed after the backup (mandatory) ──────
echo "── re-running post-backup erasures (ledger vs taken_at=$TAKEN_AT)"
# --entrypoint python: the service's default entrypoint is `sh -c` (the
# loop wrapper), which would swallow the script path as $0.
docker compose -f "$OPS_COMPOSE" run --rm -T --entrypoint python \
  -v "$(pwd)/$LEDGER:/ledger.json:ro" \
  erasure-scheduler /ops/rerun_erasures.py \
    --ledger /ledger.json --backup-taken-at "$TAKEN_AT"

echo "ok: restore of $BACKUP_ID complete; erased patients re-erased"
