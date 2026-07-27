# ADR-0034: Speaker-diarization backend — Silero VAD + ECAPA embeddings + online clustering

Date: 2026-07-26
Status: Accepted
Sprint: 14 (conversation mode)

## Context

Conversation mode points the microphone at the consultation itself and
must produce speaker-turn proposals (`S1`/`S2` + confidence, `UNKNOWN`
when ambiguous) at the streaming cadence of ADR-0013 (4 s window / 2 s
stride) without degrading the v1 latency targets. The backend must fit
the platform's non-negotiables: fully offline model loading
(`HF_HUB_OFFLINE=1`), single-artifact checksum-pinned bakes
(docs/models/PINS.md, ADR-0021 posture), no gated weights without an
explicit process, and deterministic/explainable behavior for a medical
product.

## Candidates

| Candidate | Verdict | Why |
|---|---|---|
| **pyannote.audio 3.x** (segmentation-3.0 + wespeaker embedding pipeline) | **Rejected (desk evaluation)** | Both weight repos are **HF-gated** (account-linked acceptance + token). The platform has zero gated-model process: `HF_TOKEN` exists only as a build secret for a private mirror, and no token is provisioned for gated pulls. Worse, the pipeline's `config.yaml` re-resolves its sub-repos **over the network at load time**, which fights `HF_HUB_OFFLINE=1` and the one-repo/one-artifact/one-checksum bake contract — it would need multi-repo pins plus a rewritten config. SOTA DER was not worth reopening the supply-chain posture for a pilot whose labels are human-reviewed proposals. Not benchmarked on our hardware because the gate itself was the disqualifier (decision confirmed with the product owner, 2026-07-26). |
| **NVIDIA NeMo diarization** | **Rejected (standing decision)** | ADR-0009 already rejected NeMo for ASR: "heavyweight container and unknown internal model lineage." Both objections apply unchanged to the diarization models; overturning a recorded decision needs stronger evidence than a pilot needs. Not installed/benchmarked. |
| **Silero VAD + SpeechBrain ECAPA-TDNN + online cosine clustering** | **Accepted (measured)** | `silero-vad` is already a runtime dep (asr-worker) and torch/torchaudio were already resolved in the lock. ECAPA weights are **ungated, Apache-2.0**, a single 83 MB artifact that pins cleanly (`speechbrain/spkrec-ecapa-voxceleb@0f99f2d0`, sha256 `0575cb64…`). The clustering layer is ~200 lines of deterministic, explainable code we own. The dictation-service energy-VAD docstring pre-committed to exactly this route in sprint 04. |

## Decision

Diarize per window with **Silero VAD segmentation → ECAPA embeddings →
online 2-speaker cosine clustering** (package
`dictation_service.diarization`):

- VAD tuned for turn boundaries (150 ms min-silence — deliberately NOT
  the asr-worker wrapper, which merges <500 ms gaps and caps at 30 s
  for Whisper's benefit, destroying turn structure).
- Speech regions are frontier-clipped (each ms embedded exactly once;
  the 2 s window overlap is VAD context only) and chunked to ≤1.2 s.
- **Bootstrap phase**: chunks accumulate until a complete-linkage 2-way
  split is accepted (mean cross-group cos < 0.45 and intra−cross gap
  ≥ 0.12, each side ≥ 2 chunks). On acceptance every prior chunk is
  re-scored against the two centroids with the online rule and
  retrospectively relabeled; a one-level sub-split guards against a
  third voice polluting a group. Until bootstrap (or 15 s of
  single-voice audio), word attribution reports *pending* — labels may
  trail text, never lie.
- **Online phase**: nearest-centroid with assignment floor 0.45 and
  ambiguity margin 0.08 → `UNKNOWN`, never a guess. Confidence =
  `0.5·best_sim + 0.5·min(1, (best−second)/0.3)`.
- Word attribution: majority overlap against `TokenTiming` start/end
  (≥30 % coverage, ≥65 % majority share, else `UNKNOWN`).
- Doctor/patient mapping is a separate explainable inference
  (opener 0.35 + clinician-register density 0.65; flips only on strong
  evidence; frozen by manual `SetSpeakerMapping`) — see
  `diarization/mapping.py` and docs/api/dictation-ws-v2.md.

## Measured numbers (2026-07-26, Apple M5, CPU, `eval/conversations/v1`)

Harness: `scripts/eval/run_der.py` (production code path, streaming
cadence). Corpus: 8 synthetic TTS dialogues, 204.5 s, exact generator
ground truth (see `scripts/eval/build_conversation_fixtures.py`).

| Dialogue | DER | Word attr. | UNKNOWN rate | lat p95/window | Mapping hint |
|---|---|---|---|---|---|
| uk-consult-001 | 0.021 | 0.982 | 0.018 | 54 ms | OK |
| uk-cardio-002 | 0.010 | 0.985 | 0.015 | 54 ms | OK |
| uk-patient-opens-003 | 0.040 | 1.000 | 0.000 | 57 ms | OK |
| uk-command-004 | 0.000 | 0.974 | 0.026 | 54 ms | OK |
| uk-rapid-005 (stress) | 0.441 | 0.619 | 0.381 | 56 ms | abstains |
| uk-third-voice-006 | 0.093 | 0.852 | 0.111 | 53 ms | OK |
| uk-anamnesis-007 | 0.005 | 1.000 | 0.000 | 54 ms | OK |
| en-consult-008 | 0.013 | 0.988 | 0.012 | 55 ms | OK |

- **Corpus DER (2-speaker) = 0.029** (pilot bar ≤ 0.20) — PASS
- **Word attribution (corpus) = 0.968** (bar ≥ 0.85) — PASS
- **Third-voice words wrongly labeled S1/S2 = 0** (must-be-0) — PASS
- Diarization latency p95 ≤ 57 ms/window on CPU (budget: must not push
  partial p95 over 1100 ms; on GPU it shares the device with Whisper —
  rig numbers pending, below)
- Extra memory: ECAPA resident ≈ 90 MB + Silero ≈ 2 MB (max RSS of the
  full eval process: 485 MB)

## Known limitations (documented, not defects)

1. **Rapid sub-second turns** (uk-rapid-005): ECAPA embeddings are
   noisy under ~0.6 s; the pipeline answers with `UNKNOWN` (38 % of
   words there), not wrong labels. The FE one-tap correction flow is
   the designed remedy.
2. **Near-twin third voice**: a bystander whose voice sits ≥ ~0.5
   cosine to a participant (measured: raw Milena vs Lesya 0.54–0.70 per
   chunk) cannot be separated by cosine thresholds at all — it will be
   absorbed into the nearest speaker. Score-normalised backends
   (PLDA/AHC) would be the upgrade path. The committed fixture uses a
   register-shifted third voice to verify the *distinct*-voice contract.
3. **2 speakers max** in the pilot; a distinct third voice lands
   `UNKNOWN` (verified), never a crash.
4. **CPU numbers**: this laptop has no CUDA. Per the WER-methodology
   precedent (ADR-0019), laptop numbers are the plumbing + relative
   signal; the A10G rig re-runs `make der-eval` as the standing release
   gate before conversation mode ships to staging (open item in
   todo.md; the same rig gap already blocks the sprint-07 WER gate).
   GPU capacity weighting is therefore configured, not measured:
   conversation sessions default to weight 2 vs dictation 1
   (`MDX_CONVERSATION_SESSION_WEIGHT`), to be re-measured on the rig.
   **Amended by ADR-0035** (sprint-14 deployment): the dual-model
   residency and per-window diarization cost have since been measured on
   CPU (+166 MB RSS over Whisper; 33–47 ms/window), the fleet shape is
   decided (single mixed pool), and weight 2 is retained as a deliberate
   conservative choice. The rig re-run remains the blocking gate for the
   latency claim, which CPU cannot evaluate.

## Supply chain

| Component | Licence | Pin |
|---|---|---|
| silero-vad 6.2.1 (PyPI wheel, weights bundled) | MIT | version-pinned via uv.lock; **weights arrive inside the wheel** — pre-existing PINS.md gap, now recorded there |
| speechbrain 1.1.0 (code) | Apache-2.0 | uv.lock |
| `speechbrain/spkrec-ecapa-voxceleb` weights | Apache-2.0, ungated | revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`, sha256 fail-closed via `scripts/models/prepare_ecapa.py`; baked at `/opt/models/ecapa` (PINS.md row) |

`hyperparams.yaml` is a repo-owned patched copy (infra/models/ecapa/):
upstream's file re-resolves artifacts from the HF repo id at load time
(the same offline-hostile pattern that disqualified pyannote); ours
points at the local dir and drops the unused 7205-class VoxCeleb head.

## Re-baselining

The DER corpus is generated by macOS TTS and committed; `say` output is
not bit-stable across macOS versions, so regenerating the corpus or
re-pinning the ECAPA revision requires re-baselining the numbers above
in an ADR amendment — the same discipline ADR-0019 imposes on WER.
