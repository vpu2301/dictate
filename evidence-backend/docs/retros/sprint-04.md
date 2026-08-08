# EVA-S04 — Retro

## What the sprint was betting on

That QS1 could be made a *property* rather than a promise, before the code
that would violate it exists. The bet paid: three layers landed, and the
third one — the runtime taint harness that had "nothing to catch" in a sprint
with no patient mode — caught a real gap in the identifier sanitizer on its
first run. A five-digit MRN suffix walked past a check that only looked for
six-digit runs.

The lesson generalizes: **a gate written for a future risk still finds
present bugs**, because the property it tests ("no identifier-shaped string
leaves the cluster") was already half-broken.

## What went well

- **Gates found the bugs, not review.** Seven triage rule defects, an
  order-dependent allowlist merge, two layering violations, a backwards title
  heuristic — every one surfaced from a gate or a test written to state a
  property, not from reading the code. The triage gate in particular paid for
  itself immediately: hand-reading those regexes would never have found that
  `дозуванн` matches inside `передозування`.
- **Adding import-linter was overdue and cheap.** The workspace had custom AST
  gates but no layering enforcement; turning it on immediately caught two real
  violations in code written the same day. It should have landed in S00.
- **Fakes that actually stream.** The gateway fake yields 7-character chunks
  mid-line. A `MagicMock` returning whole lines would have passed against a
  parser that could not handle partial lines — which is exactly the parser a
  real model would break.
- **The streaming/retry tension resolved cleanly** by reading the spec's own
  failure table instead of inventing a policy: nothing emitted → retry;
  something emitted → close with what validated and flag it.

## What was harder than expected

- **The triage double bind.** Deflect every live emergency, refuse to deflect
  any reference question about one. Both directions had to be gated — a
  100%-recall rule list that also refuses "what is the protocol for
  anaphylaxis?" is worse than no rule list, and only a gate on the *false*
  side keeps that honest. Five iterations to reach 100/0.
- **QS1's import contract cannot mean what it looks like it means.**
  `evidence_models/__init__` re-exports everything, so any import of the
  package transitively imports `snapshot`. The contract had to be
  direct-imports-only, with a separate symbol-level gate doing the real work.
  This is documented in three places precisely because it reads like a
  loophole.
- **Two paths to the internet was the tempting design.** `evidence-answer`
  calling `evidence-websearch` directly is one less hop and better latency.
  It also means two places to enforce QS1 forever. The extra hop was the
  right call and the latency cost is hidden by the late-source split anyway.

## What we would do differently

- **Write the eval set before the rules.** The triage rules were written from
  intuition and then corrected by the gate, five times. Writing the 154 items
  first would have shaped better regexes on the first pass — the items *are*
  the specification.
- **Land layering enforcement with the first service, not the fourth.** Both
  violations were "the adapter needs a domain type", which is a five-minute
  fix on day one and a refactor once four services share the pattern.

## Risks carried forward

| Risk | State |
|---|---|
| **No live end-to-end run** — no real generator, no real SearXNG, no real fetch. Every path is proven against fakes and fixtures | Open (delta 9). The latency NFR is unmeasured, and "unmeasured" is the honest word |
| **Migration 0070 not cycled** against a live DB | Open (delta 8). Written to convention, unproven |
| **Triage rules are regex on two languages.** 100% on 154 curated items is not 100% on the world | Mitigated by the classifier as a second layer and by the gate as a regression net; the set must grow with real questions |
| **Extraction heuristics are ours**, not a battle-tested readability library | Deliberate (deterministic, testable, no dependency). Watch the garbage-skip rate once real pages flow |
| **SearXNG operational burden** — upstream engines can rate-limit or break | Accepted in ADR-0005 with a revisit trigger |
| Grafana boards still not built (joins the S03 debt) | Open |

## Prompts for the next retro

- Did the taint harness catch anything once patient context actually flowed
  (S05)? If not, is it looking in the right place?
- What did the first real fetches teach us about the extraction thresholds?
- How many of the 154 triage items survived contact with real clinician
  questions — and how many new items did those questions add?
