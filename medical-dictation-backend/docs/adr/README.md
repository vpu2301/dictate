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
| 0017  | [HF Space embedded demo stack](0017-hf-space-embedded-stack.md)                  | Accepted |
| 0018  | [Demo privacy contract (tmpfs-only)](0018-demo-privacy-contract.md)              | Accepted |
| 0019  | [WER standing release gate](0019-wer-standing-release-gate.md)                   | Accepted |
| 0020  | [Append-only report versioning](0020-append-only-versioning.md)                  | Accepted |
| 0021  | [Postgres `simple` FTS for reports](0021-postgres-simple-fts.md)                 | Accepted |
| 0022  | [PAdES-LTV canonical PDF](0022-pades-ltv-canonical-pdf.md)                       | Accepted |
| 0023  | [Signing provider abstraction](0023-signing-provider-abstraction.md)             | Accepted |
| 0024  | [Canonical JSON via JCS](0024-canonical-json-via-jcs.md)                         | Accepted |
| 0025  | [Autocomplete trie + Redis cache](0025-autocomplete-trie-redis.md)               | Accepted |
| 0026  | [Server-side file-key signing via UAPKI; dev_password scaffold](0026-file-key-uapki-and-dev-password-scaffold.md) | Accepted |
| 0027  | [Patient identity (ІПН) & crypto-shredding strategy](0027-patient-identity-and-crypto-shredding.md) | Accepted |
| 0028  | [Privacy-ops deployment: erasure job isolation, backups vs erasure, export lifecycle](0028-privacy-ops-deployment.md) | Accepted |
| 0029  | [Redis Streams as the notification event bus](0029-redis-streams-notification-bus.md) | Accepted |
| 0030  | [Redis pub/sub for cross-worker WebSocket fan-out](0030-redis-pubsub-ws-fanout.md) | Accepted |
| 0031  | [Notification email carries pointers, never PHI](0031-email-carries-pointers-not-phi.md) | Accepted |
| 0032  | [Typed anamnesis field extraction stage](0032-field-extraction-stage.md) | Accepted |
| 0033  | [Administrators are separated from PHI; break-glass is the door](0033-admin-phi-separation-and-break-glass.md) | Accepted |
| 0034  | [Speaker-diarization backend — Silero VAD + ECAPA + online clustering](0034-diarization-backend-silero-ecapa.md) | Accepted |
| 0035  | [Conversation capacity — single mixed worker pool with weighted caps](0035-conversation-fleet-single-mixed-pool.md) | Accepted |
| 0036  | [Layer C inline completion — local Gemma behind a provider seam](0036-layer-c-inline-completion-local-gemma.md) | Accepted |
| 0037  | [Audio replay — clip-on-demand over the GCM envelope, token-streamed](0037-audio-replay-clip-pipeline.md) | Accepted |
| 0038  | [Search query expansion — synonym dictionary over `simple` FTS](0038-search-query-expansion.md) | Accepted |
| 0039  | [MFA/TOTP — auth-service proxy enforcement, envelope-encrypted secret in Keycloak attributes](0039-mfa-totp-auth-service-proxy.md) | Accepted |
| 0040  | [Session revocation — auth-service-pushed Redis denylist, fail-open checks](0040-session-revocation-denylist.md) | Accepted |
| 0041  | [Scheduled jobs — shared in-process runner per service, CLI twin for cron](0041-in-process-scheduler-pattern.md) | Accepted |
| 0042  | [HTTP/2 POST streaming fallback — closed with data, not built](0042-http2-post-fallback-closed.md) | Accepted |
| 0043  | [Clinical corpus governance — provenance, tiers, releases](0043-corpus-governance.md) | Accepted |
| 0044  | [LLM-assisted corpus review — jury, calibration, PHI boundary](0044-llm-assisted-corpus-review.md) | Accepted |

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
