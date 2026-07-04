#!/usr/bin/env bash
# Generate the LOCAL self-signed TLS pair for public-edge.
# Production replaces these with a real certificate (Let's Encrypt /
# org CA) at the same paths — see nginx.conf.template header.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)/certs"
mkdir -p "$DIR"
openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
    -keyout "$DIR/edge.key" -out "$DIR/edge.crt" \
    -subj "/C=UA/O=Medical Dictation Dev/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
echo "wrote $DIR/edge.crt + edge.key (dev only; gitignored)"
