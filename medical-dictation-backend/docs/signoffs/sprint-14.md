# Sprint 14 sign-off — Conversation Mode & Speaker Diarization

Branch `S14`. Date 2026-07-26.
ADRs: **0034** (diarization backend, measured) + an **ADR-0013
amendment** (commit-policy fixes). Protocol: `docs/api/dictation-ws-v2.md`.
Eval methodology: `docs/eval/der-methodology.md`.

## What ships

A real two-voice consultation streams to a live transcript with speaker
turns. Labels are proposals (`S1`/`S2` + confidence, `UNKNOWN` when
ambiguous, `null` while they trail the text), one tap to correct, frozen
on manual set. v1 clients and dictation mode are provably untouched.
Patient speech can never trigger voice commands. Conversation requires
a recording consent, and its transcript carries per-word speakers into
the report draft.

## Verification (real output, CPU / Apple M5)

| Check | Result |
|---|---|
| Diarization DER, 8-dialogue corpus | **corpus 0.029**, mean 0.075, worst 0.441 (bar ≤ 0.20) — PASS |
| Word-level attribution | **corpus 0.968**, worst 0.619 (bar ≥ 0.85) — PASS |
| Third-voice words mislabeled `S1`/`S2` | **0** (must be 0) — PASS |
| Diarization added latency | p95 **≤ 57 ms/window**, ~10% of pipeline |
| Partial p95, both models resident | **400 ms** (budget 1100 ms) — PASS |
| End-to-end conversation, real Whisper + diarizer | 13 finals with correct doctor/patient turn structure; mapping hint correct; 1 turn-straddling segment honestly `UNKNOWN`; 2 early segments `pending` |
| v1 regression suite | green, **unchanged** |
| v1 byte-stability | `partial`/`final` key sets asserted exactly; no v1 model gained a field |
| v1 client rejects v2 frame | `extra="forbid"` proven for `PartialV2`/`FinalV2`/`SpeakerMappingUpdated` |
| v2 negotiation | v1-only → v1; v2-only → v2; both offered → **v2**; unknown → rejected |
| dictation-service unit suite | **157 passed** (was 103) |
| nlp-service unit suite | **330 passed** (was 320) |
| lint / import-linter / grep gates / security | all green (`17 contracts kept, 0 broken`) |
| typecheck (`--strict`, CI-gated packages) | green; new diarization + protocol modules strict-clean |

## Defects found and fixed (pre-existing, sprint-04)

Running real audio end-to-end through the production streaming path for
the first time exposed **three defects that between them meant a
dictation session could emit partials forever and finalize an EMPTY
transcript.** Nothing had caught them: the committer's unit tests fed it
word ages the windower cannot produce, the chaos/load suites drive
synthetic Opus that never reaches a commit decision, and the SPA runs
browser Web Speech, so no live client exercised this path.

1. **Commit horizon unreachable** — rule 1 wanted a word older than one
   full window, but candidates only ever come from inside that window.
   Horizon is now the *overlap* (the correct LocalAgreement criterion).
2. **VAD silence gate unreachable** — required 500 ms of contiguous
   silence; real inter-utterance pauses run 200–450 ms, so no boundary
   was ever found. Now 240 ms.
3. **No backstop on silence-gating** — transcript progress was
   conditional on the speaker pausing; a pause-free stretch lost words
   at finalize. Added `commit_max_provisional_ms` (4 s).

Regression coverage now sits where the bug lived
(`test_windower_commits.py` drives the real windower), so an
unreachable commit horizon fails CI instead of shipping. Full analysis
in the ADR-0013 amendment.

Also fixed during wiring: honesty metrics were double-counted across
partial re-emissions and read by nothing (now committed-words only, and
exported); `SpeakerMappingUpdated` punched a gap in the client `seq`
sequence; `_send_and_close` always encoded at v1.

## 🔴 Blocking finding NOT fixed here — S13 template regression

`required: true → false` on the diagnosis/assessment sections of **all
20 shipped templates**, introduced by commit `991fc20` (S13). By the
schema's own doctrine this is a **structural** template change, and
clinically it makes the diagnosis section optional everywhere.
`test_all_seed_templates_validate_and_dump_byte_identical` has been
failing on the branch since S13 merged — **`make test` / `make ci` are
red for this reason alone, independent of sprint 14.**

Sprint 14 deliberately did **not** regenerate the frozen fixtures:
doing so would erase the only evidence. Needs a clinical-content
decision (todo.md, top section).

## Open items (todo.md §S14)

- **A10G rig re-run is the release gate.** All numbers above are CPU
  plumbing numbers per the ADR-0019 precedent. Before staging: DER,
  per-window latency alongside 4 Whisper sessions, VRAM headroom.
- **Conversation session weight (2) is configured, not measured.**
- **pyannote** desk-rejected (HF-gated weights + offline-bake conflict);
  revisit only with a gated-weights custody process.
- **Real consented two-speaker audio** — the corpus is synthetic TTS
  with generator ground truth; a clinical claim needs real recordings
  (DPO + clinical content lead).
- **Sprint-12 linkage is a documented contract, not code** — the
  generation-service does not exist in this repo. The speaker-aware
  `SynthesisInput` extension and the speaker-attribution grounding
  requirement are specified in `dictation-ws-v2.md` §hand-off; the
  transcript already carries the data.

## Sign-offs

- [ ] Tech lead — protocol v2, capacity model, committer fixes
- [ ] Clinical content lead — **S13 template `required` regression
      (blocking)**; conversation fixture wording; `recording` consent text
- [ ] DPO / legal — `recording` consent text + real-audio eval corpus path
- [ ] SRE — A10G rig DER gate, Grafana `sprint-14-conversation` panels
