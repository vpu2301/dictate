#!/usr/bin/env bash
# Weekly trust-store refresh (S09; reworked S09-rev against the REAL artifact).
#
# The Ukrainian trusted list (Довірчий список) is an ETSI TS 119 612
# XML at https://czo.gov.ua/download/tl/TL-UA.xml carrying an
# **enveloped XAdES (XML-DSig) signature** — there is no detached
# .p7s (the original sprint-09 script assumed one and had never run).
#
# Verification model:
#   1. The TL signer certificate is PINNED at
#      infra/trust-store/czo-cert.pem (first run bootstraps the pin
#      trust-on-first-use and prints its SHA-256 fingerprint — the pin
#      itself goes through PR review like every trust-store change).
#   2. Every run REQUIRES the embedded signer cert to match the pin.
#   3. When xmlsec1 is installed (the GitHub Actions job installs it),
#      the enveloped XML-DSig signature is fully verified against the
#      pinned cert. Without xmlsec1 (local dev), the run fails unless
#      ALLOW_PIN_ONLY=1 — pin-match alone is NOT signature proof.
#   4. CA certs are extracted from ServiceDigitalIdentity entries only
#      (never from the signature block) into ca-bundle.candidate.pem.
#   5. Non-empty diff → Slack #security + candidate written next to
#      the bundle. NEVER auto-applied; a human PRs the change.

set -euo pipefail

WORKDIR="$(mktemp -d)"
export WORKDIR   # consumed by the python extractor heredocs below
# Anchor on the script location, NOT `git rev-parse --show-toplevel` —
# the backend lives one level below the repo root.
TRUST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/trust-store"
export TRUST_DIR
TSL_URL="${TSL_URL:-https://czo.gov.ua/download/tl/TL-UA.xml}"
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"

cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "fetching trusted list: $TSL_URL"
curl -fsSL "$TSL_URL" -o "$WORKDIR/tsl.xml"

echo "extracting embedded TL signer certificate ..."
python3 - <<'PY'
import base64
import os
import pathlib

import defusedxml.ElementTree as ET

ns_ds = "{http://www.w3.org/2000/09/xmldsig#}"
root = ET.parse(os.path.join(os.environ["WORKDIR"], "tsl.xml")).getroot()
sig = root.find(f"{ns_ds}Signature")
if sig is None:
    raise SystemExit("TL carries no enveloped ds:Signature — refusing")
cert_el = sig.find(f".//{ns_ds}X509Certificate")
if cert_el is None or not cert_el.text:
    raise SystemExit("TL signature has no KeyInfo certificate — refusing")
der = base64.b64decode("".join(cert_el.text.split()))
out = pathlib.Path(os.environ["WORKDIR"]) / "tl-signer.pem"
with out.open("w", encoding="ascii") as fh:
    fh.write("-----BEGIN CERTIFICATE-----\n")
    fh.write(base64.encodebytes(der).decode("ascii"))
    fh.write("-----END CERTIFICATE-----\n")
print("signer cert extracted")
PY

if [ ! -f "$TRUST_DIR/czo-cert.pem" ]; then
  echo "bootstrap: pinning the TL signer cert (TOFU — PR-review this pin!)"
  cp "$WORKDIR/tl-signer.pem" "$TRUST_DIR/czo-cert.pem"
fi

pin_fpr="$(openssl x509 -in "$TRUST_DIR/czo-cert.pem" -noout -fingerprint -sha256)"
got_fpr="$(openssl x509 -in "$WORKDIR/tl-signer.pem" -noout -fingerprint -sha256)"
echo "pinned : $pin_fpr"
echo "signer : $got_fpr"
if [ "$pin_fpr" != "$got_fpr" ]; then
  echo "FATAL: TL signer certificate does not match the pinned czo-cert.pem" >&2
  exit 1
fi

if command -v xmlsec1 >/dev/null 2>&1; then
  echo "verifying enveloped XML-DSig with xmlsec1 ..."
  xmlsec1 --verify --insecure \
    --pubkey-cert-pem "$TRUST_DIR/czo-cert.pem" \
    --id-attr:Id "TrustServiceStatusList" \
    "$WORKDIR/tsl.xml"
elif [ "${ALLOW_PIN_ONLY:-0}" = "1" ]; then
  echo "WARNING: xmlsec1 not installed — signer-pin matched but the XML"
  echo "signature itself was NOT verified (ALLOW_PIN_ONLY=1)."
else
  echo "FATAL: xmlsec1 not installed and ALLOW_PIN_ONLY!=1" >&2
  exit 1
fi

echo "extracting CA certs from ServiceDigitalIdentity entries ..."
python3 - <<'PY'
import base64
import os
import pathlib

import defusedxml.ElementTree as ET

ns = {"t": "http://uri.etsi.org/02231/v2#"}
root = ET.parse(os.path.join(os.environ["WORKDIR"], "tsl.xml")).getroot()
out = pathlib.Path(os.environ["WORKDIR"]) / "ca-bundle.candidate.pem"
seen = set()
count = 0
with out.open("w", encoding="ascii") as fh:
    for sdi in root.findall(".//t:ServiceDigitalIdentity", ns):
        for cert_el in sdi.findall(".//t:X509Certificate", ns):
            if not cert_el.text:
                continue
            der = base64.b64decode("".join(cert_el.text.split()))
            if der in seen:
                continue
            seen.add(der)
            count += 1
            fh.write("-----BEGIN CERTIFICATE-----\n")
            fh.write(base64.encodebytes(der).decode("ascii"))
            fh.write("-----END CERTIFICATE-----\n")
print(f"extracted {count} unique service certificates")
PY

touch "$TRUST_DIR/ca-bundle.pem"   # first run: empty bundle -> full diff
DIFF="$(diff -u "$TRUST_DIR/ca-bundle.pem" "$WORKDIR/ca-bundle.candidate.pem" || true)"
if [ -z "$DIFF" ]; then
  echo "no change in CA bundle — nothing to do."
  exit 0
fi

echo "trust-store diff detected ($(printf '%s' "$DIFF" | grep -c '^+') added lines); surfacing for security review."
if [ -n "$SLACK_WEBHOOK" ]; then
  curl -fsSL -X POST -H "Content-Type: application/json" \
    --data "$(jq -n --arg d "$(printf '%s' "$DIFF" | head -c 3500)" '{text: ("Trust-store change detected:\n```\n" + $d + "\n```\nReview required before deploy.")}')" \
    "$SLACK_WEBHOOK"
fi
echo
echo "NOT auto-applying. Open a PR with the new bundle after security review."
# Leave the reviewed candidate next to the bundle so the PR author can
# `cp ca-bundle.candidate.pem ca-bundle.pem` after sign-off.
cp "$WORKDIR/ca-bundle.candidate.pem" "$TRUST_DIR/ca-bundle.candidate.pem"
exit 0
