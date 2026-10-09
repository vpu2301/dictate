# Data register — EU AI Act / GDPR

**This document is an engineering transparency artefact, not a legal
opinion.** It describes what the platform records about the datasets it
measures itself against, and it is written so that a lawyer or an auditor
can start from facts rather than from interviews. Where a legal conclusion
is required, this document says so and stops.

Mechanics: migration `0097`, `services/autocomplete-service/.../eval_register.py`,
`GET /compliance/data-register`. This file is the content the register
points at.

## What is in scope

The **evaluation corpus** — the scripted recordings and reference
transcripts used to measure speech-recognition quality (WER/CER, dose
accuracy). It is the only dataset the platform holds for the purpose of
measuring itself.

Explicitly **not** in scope here: patient records, dictations, reports, and
audio produced in clinical use. Those are covered by the privacy programme
(ADR-0028, `docs/runbooks/privacy-ops.md`) and are never part of the eval
corpus.

## The datasets

| dataset | origin | contents | frozen |
| --- | --- | --- | --- |
| Corpus snapshots (`snapshot-vN`) | synthetic, scripted | audio + reference transcripts | yes, by construction |
| Replica imports (CSV) | synthetic, scripted | text only, no audio | yes (a file digest) |

Both are auto-registered: publishing a snapshot and committing a CSV import
each write their own register entry, re-using the SHA-256 that was already
computed for integrity. Nobody has to remember to update the register, which
is the only way a register stays true.

## Patient data: none, and why that is enforceable

`dataset_registry.contains_patient_data` carries a CHECK constraint pinning
it to `false`. That is not decoration — it means the schema cannot record a
dataset containing patient data, and changing the policy requires a
migration somebody reviews.

The claim behind the constraint rests on three independent controls, each of
which predates this epic:

1. **Scripted-only capture.** The recorder may only record a line that
   exists server-side; the upload route refuses any `script_id` (or drifted
   text) it cannot find. There is no free-form capture path.
2. **A PII sweep at every write boundary** — line authoring, CSV import, and
   again over the whole set at publish time. A finding refuses the write.
3. **A named attestation for ad-hoc capture**, the one path where the words
   are not known before the microphone opens. A human states that the
   recording contains no patient data, and that statement is written into
   the hash-chained audit trail with their name on it.

## Personal data: yes

The recordings are identifiable employees reading a script. **A voice is
personal data under the GDPR whatever the words are**, so
`contains_personal_data` is true for every snapshot. Reading the
patient-data field and assuming this one is the most common way a register
like this ends up being wrong, which is why they are two columns and not one.

- **Legal basis:** consent (GDPR Art. 6(1)(a)), recorded per speaker in
  `corpus_speaker_consents`, scope `corpus_voice`.
- **Collection:** a take cannot be stored without a live consent — the
  upload is refused with 403 before the audio is decoded, so unconsented
  audio never exists server-side even briefly.
- **Withdrawal:** stamps `revoked_at`. The speaker's takes stop entering
  **new** snapshots from that moment. Published snapshots are unchanged: the
  basis that existed when they were frozen is not unmade by a later
  withdrawal, and a measurement journal that rewrote itself would be worth
  nothing. Withdrawal deletes no audio; erasure is the separate, deliberate
  act in the privacy runbook.
- **Retention:** for as long as the corpus is the measurement baseline,
  reviewed annually.

## Record-keeping

The register does not duplicate the journals; it points at them. All four
are append-only and none is granted UPDATE or DELETE:

| journal | question it answers |
| --- | --- |
| `corpus_eval_runs` | which measurements were taken, under which rules, against which corpus digest |
| `corpus_eval_imports` | which file added which replicas, including previews that were not committed |
| `corpus_eval_take_attempts` | how many attempts a replica cost, in which condition, by which speaker |
| `corpus_eval_gold_revisions` | which reference transcripts changed, when, and whether the change moved the measurement |

Together these are the technical documentation and logging a
record-keeping obligation would look for. Whether they satisfy a *specific*
obligation is a legal question, not an engineering one — see below.

## Classification — open, for a lawyer

The following are **not** settled by this document and must be confirmed by
qualified counsel:

1. **Does Klarnote fall under the EU AI Act's high-risk category?** The
   plausible route is the medical context: an AI system that is a safety
   component of, or is itself, a medical device under Regulation (EU)
   2017/745 falls under Annex I. Klarnote transcribes and structures
   clinician dictation and does not diagnose, but "clinical documentation
   assistance" is a boundary somebody with standing has to draw.
2. **If high-risk, which obligations attach** — data governance (Art. 10),
   technical documentation (Art. 11), record-keeping (Art. 12), accuracy and
   robustness (Art. 15) — and whether the artefacts above satisfy them as
   they stand.
3. **Whether consent is the right basis** for employee voice recordings, or
   whether legitimate interest is more appropriate given the
   employer–employee relationship (consent freely given is a known difficulty
   there). This one is worth asking early: it changes the withdrawal
   mechanics, not just the paperwork.
4. **Whether the corpus counts as "training data"** for any obligation. It
   is not used for training — it is a held-out measurement set, and the
   dev/test split (migration 0092) exists specifically so that tuning cannot
   contaminate it — but the distinction may or may not be the one the
   regulation draws.

Until (1) is answered, treat the register as what it is: a good-faith,
automatically-maintained inventory that makes the answers cheap to produce.

## Reading the register

- Console: **Безпека → Реєстр даних**.
- `GET /compliance/data-register` — JSON.
- `GET /compliance/data-register/export.html` — the document, always available.
- `GET /compliance/data-register/export.pdf` — the same document as a file.
  Needs the `pdf` extra and its native libraries (pango/cairo); a deployment
  without them answers 503 and points at the HTML rather than serving a file
  that will not open.
