#!/usr/bin/env bash
# Start the two `generator.*` backends the gateway expects (ADR-0006).
#
#   generator.fast   :8081   gemma3:4b    triage classification, intent extraction
#   generator.heavy  :8082   gemma3:12b   clinical synthesis
#
# Serving is llama-server, per ADR-0006 ("llama-server in dev, vLLM on the
# rig").
#
# WEIGHTS TRAP #1 — do NOT reach for the host's Ollama blobs. The platform
# mounts its `ollama pull gemma3:1b` blob straight into a llama-server
# (docker-compose.override.yml), so the obvious move is to do the same for 4b
# and 12b. It does not work: Ollama writes gemma3 4b/12b with its own
# converter for its own engine, and upstream llama-server rejects them with
#     error loading model hyperparameters:
#     key not found in model: gemma3.attention.layer_norm_rms_epsilon
# 1b happens to be loadable, which is exactly why the platform's trick looks
# transferable and is not. Weights therefore come from ggml-org's own
# conversions — ggml-org being the llama.cpp org, so these are the reference
# artifacts for this server, and the repos map 1:1 onto the ADR-0006 pins
# (google/gemma-3-4b-it -> ggml-org/gemma-3-4b-it-GGUF).
#
# WEIGHTS TRAP #2 — the platform's llama-server on :8089 is NOT reusable. It
# serves gemma3:1b at 4096 tokens: the wrong model for both roles and a context
# far too small for synthesis over retrieved chunks. Pointing the gateway at it
# yields confusing garbage rather than a clean failure.
#
# `-hf` resolves against Hugging Face on FIRST run only and caches to
# ~/Library/Caches/llama.cpp. That runtime resolve is fine on a dev laptop and
# forbidden in the offline images (rule BE6) — so a local GGUF path always wins
# when given, mirroring the gateway's own embed_model_dir-over-embed_model_id
# precedence:
#     EVA_GEN_FAST_GGUF=/opt/models/gemma-3-4b-it-Q4_K_M.gguf make run-generators
#
# Ports/URLs match services/evidence-model-gateway/.../config.py defaults
# exactly, so nothing needs an env override to use this.
set -euo pipefail

# Q4_K_M: the quality/footprint knee. Resident cost with KV at the contexts
# below: fast ~3.1 GiB, heavy ~8.6 GiB.
#
# ── ADR-0006 pins vs what a laptop can actually run ──────────────────
# The pins below are the ADR's dev pins and stay the default. On a 24 GiB
# machine they do NOT fit alongside the stack the live run needs: measured
# here, ~14 GiB available against ~11.7 GiB of generators plus ~2.5 GiB of
# evidence infra, ~1 GiB of platform infra and a ~2.5 GiB gateway. Exceeding
# it does not fail cleanly — the Docker engine OOM-kills containers
# (postgres, keycloak and kafka went first) while the generators keep serving,
# so the stack appears to collapse around a component that looks healthy.
#
# `make run-generators-lite` serves gemma-3-4b for BOTH roles (~6.2 GiB total)
# and is the way to exercise the pipeline on a laptop. It is off-pin for
# generator.heavy by definition, so it prints a banner saying so: a first-token
# number measured that way is plumbing-grade and is not the NFR gate, which
# ADR-0006 already places on the rig.
readonly ADR_HEAVY_PIN="ggml-org/gemma-3-12b-it-GGUF:Q4_K_M"
FAST_HF="${EVA_GEN_FAST_HF:-ggml-org/gemma-3-4b-it-GGUF:Q4_K_M}"
HEAVY_HF="${EVA_GEN_HEAVY_HF:-${ADR_HEAVY_PIN}}"
FAST_GGUF="${EVA_GEN_FAST_GGUF:-}"
HEAVY_GGUF="${EVA_GEN_HEAVY_GGUF:-}"
FAST_PORT="${EVA_GEN_FAST_PORT:-8081}"
HEAVY_PORT="${EVA_GEN_HEAVY_PORT:-8082}"

# ── context sizing (deliberate — the doc asks for this to be written down) ──
# The gateway caps prompts at EVA_GATEWAY_MAX_PROMPT_CHARS (120 000 by
# default). A prompt that passes that cap but overflows the launched context
# fails *inside* llama-server and surfaces as a synthesis error that reads like
# a prompt bug — so the two must be reconciled rather than left to chance.
#
# THE TRAP: `-c` is the TOTAL context, which llama.cpp divides across
# `--parallel` slots. `-c 16384 --parallel 2` gives each request 8192 tokens,
# not 16384; /props reports the per-slot number. Sizing `-c` as if it were
# per-request silently halves the usable prompt — the exact overflow this
# reconciliation exists to prevent. So the per-slot budget is the knob here and
# `-c` is derived from it.
#
# Arithmetic, at a conservative 3 chars/token (Cyrillic tokenizes worse than
# Latin, so this is the pessimistic end) and reserving max_generate_tokens=2048
# for the output:
#
#   16384 per slot - 2048 out = 14336 tok x 3 chars = ~43 000 chars
#
# EVA_GATEWAY_MAX_PROMPT_CHARS=40000 fits under that for BOTH roles, and is
# still far above the real ceiling: evidence-answer sends
# max_evidence_blocks=12 chunks, ~20 000 chars in practice. Nothing legitimate
# gets rejected.
#
# Parallelism differs by role, and only because memory is tight: heavy is 12B,
# and its KV cache scales with total context, so it gets one slot (~1.3 GiB KV)
# instead of two (~2.5 GiB). Synthesis is one call per question behind a
# per-user in-flight cap of 2, and a queued second request measures cleaner
# than two requests contending for the same weights anyway.
#
# The rig keeps the 120 000 default — vLLM serves the 27B at 128k context.
FAST_SLOT_CTX="${EVA_GEN_FAST_SLOT_CTX:-16384}"
HEAVY_SLOT_CTX="${EVA_GEN_HEAVY_SLOT_CTX:-16384}"
FAST_PARALLEL="${EVA_GEN_FAST_PARALLEL:-2}"
HEAVY_PARALLEL="${EVA_GEN_HEAVY_PARALLEL:-1}"
FAST_CTX=$(( FAST_SLOT_CTX * FAST_PARALLEL ))
HEAVY_CTX=$(( HEAVY_SLOT_CTX * HEAVY_PARALLEL ))

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${REPO_ROOT}/.run"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# ── preflight ────────────────────────────────────────────────────────────
command -v llama-server >/dev/null 2>&1 \
  || die "llama-server not on PATH. Install it: brew install llama.cpp"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

for port in "${FAST_PORT}" "${HEAVY_PORT}"; do
  if port_busy "${port}"; then
    die ":${port} is already in use. If that is a stale generator, stop it:
    lsof -nP -iTCP:${port} -sTCP:LISTEN"
  fi
done

[[ -z "${FAST_GGUF}"  || -f "${FAST_GGUF}"  ]] || die "EVA_GEN_FAST_GGUF is not a file: ${FAST_GGUF}"
[[ -z "${HEAVY_GGUF}" || -f "${HEAVY_GGUF}" ]] || die "EVA_GEN_HEAVY_GGUF is not a file: ${HEAVY_GGUF}"

# Memory sanity. Both models are resident at once and compete with whatever the
# Docker VM has already claimed — 13.6 GiB of a 24 GiB laptop, in the dev setup
# this was written on. Evidence needs only postgres/redis/minio/keycloak/
# auth-service from the platform, so the lever is stopping the rest:
#   scripts/dev/free_ram_for_generators.sh
#
# Measured as total - wired - active. NOT free+inactive: on macOS that reads
# ~4 GiB on a machine with 16 GiB genuinely available, because inactive and
# compressed pages are reclaimable but are not counted as free. Using the naive
# metric here produced a warning on every single run, which is the fastest way
# to teach someone to ignore it. Docker Desktop also balloons — its containers
# reported 5.8 GiB while the VM's host footprint was 3.5 GiB — so container
# totals overstate the pressure too.
# Budget: generators only. The rest of the stack (gateway ~2.5, evidence infra
# ~2.5, platform infra ~1) needs roughly 6 GiB on top, which is why the check
# is against generators + headroom rather than generators alone.
if [[ "${HEAVY_HF}" == "${ADR_HEAVY_PIN}" ]]; then NEED_GB=18; else NEED_GB=12; fi
if command -v vm_stat >/dev/null 2>&1 && command -v sysctl >/dev/null 2>&1; then
  page=$(vm_stat | awk -F'[ .]' '/page size of/{print $8}')
  pinned=$(vm_stat | awk -F: '/Pages wired down|Pages active/{gsub(/[ .]/,"",$2); s+=$2} END{print s}')
  avail_gb=$(( ( $(sysctl -n hw.memsize) - pinned * page ) / 1073741824 ))
  if (( avail_gb < NEED_GB )); then
    printf '\033[33mwarning:\033[0m ~%s GiB available; these generators plus the rest of\n' "${avail_gb}" >&2
    printf '          the stack want ~%s GiB. Overshooting does not fail cleanly — the\n' "${NEED_GB}" >&2
    printf '          Docker engine OOM-kills containers while llama-server keeps\n' >&2
    printf '          serving, so the stack looks like it collapsed on its own.\n' >&2
    printf '          Either free memory:   bash scripts/dev/free_ram_for_generators.sh\n' >&2
    if [[ "${HEAVY_HF}" == "${ADR_HEAVY_PIN}" ]]; then
      printf '          or run the lite pair: make run-generators-lite\n' >&2
    fi
  fi
fi

if [[ "${HEAVY_HF}" != "${ADR_HEAVY_PIN}" ]]; then
  printf '\033[33m'
  cat <<EOF
  ┌──────────────────────────────────────────────────────────────────┐
  │  OFF-PIN: generator.heavy is NOT the ADR-0006 dev pin.            │
  │    pinned: ${ADR_HEAVY_PIN}
  │    running: ${HEAVY_HF}
  │  Latency and synthesis quality measured this way are             │
  │  plumbing-grade. They are NOT the NFR gate, which ADR-0006        │
  │  places on the rig. Label any number you record accordingly.      │
  └──────────────────────────────────────────────────────────────────┘
EOF
  printf '\033[0m'
fi

# ── launch ───────────────────────────────────────────────────────────────
mkdir -p "${RUN_DIR}"
PIDS=()

cleanup() {
  info "stopping generators"
  for pid in "${PIDS[@]:-}"; do
    [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start_role() {
  local role="$1" gguf="$2" hf="$3" port="$4" ctx="$5" par="$6"
  local -a src
  local slot=$(( ctx / par ))
  if [[ -n "${gguf}" ]]; then
    src=(-m "${gguf}"); info "generator.${role}  ${gguf}  :${port}  ${slot} tok/slot x${par}"
  else
    src=(-hf "${hf}");  info "generator.${role}  ${hf}  :${port}  ${slot} tok/slot x${par}"
  fi
  # --no-mmproj: gemma-3 4b/12b are multimodal and llama-server fetches and
  # loads the ~850 MB vision projector by default. Nothing in this pipeline
  # sends an image — evidence is text — so that is pure download and pure
  # resident memory, and memory is the binding constraint here.
  #
  # --jinja applies the GGUF's own chat template. Not needed by the gateway,
  # whose LlamaCppBackend hand-wraps Gemma turns for the raw /completion
  # endpoint, but it keeps /v1/chat/completions usable for probing by hand.
  llama-server \
    "${src[@]}" \
    --host 127.0.0.1 --port "${port}" \
    -c "${ctx}" \
    --parallel "${par}" \
    --no-mmproj \
    --jinja \
    > "${RUN_DIR}/generator-${role}.log" 2>&1 &
  PIDS+=("$!")
}

start_role fast  "${FAST_GGUF}"  "${FAST_HF}"  "${FAST_PORT}"  "${FAST_CTX}"  "${FAST_PARALLEL}"
start_role heavy "${HEAVY_GGUF}" "${HEAVY_HF}" "${HEAVY_PORT}" "${HEAVY_CTX}" "${HEAVY_PARALLEL}"

# ── wait for readiness ───────────────────────────────────────────────────
# `ready()` in the gateway's LlamaCppBackend polls exactly this endpoint, so a
# green wait here means the gateway will report ready too (rule BE6).
# The ceiling is generous because the FIRST run downloads ~10 GiB of GGUF
# before it loads anything. Subsequent runs are cache hits and take seconds.
# A dead process is detected immediately rather than waited out.
wait_healthy() {
  # Separate `local` statements on purpose: bash expands every word of a single
  # `local` before performing any of its assignments, so a later value cannot
  # reference an earlier one — under `set -u` that is an unbound-variable exit.
  local role="$1"
  local port="$2"
  local hf="${3:-}"
  local log="${RUN_DIR}/generator-${role}.log"
  local i=0
  # ggml-org/gemma-3-4b-it-GGUF:Q4_K_M -> models--ggml-org--gemma-3-4b-it-GGUF
  # (substitute slashes in the REPO only — doing it on the whole path would
  # mangle $HOME too)
  local repo="${hf%%:*}"
  local cache_dir="${HOME}/.cache/huggingface/hub/models--${repo//\//--}"
  while (( i++ < 3600 )); do
    if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      info "generator.${role} ready on :${port}"
      return 0
    fi
    for pid in "${PIDS[@]}"; do kill -0 "${pid}" 2>/dev/null || {
      die "generator.${role} exited during load. Last lines:
$(tail -20 "${log}")"
    }; done
    if (( i % 30 == 0 )); then
      # Not the log tail: llama-server draws download progress with carriage
      # returns, so the last *line* stays frozen on the startup banner for the
      # entire download and reads like a hang. The cache size is the honest
      # signal — it is the only thing moving.
      local cached=""
      [[ -d "${cache_dir}" ]] && cached="$(du -sh "${cache_dir}" 2>/dev/null | awk '{print $1}')"
      info "generator.${role} still starting (${i}s)${cached:+ — ${cached} fetched}"
    fi
    sleep 1
  done
  die "generator.${role} did not become healthy within 3600s. See ${log}"
}

wait_healthy fast  "${FAST_PORT}"  "${FAST_HF}"
wait_healthy heavy "${HEAVY_PORT}" "${HEAVY_HF}"

cat <<EOF

  generator.fast    http://localhost:${FAST_PORT}   ${FAST_SLOT_CTX} tok/slot   ${FAST_GGUF:-${FAST_HF}}
  generator.heavy   http://localhost:${HEAVY_PORT}   ${HEAVY_SLOT_CTX} tok/slot   ${HEAVY_GGUF:-${HEAVY_HF}}

  logs: ${RUN_DIR}/generator-{fast,heavy}.log

  Next: \`make run-model-gateway\` — it already sets the matching
  EVA_GATEWAY_MAX_PROMPT_CHARS so an over-long prompt is a clean gateway 413
  and never a backend error. Ctrl-C here stops both generators.

EOF

wait
