#!/usr/bin/env python3
"""Produce the PRODUCTION Keycloak realm import from the dev export.

Sprint-16 deployment — the threat-model IOU: every
`dev-secret-change-in-prod-*` client secret is replaced, every dev user
and the dev-only `mdx-dev-cli` client removed. The output feeds
Keycloak's `--import-realm` (or `kc.sh import`).

Client secrets come from Vault (written by gen-prod-secrets.py) so the
realm and the services can never disagree; --generate mints fresh ones
instead (then you must write the SAME values to Vault yourself).

    python scripts/k8s/gen-prod-realm.py --vault  > /tmp/realm-prod.json
    python scripts/k8s/gen-prod-realm.py --generate > /tmp/realm-prod.json

The output is verified by the operator with:
    grep -c "dev-secret-change-in-prod" /tmp/realm-prod.json   # must be 0
    grep -c "dev-password" /tmp/realm-prod.json                # must be 0
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEV_EXPORT = ROOT / "infra" / "keycloak" / "realm-export.json"

DEV_ONLY_CLIENTS = {"mdx-dev-cli"}
CLIENT_SECRET_FIELDS = {
    "mdx-backend": "KEYCLOAK_LOGIN_CLIENT_SECRET",
    "mdx-admin": "KEYCLOAK_ADMIN_CLIENT_SECRET",
}


def _vault_secrets() -> dict[str, str]:
    addr = os.environ["MDX_VAULT_ADDR"].rstrip("/")
    token = os.environ["MDX_VAULT_TOKEN"]
    req = urllib.request.Request(
        f"{addr}/v1/secret/data/mdx/keycloak-clients",
        headers={"X-Vault-Token": token},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["data"]["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--vault", action="store_true", help="client secrets from Vault")
    src.add_argument("--generate", action="store_true", help="mint fresh client secrets")
    args = ap.parse_args()

    realm = json.loads(DEV_EXPORT.read_text())

    if args.vault:
        material = _vault_secrets()
    else:
        material = {field: secrets.token_urlsafe(32) for field in CLIENT_SECRET_FIELDS.values()}
        print("generated fresh client secrets — write the SAME values to "
              "Vault (secret/mdx/keycloak-clients) or the services cannot "
              "authenticate", file=sys.stderr)

    # 1. Regenerate confidential client secrets; drop dev-only clients.
    clients = []
    for client in realm.get("clients", []):
        cid = client.get("clientId")
        if cid in DEV_ONLY_CLIENTS:
            continue  # the proxy-OTP bypass surface (ADR-0039) — prod never ships it
        field = CLIENT_SECRET_FIELDS.get(cid)
        if field:
            client["secret"] = material[field]
        elif "secret" in client and str(client["secret"]).startswith("dev-secret"):
            client["secret"] = secrets.token_urlsafe(32)
        clients.append(client)
    realm["clients"] = clients

    # 2. No dev users in production — accounts are provisioned through
    #    auth-service invites (sprint 02) against the live realm.
    realm["users"] = []

    # 3. Belt and braces: the serialized realm must be clean.
    out = json.dumps(realm, indent=2, ensure_ascii=False)
    for needle in ("dev-secret-change-in-prod", "dev-password"):
        if needle in out:
            print(f"FATAL: {needle!r} survived the rewrite", file=sys.stderr)
            return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
