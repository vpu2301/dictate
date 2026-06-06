# Sprint 05 Retrospective — NLP Post-Processing & Voice Commands

Date: 2026-06-23.
Facilitator: tech lead.
Participants: backend, ML/MLOps, frontend, clinical content lead, linguist consultant, SRE, security.

## What worked

(Filled at retro.)

## What didn't

(Filled at retro.)

## Action items

| # | Item | Owner | By |
| - | ---- | ----- | -- |

## Spec retrospective prompts (canonical TODO §7)

1. Did the rule-based number normalizer get embarrassed by any clinical phrasing in pilot? Capture; expand corpus.
2. Did clinicians use voice commands the way we expected? Synonyms missing from vocabulary?
3. Did pause-before requirement get tuned per command type?
4. Was the punctuation model fast enough on partials, or did we keep partials text-only as designed?
5. Was the snapshot pattern ergonomic, or did engineers complain?
6. Did per-tenant abbreviation policy ever conflict with clinician muscle memory?
7. Did confidence-span character indexing drift between languages or after multiple stages?
8. Was the cache hit ratio meaningful in dev, or theatre?
9. Did the pilot session change TPR/FPR thresholds?
10. Were the latency budgets respected under 4 concurrent dictations + NLP?
