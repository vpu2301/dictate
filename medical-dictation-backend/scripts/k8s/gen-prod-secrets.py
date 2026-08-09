#!/usr/bin/env python3
"""Generate the production secret material and write it to Vault.

Sprint-16 deployment: every `dev-secret-change-in-prod-*` placeholder
(threat model) is REGENERATED here with cryptographically random values;
nothing is printed unless --show is passed, nothing ever lands in the
repo. The Vault KV layout matches the chart's ExternalSecret templates
(infra/k8s/mdx/templates/externalsecrets.yaml).

Usage (operator, against the production Vault):

    MDX_VAULT_ADDR=https://vault... MDX_VAULT_TOKEN=... \\
    python scripts/k8s/gen-prod-secrets.py            # write to Vault
    python scripts/k8s/gen-prod-secrets.py --dry-run  # report keys only

The Keycloak realm import with the SAME regenerated client secrets is
produced by gen-prod-realm.py --vault (reads them back so the realm and
the services can never disagree).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import urllib.request

SECRET_LAYOUT: dict[str, dict[str, str]] = {
    # path (under <mount>/mdx/) → {field: kind}
    "keycloak-clients": {
        "KEYCLOAK_LOGIN_CLIENT_SECRET": "token",
        "KEYCLOAK_ADMIN_CLIENT_SECRET": "token",
    },
    "hmac-keys": {
        "SIGNER_IPN_HMAC_KEY": "hex32",
        "PUBLIC_VERIFY_IP_HMAC_KEY": "hex32",
        "IIT_CALLBACK_HMAC_KEY": "hex32",
        "MDX_PATIENT_IPN_HMAC_KEY": "hex32",
        "DSAR_DOWNLOAD_TOKEN_HMAC_KEY": "hex32",
    },
    "master-key": {
        # Only for file-provider pods; Transit-mode pods (ADR-0011
        # amendment, the production default) need no key file at all.
        "master.key": "bytes32-b64",
    },
    "infra": {
        "password": "token",       # postgres superuser
        "user": "literal:mdx",     # minio root user
        # minio password + keycloak admin share the postgres row shape;
        # split into per-store paths if the hosting choice separates them.
    },
}


def _value(kind: str) -> str:
    if kind == "token":
        return secrets.token_urlsafe(32)
    if kind == "hex32":
        return secrets.token_hex(32)
    if kind == "bytes32-b64":
        return base64.b64encode(secrets.token_bytes(32)).decode()
    if kind.startswith("literal:"):
        return kind.split(":", 1)[1]
    raise ValueError(kind)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true", help="print generated values (DANGEROUS)")
    ap.add_argument("--mount", default="secret")
    ap.add_argument("--prefix", default="mdx")
    args = ap.parse_args()

    addr = os.environ.get("MDX_VAULT_ADDR", "")
    token = os.environ.get("MDX_VAULT_TOKEN", "")
    if not args.dry_run and (not addr or not token):
        print("MDX_VAULT_ADDR / MDX_VAULT_TOKEN required (or use --dry-run)", file=sys.stderr)
        return 2

    for path, fields in SECRET_LAYOUT.items():
        data = {field: _value(kind) for field, kind in fields.items()}
        target = f"{args.mount}/data/{args.prefix}/{path}"
        if args.dry_run:
            print(f"would write {target}: fields={sorted(data)}")
            continue
        req = urllib.request.Request(
            f"{addr.rstrip('/')}/v1/{target}",
            data=json.dumps({"data": data}).encode(),
            headers={"X-Vault-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            resp.read()
        shown = data if args.show else dict.fromkeys(data, "<generated>")
        print(f"wrote {target}: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
