# Architecture Decision Records

ADRs capture decisions whose reversal would be expensive — choices about
runtimes, build surfaces, security primitives, and contracts other code
depends on. Casual decisions (file layout inside a service, choice of HTTP
status code for a niche error) do not need ADRs.

Numbering is monotonic and global. Sprint 02 starts at ADR-0006; sprint
03 starts at ADR-0009.

| #     | Title                                                                            | Status   |
| ----- | -------------------------------------------------------------------------------- | -------- |
| 0001  | [Python version pin and `uv` workspace](0001-python-version-and-uv.md)           | Accepted |
| 0002  | [Distroless, nonroot production containers](0002-distroless-nonroot-container.md)| Accepted |
| 0003  | [Typed `Secret[T]` wrapper](0003-secret-typed-wrapper.md)                        | Accepted |
| 0004  | [Single-helper tenant connection (`tenant_connection`)](0004-rls-tenant-connection.md) | Accepted |
| 0005  | [Observability stack (logs / traces / metrics)](0005-observability-stack.md)     | Accepted |
| 0006  | [Keycloak as Identity Provider](0006-keycloak-as-idp.md)                         | Accepted |
| 0007  | [RLS-first tenant isolation](0007-rls-first-tenant-isolation.md)                 | Accepted |
| 0008  | [Hash-chained audit log + `audit_writer` escape hatch](0008-hash-chained-audit-log.md) | Accepted |
| 0009  | [Inference engine: faster-whisper](0009-faster-whisper-inference-engine.md)      | Accepted |
| 0010  | [Queue tech: Redis Streams (jobs) + Kafka later](0010-redis-streams-for-asr-jobs.md) | Accepted |
| 0011  | [3-layer encryption envelope](0011-three-layer-encryption-envelope.md)           | Accepted |
| 0012  | [WebSocket vs WebRTC for streaming dictation](0012-websocket-vs-webrtc-for-streaming.md) | Accepted |
| 0013  | [Whisper streaming windowing (4s + 2s overlap)](0013-whisper-streaming-windowing.md) | Accepted |
| 0014  | [Punctuation model selection](0014-punctuation-model-selection.md)               | Accepted |
| 0015  | [Rule-based number normalization](0015-rule-based-number-normalization.md)       | Accepted |
| 0016  | [JSONB template schema + cosmetic-vs-structural rule](0016-jsonb-template-schema.md) | Accepted |

## Template

```markdown
# ADR-NNNN — Title

**Date:** YYYY-MM-DD
**Status:** Proposed | Accepted | Deprecated | Superseded by ADR-NNNN
**Deciders:** <names / roles>

---

## Context

What changed in the world that forces a choice now? What constraints?

## Decision

What we are doing. Be unambiguous.

## Consequences

Positive and negative effects. Be honest about the cost.

## Alternatives considered

What we rejected and why.

## Trigger conditions for revisiting

What signal would make us re-open this decision?
```

## Authoring rules

- Number monotonically. Don't reuse a number even after deprecation; mark
  the original as `Superseded by ADR-XXXX` and link forward.
- Keep ADRs short. Two pages is the upper bound. If you need more, the
  decision is several decisions; split.
- Reference the ADR from the affected code (`libs/secret/README.md` →
  ADR-0003). Discoverability matters more than completeness.
