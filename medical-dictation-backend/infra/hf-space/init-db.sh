#!/usr/bin/env bash
# Helper: re-initialize the demo Postgres from scratch (debug only).
# Production HF Space uses start.sh which runs init once per container life.
set -euo pipefail

rm -rf /run/pgdata/*
exec /usr/local/bin/start.sh
