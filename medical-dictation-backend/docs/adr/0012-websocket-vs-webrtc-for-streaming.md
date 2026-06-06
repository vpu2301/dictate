# ADR-0012 — WebSocket vs WebRTC vs gRPC-Web for streaming dictation

**Date:** 2026-06-05
**Status:** Accepted
**Deciders:** tech lead, frontend lead, SRE/DevOps lead, security lead

---

## Context

Sprint 04 ships the user-facing real-time dictation surface. The
transport must:

- Carry low-latency bidirectional audio (~20 ms frames) + control text.
- Work in browsers without a per-installation client.
- Traverse corporate proxies and TLS terminators.
- Be authenticatable with the existing Keycloak bearer tokens.
- Tolerate flaky mobile networks (reconnect, retransmit).

Options:

| Transport     | Browser? | Latency | Proxy-friendly | Codec flexibility | Complexity |
| ------------- | -------- | ------- | -------------- | ----------------- | ---------- |
| WebSocket     | ✅       | ~10 ms  | ✅ (CONNECT-grade) | full (binary frames) | low |
| WebRTC        | ✅       | ~5 ms   | ⚠️ (STUN/TURN) | Opus-by-spec      | high |
| gRPC-Web      | ✅       | ~50 ms  | ⚠️ (some HTTP/2-proxies break) | proto-bound | medium |
| Long-poll HTTP| ✅       | ~200 ms | ✅              | per-request       | low |

## Decision

Use **WebSocket** with subprotocol `medical-dictation.v1`. The wire is:

- text frames: JSON discriminated-union messages (`StartSession`,
  `Partial`, `Final`, …).
- binary frames: `[4-byte BE seq][opaque Opus payload]`.

Subprotocol negotiation gives us a versioning hook (`medical-dictation.v2`
in sprint 14 for diarization).

## Consequences

- **Proxy compatibility**: WebSocket goes over HTTP/1.1 Upgrade, which
  every load balancer and corporate proxy supports today. WebRTC's
  UDP/SRTP path is regularly blocked.
- **No TURN servers**: WebRTC's UDP-first model requires TURN for
  ~10–30% of corporate networks. WebSocket has zero TURN footprint.
- **Latency floor**: WebRTC would shave ~5 ms but at huge ops cost;
  ASR inference is the dominant latency contributor (sprint-04 §9
  targets are GPU-bound, not transport-bound).
- **Codec flexibility**: binary WS frames are opaque; the codec is a
  client/server agreement (Opus 16 kHz mono VOIP profile in sprint 4).
  WebRTC would lock us into Opus by spec — same outcome, less freedom.
- **Audit + auth model**: bearer JWT validated at the upgrade; same
  pattern as the rest of the platform. WebRTC needs per-track auth
  signalling that doesn't compose with Keycloak.
- **Reconnect model**: WS close + retry with `resume_session_id` is
  trivial. WebRTC ICE-restart is a larger surface.

## Migration path off WebSocket

If a regulator or a future proxy generation forces a move, the
`medical-dictation.v1` protocol is transport-agnostic (JSON + binary
frames). It can run over HTTP/2 chunked POST (sprint 16 considers as
a fallback for WS-blocked networks) or gRPC-Web with minimal code
changes — the messages and codec layers don't care which carrier
fragmented them.

## Alternatives considered

- **WebRTC**: rejected — proxy hostility, TURN ops cost, no real
  latency win in our pipeline.
- **gRPC-Web**: rejected — fewer proxy compatibilities than WS in the
  browsers we target; harder to debug; subprotocol versioning weaker.
- **HTTP/2 chunked POST**: kept on the bench as the fallback for
  WS-blocked corporate networks; sprint 16 evaluates.
- **Long-polling**: latency floor too high for real-time dictation;
  rejected.

## Trigger conditions for revisiting

- Adoption blocked in any tenant due to corporate proxy stripping
  `Upgrade: websocket`.
- Regulator requires UDP-style media transport (unlikely).
- A streaming-native ASR model lands that's tightly coupled to WebRTC
  (unlikely; ADR-0013 covers the model question).
