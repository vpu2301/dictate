# Sprint 02 Retrospective

**Window:** 2026-05-09 → 2026-05-23
**Demo:** YYYY-MM-DD
**Participants:** ___________

Each prompt from § 7 of the post-sprint TODO is below. Answer in 2-4
sentences; signal-over-noise wins. If the answer is "we didn't have
that situation," say so — non-occurrences are interesting.

---

## 1. Did the property test actually catch a bug?

> If ceremonial, harden it (more tenants, weirder data shapes).

> _Your answer_

---

## 2. Was `tenant_connection` ergonomic in real code?

> Did engineers complain? If so, the cost might be too high — design alternatives.

> _Your answer_

---

## 3. Did anyone get tempted to bypass `tenant_connection` for performance?

> Document the impulse, push back, capture the rejection.

> _Your answer_

---

## 4. Pair-programming throughput cost

> How many minutes did pair-programming on security-adjacent code add? Was it worth it?

> _Your answer_

---

## 5. JCS canonicalisation in the audit hot path

> If hot-path latency p95 > 50 ms, evaluate caching strategies.

> _Your answer_

---

## 6. SERIALIZABLE-FOR-UPDATE contention

> On a real-tenant write-rate test (100 events/s), what was the failure rate?

> _We switched to READ COMMITTED + advisory lock + FOR UPDATE._
> _Your follow-up here._

---

## 7. Refresh-replay alert in the synthetic test

> Did it fire correctly? Was the alert payload useful (tenant, time, action taken)?

> _Your answer_

---

## 8. Permission matrix CSV — right level of abstraction?

> Or do we need finer-grained scopes already?

> _Your answer_

---

## 9. Admin invite + MFA enrol flow end-to-end

> Did it take more than 5 minutes for the test user? If yes, friction analysis before pilot.

> _Sprint 02 ships MFA disabled — only invite was exercised._
> _Your answer on invite friction._

---

## 10. Missing audit event kinds

> Walk every router; what should be emitting that isn't?

> _Your answer_

---

## Decisions to carry forward

- _Item 1_
- _Item 2_

## Decisions to revisit in sprint 3

- _Item 1_
- _Item 2_
