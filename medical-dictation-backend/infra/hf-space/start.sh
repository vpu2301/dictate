#!/usr/bin/env bash
# HF Space container entrypoint. Initializes Postgres on tmpfs, runs
# all migrations, seeds templates + voice commands + abbreviations,
# stages the Keycloak realm-export for `start-dev --import-realm`,
# then hands off to supervisord.
#
# Auth model: real Keycloak with the sprint-02 realm. A seeded
# `clinician@demo.test` user is added at start. HF API key gates the
# Space itself.
set -euo pipefail

LOG=/var/log/medical-dictation/start.log
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

echo "[start] $(date -u +%FT%TZ) initializing HF Space container"

# ── Postgres bootstrap on tmpfs ──────────────────────────────────────
if [ ! -s "/run/pgdata/PG_VERSION" ]; then
    echo "[start] initdb on tmpfs"
    /usr/lib/postgresql/16/bin/initdb -D /run/pgdata --auth=trust --no-locale --encoding=UTF8
    chown -R postgres:postgres /run/pgdata
fi

su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D /run/pgdata -l /var/log/medical-dictation/pg-bootstrap.log start"
sleep 2

echo "[start] applying init.sql + migrations + seeds"
su postgres -c "psql -d postgres -f /opt/init.sql" || true
for f in /opt/migrations/*.sql; do
    case "$f" in
        *.down.sql) continue;;
    esac
    echo "[start] migrate: $(basename "$f")"
    su postgres -c "psql -d medical_dictation -f $f" || true
done

su postgres -c "psql -d medical_dictation -f /opt/seed/medical_prompts.sql" || true
su postgres -c "psql -d medical_dictation -f /opt/seed/abbreviations_global.sql" || true

# Seed 4 system templates that fit the demo's purpose.
echo "[start] seeding system templates"
for tpl in cardiology_outpatient_uk cardiology_outpatient_en internal_medicine_uk radiology_xray_uk; do
    if [ -f "/opt/templates/${tpl}.json" ]; then
        SCHEMA=$(cat "/opt/templates/${tpl}.json")
        CODE=$(echo "$SCHEMA" | python3 -c "import sys, json; print(json.load(sys.stdin)['code'])")
        NAME=$(echo "$SCHEMA" | python3 -c "import sys, json; print(json.load(sys.stdin)['name'])")
        LANG=$(echo "$SCHEMA" | python3 -c "import sys, json; print(json.load(sys.stdin)['language'])")
        SPEC=$(echo "$SCHEMA" | python3 -c "import sys, json; print(json.load(sys.stdin)['specialty'])")
        VER=$(echo "$SCHEMA" | python3 -c "import sys, json; print(json.load(sys.stdin).get('schema_version', 1))")
        ESC=$(echo "$SCHEMA" | python3 -c "import sys; s=sys.stdin.read(); print(s.replace(chr(39), chr(39)*2))")
        su postgres -c "psql -d medical_dictation -v ON_ERROR_STOP=1 -c \"SELECT upsert_system_template('${CODE}', '${NAME}', '${LANG}', '${SPEC}', ${VER}, '${ESC}'::jsonb)\"" || true
    fi
done

# Seed a demo tenant + a clinician user that Keycloak will mirror.
echo "[start] seeding demo tenant + user"
cat <<'EOF' | su postgres -c "psql -d medical_dictation"
INSERT INTO tenants (id, name, display_name)
VALUES ('00000000-0000-0000-0000-0000000000d0', 'demo', 'Demo Tenant')
ON CONFLICT DO NOTHING;
EOF

# ── Keycloak realm-export staging ───────────────────────────────────
# The sprint-02 realm-export.json + an additional seeded user. Keycloak's
# `start-dev --import-realm` reads from /opt/keycloak/data/import.
echo "[start] staging keycloak realm with seeded user"
mkdir -p /opt/keycloak/data/import
python3 <<'PY'
import json, pathlib, sys
src = pathlib.Path("/opt/keycloak-realm.json")
realm = json.loads(src.read_text("utf-8"))
realm.setdefault("users", [])
# Idempotent seed: only add if username absent.
existing = {u.get("username") for u in realm["users"]}
if "clinician@demo.test" not in existing:
    realm["users"].append({
        "username": "clinician@demo.test",
        "email": "clinician@demo.test",
        "firstName": "Demo",
        "lastName": "Clinician",
        "enabled": True,
        "emailVerified": True,
        "credentials": [{"type": "password", "value": "demo-please-change", "temporary": False}],
        "realmRoles": ["clinician"],
        "attributes": {"tenant_id": ["00000000-0000-0000-0000-0000000000d0"]},
    })
dst = pathlib.Path("/opt/keycloak/data/import/medical-dictation.json")
dst.write_text(json.dumps(realm, ensure_ascii=False, indent=2), "utf-8")
print(f"realm staged at {dst}")
PY

# Stop Postgres so supervisord can take over (Keycloak boots first via priority).
su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D /run/pgdata stop"
sleep 1

echo "[start] $(date -u +%FT%TZ) handing off to supervisord"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
