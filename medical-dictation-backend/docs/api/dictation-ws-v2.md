# `medical-dictation.v2` — conversation mode & speaker diarization

Sprint 14. Companion to `dictation-ws-v1.md`, which stays the canonical
reference for everything v2 does not change. **v1 is byte-stable**: no
v1 model gained a field, and a v1 client that receives a v2 frame
rejects it cleanly via `extra="forbid"` (the sprint-04 promise, proven
in `tests/unit/test_protocol_v2.py`).

Read this together with **ADR-0034** (why the diarization backend is
Silero VAD + ECAPA + online clustering, with measured DER) and the
**ADR-0013 amendment** (the commit-policy fixes that made real
transcripts possible at all).

## The honesty principle

**Diarization is probabilistic. Every speaker label on this wire is a
PROPOSAL, never a fact.**

- Labels are anonymous (`S1`/`S2`) — the doctor/patient interpretation
  is a *separate*, always-overridable mapping.
- `UNKNOWN` is a first-class answer. Overlapping speech, a
  turn-straddling segment, or a third voice yields `UNKNOWN` rather
  than a guess.
- `null` is also legal: labels may trail the text by up to one window.
  **Render text immediately; colour it when the label lands.**
- Nothing downstream (note synthesis, the record) may treat a label as
  ground truth until the clinician finalizes. A mislabeled turn must be
  one tap to flip (`set_speaker_mapping`).

## Negotiation

The client offers subprotocols in `Sec-WebSocket-Protocol`; the server
picks by preference, **v2 over v1**:

```
Sec-WebSocket-Protocol: medical-dictation.v2, medical-dictation.v1
→ server accepts with: medical-dictation.v2
```

A v1-only client is unaffected. Offering neither is
`400 unsupported_protocol` (unchanged).

A session is **exactly one version for its whole lifetime**:

- `protocol_version` on a client frame must equal the negotiated
  version, else `error{code: unsupported_protocol}`.
- A reconnect (`resume_session_id`) that negotiates a *different*
  subprotocol than the original session gets the uniform
  `session_not_found` — a v1 tab must never receive v2 frames.

`PROTOCOL_VERSION_V2 = 2`.

## Client → server

### `start_session` (v2: gains `mode`)

```jsonc
{
  "type": "start_session",
  "protocol_version": 2,
  "prompt_id": "…",
  "language": "uk",              // "uk" | "en" | "de"
  "mode": "conversation",        // NEW: "dictation" (default) | "conversation"
  "encounter_id": "…",           // REQUIRED when mode=conversation
  "template_id": "…",            // needed for a draft at finalize
  "target_kind": "generic",
  "resume_session_id": null
}
```

`mode: "conversation"` points the microphone at the consultation
itself. It requires, checked before a single audio frame is accepted:

1. an `encounter_id`, and
2. a granted, non-withdrawn **`recording`** consent for that
   encounter's patient (patient-wide or scoped to this encounter).

Either one missing → `error{code: "consent_required", recoverable:
false}` and a `conversation.consent_refused` audit row. Obtain consent
via core-service (`POST /v1/patients/{id}/consents`, type `recording`),
then start again.

If the diarizer cannot load, conversation start fails **loudly** with
`error{code: "worker_failed", recoverable: true}` (close 1013) rather
than silently producing an unlabeled transcript.

### `set_speaker_mapping` (v2, new)

The clinician's manual assignment. **Authoritative from the moment
received** — the server stops re-inferring for the rest of the session
and never emits another non-manual `speaker_mapping_updated`.

```jsonc
{ "type": "set_speaker_mapping", "mapping": { "S1": "patient", "S2": "doctor" } }
```

Roles are `"doctor" | "patient"`. Sent on a dictation-mode session it
is `error{code: bad_message, recoverable: true}`.

The server acknowledges with a `speaker_mapping_updated{manual: true,
confidence: 1.0}`.

## Server → client

### `partial` / `final` (v2: gain speaker fields)

```jsonc
{
  "type": "final",
  "session_id": "…", "seq": 12,
  "text": "вже тиждень болить голова",
  "start_ms": 7120, "end_ms": 8740,
  "words": [ /* unchanged TokenTiming */ ],
  "avg_confidence": 0.91,
  "is_provisional": false,
  "voice_command": null,

  // v2 additions:
  "speaker": "S2",                 // "S1" | "S2" | "UNKNOWN" | null
  "speaker_confidence": 0.84,      // 0..1, null when speaker is null
  "speaker_mapping_hint": { "S1": "doctor", "S2": "patient" }  // or null
}
```

| `speaker` | meaning | FE |
|---|---|---|
| `"S1"` / `"S2"` | proposal with `speaker_confidence` | colour the turn; allow one-tap flip |
| `"UNKNOWN"` | diarized, but genuinely ambiguous | render text plainly; invite assignment |
| `null` | not diarized *yet* (labels trail by ≤1 window) | render text now, colour when a later frame resolves it |

`speaker_mapping_hint` is the current doctor/patient hypothesis
attached for convenience; `speaker_mapping_updated` is the
authoritative change notification.

### `speaker_mapping_updated` (v2, new)

```jsonc
{
  "type": "speaker_mapping_updated",
  "session_id": "…",
  "mapping": { "S1": "doctor", "S2": "patient" },
  "confidence": 0.72,
  "rationale": "opener 0.81 vs 0.19; clinician-register density 0.77 vs 0.23",
  "manual": false
}
```

Emitted when the inference **changes its hypothesis**, and once with
`manual: true` to acknowledge a `set_speaker_mapping`. The FE relabels
already-rendered turns. It carries no `seq` and does not advance the
partial/final sequence.

**The inference abstains rather than guess.** It is deliberately
conservative and explainable — no classifier:

- session opener (weight 0.35) + clinician-register vocabulary density
  (weight 0.65);
- it emits nothing at all unless there is real vocabulary evidence
  (≥ 2 clinician-register matches that actually discriminate). The
  opener alone is a coin flip — patients open consultations too, and
  degraded ASR silently erases the vocabulary signal;
- it flips only on strong evidence (new confidence ≥ old + 0.15);
- it freezes permanently on `set_speaker_mapping`.

If no hint ever arrives, that is the system saying *"I don't know"*.
The FE should let the clinician assign the mapping.

## Limits (pilot, documented — not bugs)

- **Two speakers.** A distinct third voice is reported `UNKNOWN`, never
  crammed into `S1`/`S2` (verified: 0 mislabeled third-voice words).
  A bystander whose voice is very close to a participant's cannot be
  separated by this backend at all (ADR-0034 §limitations).
- **Rapid sub-second turns** raise the `UNKNOWN` rate sharply
  (measured 38% on the stress fixture) — embeddings are unreliable
  below ~0.6 s of speech. The system abstains; it does not guess.
- Labels can trail text by one window; they never *retro-lie* (a
  committed label is not silently changed, though the doctor/patient
  *mapping* over labels can flip, which is what
  `speaker_mapping_updated` is for).

## Voice commands are OFF in conversation mode

A patient saying «новий абзац» mid-story must not edit the record. The
finalize-time NLP pipeline runs with `stages_disabled=["voice_commands"]`
for conversation sessions: those words stay **verbatim text** and no
operation is produced. Dictation mode is unchanged. Enforced
server-side (nlp-service skips the stage) with a defence-in-depth drop
in dictation-service if any operation arrives anyway.

## Finalize, persistence, drafts

Unchanged flow (`end_session` → `session_terminated`), plus:

- The persisted transcript (`dictation_sessions.transcript_jsonb`)
  gains, **for conversation sessions only**, per segment: `id` (UUID),
  `speaker`, `speaker_confidence`, `speaker_role`; and per word:
  `speaker`, `speaker_confidence`. A **dictation-mode transcript is
  byte-identical to the pre-sprint-14 shape** — existing consumers are
  untouched.
- `dictation_sessions.mode` records `'dictation' | 'conversation'`
  (migration 0057; a conversation row always carries an encounter).
- On finalize a **report draft** is created through the existing
  `POST /v1/reports` (sprint-08 hand-off — no parallel write path),
  authored with the clinician's own bearer, carrying
  `source_session_id` and the segment UUIDs in
  `transcript_segment_ids`. The dialogue is rendered as speaker-turn
  lines (`ЛІКАР:` / `ПАЦІЄНТ:` / `НЕВІДОМО:`).
  Draft creation never fails a finalize: the transcript is already
  persisted, and a skip is a `conversation.draft.create_failed` audit
  row (common reason: the session had no `template_id`).

## Capacity

A conversation session holds two models resident, so it costs
`MDX_CONVERSATION_SESSION_WEIGHT` (default **2**) capacity slots
against `MDX_PER_WORKER_MAX_SESSIONS` (default 4): **4 dictation OR
2 conversation OR a weighted mix**. Over capacity →
`error{code: gpu_full, recoverable: true}`, close 1013, as today.

> The weight is currently a configured estimate, not a GPU
> measurement — the A10G rig re-measures it before staging
> (todo.md §S14).

## New error codes

| code | recoverable | meaning |
|---|---|---|
| `consent_required` | no | conversation start without an encounter, or without a granted `recording` consent for its patient |

## Resume

Resume semantics are unchanged from v1 (§"Reconnection sequence"). The
speaker timeline lives on the session context beside the committed
transcript, so an in-process resume preserves speakers up to the
committed high-water mark; the version-pinning rule above applies.

## Audit kinds

`dictation.session.started` gains `mode` + `protocol_version` in its
payload. New: `conversation.speaker_mapping.inferred`,
`conversation.speaker_mapping.manual_set`,
`conversation.consent_refused` (warn), `conversation.draft.created`,
`conversation.draft.create_failed` (warn). See
`docs/audit/event-kinds.md`.

## Hand-off to note synthesis (sprint 12)

The generation-service does not exist in this repo yet. The contract it
must honour when it lands is fixed here, additively:

- `SynthesisInput.transcript` entries gain optional `speaker` and
  `speaker_role`, fed directly by the persisted conversation transcript
  above (already carries both, per segment and per word).
- The grounding check (sprint-12 §2.4) must add **speaker attribution**
  to its entity sweep: a drafted *"patient reports X"* where X was the
  **doctor's** utterance is a grounding violation and must be flagged.
  This matters more, not less, than entity grounding — misattributing
  a statement inverts clinical meaning.
- Segments whose `speaker` is `UNKNOWN`/`null`, or whose
  `speaker_role` is unset, must **not** be attributed to either party
  in generated prose.
