#!/usr/bin/env bash
# doctor.sh — diagnose local environment before running make dev-up
set -euo pipefail

ERRORS=0

red()   { printf '\033[31m✗ %s\033[0m\n' "$*"; }
green() { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m⚠ %s\033[0m\n' "$*"; }

require_cmd() {
  local cmd=$1 label=${2:-$1} min_label=${3:-}
  if command -v "$cmd" &>/dev/null; then
    green "$label: $(command -v "$cmd")"
  else
    red "$label: not found${min_label:+ (need $min_label)}"
    ERRORS=$((ERRORS + 1))
  fi
}

require_version() {
  local cmd=$1 label=$2 pattern=$3
  if command -v "$cmd" &>/dev/null; then
    local ver
    ver=$("$cmd" --version 2>&1 | head -1)
    green "$label: $ver"
  else
    red "$label: not found (need $pattern)"
    ERRORS=$((ERRORS + 1))
  fi
}

echo "==================================================================="
echo " Medical Dictation Backend — Environment Doctor"
echo "==================================================================="
echo ""

echo "── Required tools ─────────────────────────────────────────────────"
require_version "docker"  "Docker"         "≥ 25.0"
require_cmd     "make"    "Make"
require_version "python3" "Python"         "≥ 3.12"
require_version "git"     "Git"            "≥ 2.40"

echo ""
echo "── Recommended tools ───────────────────────────────────────────────"
if command -v uv &>/dev/null; then
  green "uv: $(uv --version)"
else
  warn "uv not found — install from https://github.com/astral-sh/uv"
fi

echo ""
echo "── Docker ──────────────────────────────────────────────────────────"
if docker info &>/dev/null 2>&1; then
  green "Docker daemon is running"
else
  red "Docker daemon is NOT running — start Docker Desktop or the daemon"
  ERRORS=$((ERRORS + 1))
fi

if docker compose version &>/dev/null 2>&1; then
  green "Docker Compose plugin: $(docker compose version --short)"
else
  red "docker compose plugin not found (need Docker >= 23 or compose plugin installed)"
  ERRORS=$((ERRORS + 1))
fi

echo ""
echo "── Python version ──────────────────────────────────────────────────"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")
if [[ "$PY_VER" == "unknown" ]]; then
  red "Could not determine Python version"
  ERRORS=$((ERRORS + 1))
elif python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
  green "Python $PY_VER (≥ 3.12 required)"
else
  red "Python $PY_VER is too old — need ≥ 3.12"
  ERRORS=$((ERRORS + 1))
fi

echo ""
echo "── WSL2 check (Windows only) ───────────────────────────────────────"
if [[ "$(uname -s)" == "Linux" ]] && grep -qi microsoft /proc/version 2>/dev/null; then
  green "Running in WSL2"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  green "macOS — no WSL2 needed"
else
  warn "Windows detected without WSL2 — Docker performance may be poor. Enable WSL2."
fi

echo ""
echo "─────────────────────────────────────────────────────────────────────"
if [[ "$ERRORS" -eq 0 ]]; then
  echo -e "\033[32mAll checks passed! Run: make dev-up\033[0m"
else
  echo -e "\033[31m$ERRORS issue(s) found. Fix them before running make dev-up.\033[0m"
  exit 1
fi
