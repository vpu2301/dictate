# Sprint-16 deployment — measured resource envelopes

Basis for the chart's requests/limits. Two measurement sources:

1. **Idle resident set** — `docker stats` over the full compose stack
   (2026-08-08, Apple-silicon Docker VM, CPU inference):

| Service | Idle RSS | Idle CPU | Chart request → limit |
|---|---|---|---|
| dictation-service (large-v3 + diarizer, CPU) | 2.01 GiB | 0.2% | 3Gi/500m → 5Gi/4 |
| asr-worker (large-v3, CPU) | 2.61 GiB | 0.03% | 3Gi/500m → 5Gi/4 |
| nlp-service | 729 MiB | 9.5% | 1Gi/250m → 2Gi/2 |
| keycloak | 738 MiB | 0.1% | (stateful; operator-managed in prod) |
| kafka | 311 MiB | 0.8% | dropped (unused — inventory.md) |
| asr-service | 141 MiB | 0.3% | 192Mi/100m → 768Mi/1 |
| report-service | 102 MiB | 8.8% | 192Mi/100m → 768Mi/1 |
| core-service | 96 MiB | 0.2% | 192Mi/100m → 768Mi/1 |
| notification-service | 90 MiB | 0.2% | 128Mi/100m → 512Mi/1 |
| autocomplete-service | 82 MiB | 8.4% | 160Mi/100m → 512Mi/1 |
| generation-service | 75 MiB | 9.5% | 128Mi/100m → 512Mi/1 |
| auth-service | 59 MiB | 2.0% | 128Mi/100m → 512Mi/1 |
| signing-service | 53 MiB | 0.2% | 128Mi/100m → 512Mi/1 |

2. **Under session load** — 4 concurrent streaming sessions on one
   dictation pod (the KEDA scale-out proof run) stayed inside the 5 Gi
   limit with the tiny model; the compose overlay's measured large-v3
   numbers (3.0 GiB dictation resident incl. diarizer, override-file
   comment) are the CPU floor. **GPU envelopes (prod)** come from the
   sprint-14 deployment measurements — weight budget
   `MDX_PER_WORKER_MAX_SESSIONS` per pod, one pod per GPU; re-measure on
   the real rig at bring-up (asr-worker runbook's standing instruction).

The k6 suites (sprints 08/10) remain the load source for the CPU
services' HPA thresholds; the 70% CPU targets in values-prod are
initial and expected to be tuned against staging k6 runs on the real
cluster hardware — the numbers above are the measured starting point,
not folklore.
