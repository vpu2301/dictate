#!/usr/bin/env bash
# Sprint-09 day-7: weekly trust-store refresh.
#
# 1. Download the current TSL from czo.gov.ua.
# 2. Verify its signature against the locally-bundled CZO root.
# 3. Diff the extracted CA bundle vs the in-tree ``infra/trust-store/ca-bundle.pem``.
# 4. If different, post to ``#security`` Slack with the diff and a
#    PR template. NEVER auto-apply.
#
# Triggered weekly via GitHub Actions cron + on-demand by SRE.

set -euo pipefail

WORKDIR="$(mktemp -d)"
TRUST_DIR="$(git rev-parse --show-toplevel)/infra/trust-store"
TSL_URL="${TSL_URL:-https://czo.gov.ua/download/tsl/TSL.xml}"
TSL_SIG_URL="${TSL_SIG_URL:-https://czo.gov.ua/download/tsl/TSL.xml.p7s}"
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"

cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "fetching TSL ..."
curl -fsSL "$TSL_URL"     -o "$WORKDIR/tsl.xml"
curl -fsSL "$TSL_SIG_URL" -o "$WORKDIR/tsl.xml.p7s"

echo "verifying TSL signature against CZO cert ..."
openssl smime -verify \
    -in "$WORKDIR/tsl.xml.p7s" -inform DER \
    -content "$WORKDIR/tsl.xml" \
    -CAfile "$TRUST_DIR/czo-cert.pem" \
    -out /dev/null

echo "extracting CA certs from TSL (uses defusedxml-equivalent in production)..."
# Sprint-09 sketch: production runs a Python extractor with defusedxml.
python3 - <<'PY'
import os, sys, defusedxml.ElementTree as ET, base64, pathlib
tsl = ET.parse(os.path.join(os.environ["WORKDIR"], "tsl.xml")).getroot()
ns = {"t": "http://uri.etsi.org/02231/v2#"}
out = pathlib.Path(os.environ["WORKDIR"]) / "ca-bundle.candidate.pem"
with out.open("w", encoding="utf-8") as fh:
    for cert_b64 in tsl.findall(".//t:X509Certificate", ns):
        der = base64.b64decode("".join(cert_b64.text.split()))
        fh.write("-----BEGIN CERTIFICATE-----\n")
        fh.write(base64.encodebytes(der).decode("ascii"))
        fh.write("-----END CERTIFICATE-----\n")
print("extracted to", out)
PY

DIFF="$(diff -u "$TRUST_DIR/ca-bundle.pem" "$WORKDIR/ca-bundle.candidate.pem" || true)"
if [ -z "$DIFF" ]; then
  echo "no change in CA bundle — nothing to do."
  exit 0
fi

echo "trust-store diff detected; surfacing for security review."
if [ -n "$SLACK_WEBHOOK" ]; then
  curl -fsSL -X POST -H "Content-Type: application/json" \
    --data "$(jq -n --arg d "$DIFF" '{text: ("Trust-store change detected:\n```\n" + $d + "\n```\nReview required before deploy.")}')" \
    "$SLACK_WEBHOOK"
fi
echo "$DIFF"
echo
echo "NOT auto-applying. Open a PR with the new bundle after security review."
exit 0
