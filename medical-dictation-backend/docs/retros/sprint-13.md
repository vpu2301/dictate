# Sprint 13 retro — Structured Anamnesis

**Date:** 2026-07-23 · **Branch:** `S13`

## What went well

**The corpora caught what review would have missed.** Six real defects
surfaced from labeled test data, not from reading code. Two of them —
"кинув палити" filling nothing, and "алергій немає, окрім латексу"
dropping a latex allergy — are the kind of bug that reads as correct
in a diff and only fails in a clinic. Writing the Ukrainian corpus
*before* tuning the matcher was the highest-value hour of the sprint.

**"Empty is a valid answer" made decisions easy.** Every ambiguous
case had an obvious resolution once the prime directive was treated as
a rule rather than a preference: below threshold, conflicting,
negated, unit-less, two dates, lookup timeout — all resolve to empty.
No design meeting needed.

**Additive proofs beat additive claims.** Freezing the 20 template
dumps and the canonical-bytes corpus *before* touching the models
turned "this is additive" from an assertion into a test. The
byte-identical dump requirement also forced a real design decision
(omit empty `options` from serialization) that would otherwise have
been discovered by a signature failure months later.

## What was harder than planned

**Five of eight steps described mechanisms that did not exist.** The
NLP context did not carry options; the normalizers emitted no
structured artifacts and there was no channel to pass any; there is no
tenant-settings store; migration 0011 does not seed voice commands;
there is no sprint-05 TP/FP corpus. Each cost an hour of exploration
and forced a small design decision that the plan had assumed away.

*Lesson:* the plan's "EXPLORE first, pin the exact shape" instruction
was the single most valuable line in the step docs. Every step that
skipped straight to implementation would have built on a fiction. Next
sprint's plans should mark load-bearing assumptions explicitly as
"verify before building" rather than stating them as context.

**Pre-existing defects surfaced at the edges.** The `слід`→`slash`
false positive has been in production-shaped code since sprint 05,
inserting stray slashes into Ukrainian notes. Nine sprint-08 audit
kinds were never documented. Neither was in scope; both were cheap to
fix once a new gate looked at the area with fresh eyes.

*Lesson:* a close-out guard that scans a whole service (not just the
sprint's diff) pays for itself the first time it runs.

## What we would do differently

**Decide the delivery path on day one.** Step 04's "StageOutput vs
Operation" question sat open until exploration proved that neither
candidate consumer existed and a third (report-service draft assembly)
was the only built path. That could have been a 20-minute check at
sprint planning instead of a mid-step fork.

**Fixture data is clinical content, and we authored it.** The
`anamnesis_intake` template, the 239 ICD-10 codes and every voice
alias were written by engineering because the sprint could not proceed
without them. That is the right call for velocity and the wrong call
for clinical accuracy — hence three of the six carry-overs. Ideally
the clinical content lead is engaged *during* the sprint, not handed a
review queue at the end.

## Numbers worth remembering

- ICD-10 search p95 **1.73 ms** at 12k codes against a 50 ms budget —
  the budget was never the constraint; the data availability is.
- Command corpus: **0 false positives** before and after adding four
  specs — but only after two rounds of tightening.
- Extraction threshold **0.8**, unchanged all sprint. We deliberately
  did not tune it: with no clinician traffic, tuning would be fitting
  to ourselves.

## Carried into the pilot

The override-rate dashboard is the sprint's real deliverable in one
sense: everything else is a hypothesis about what clinicians want, and
that panel is the first instrument that can falsify it. The discipline
to protect is refusing to tune aliases or thresholds from dev-team
self-testing — the numbers only mean something with real users.
