# `medical-dictation.v1` — WebSocket Streaming Protocol

**Status:** stable (sprint 04). Breaking changes require `v2`.
**Endpoint:** `wss://<host>/ws/dictate`
**Subprotocol:** `medical-dictation.v1`

## Connection upgrade

The client opens a WebSocket with:

- `Sec-WebSocket-Protocol: medical-dictation.v1`
- `Authorization: Bearer <jwt>` (or `?token=<jwt>` query param —
  browser WebSocket APIs can't set arbitrary headers).
- `Origin` in the configured allow-list (production-only enforcement).

The server validates BEFORE accepting:

| Failure                                                    | HTTP code | Body code            |
| ---------------------------------------------------------- | --------- | -------------------- |
| Per-IP rate limit (10/min)                                 | 429       | `rate_limited`       |
| Subprotocol absent / wrong                                 | 400       | `unsupported_protocol` |
| Bearer missing / malformed                                 | 401       | `auth_invalid`       |
| Token expired / signature invalid / wrong issuer / audience | 401      | `auth_invalid`       |
| Per-user rate limit (30/hour)                              | 429       | `rate_limited`       |
| Origin not in allow-list (prod only)                       | 403       | `origin_rejected`    |

A rejection writes `dictation.upgrade.failed` audit (severity = `warn`
for benign, `sec` for repeated 401 / `rate_limited_user`).

## State machine

```
creating ──► active ──► finalized
              │ ▲
              ▼ │
            paused
              │
              ▼
          reconnecting ──► finalized | abandoned | failed
              │
              ▼
             failed
```

## Cadence

- Server emits `heartbeat` every **10 s**.
- Server expects ANY client message every **35 s**; silence beyond
  that closes the WS with code 1011 → session moves to `reconnecting`.
- Idle in `reconnecting` for **30 min** → `abandoned`.
- Hard cap per session: **60 min**.

## Single-tab guard

If a live WS is already attached to a `session_id`, a second
`start_session{resume_session_id}` request is rejected with
`session_not_found`. The frontend uses a BroadcastChannel before
attempting resume to detect a duplicate tab client-side.

## Client → server messages

All client messages MUST include `protocol_version: 1` (default). Extra
fields are rejected (`extra="forbid"`).

| Type              | Fields                                                         | Notes |
| ----------------- | -------------------------------------------------------------- | ----- |
| `start_session`   | `prompt_id`, `language` (`uk`|`en`), `target_kind` (default `generic`), `encounter_id?`, `template_id?`, `resume_session_id?` | First message after upgrade |
| `refresh_token`   | `token`                                                        | Replaces the bearer; must have same `sub`+`tid` |
| `end_session`     | —                                                              | Initiates finalize |
| `pause`           | —                                                              | Audio frames now rejected with `pause_state_mismatch` |
| `resume`          | —                                                              | Resume from `paused` |
| `retransmit_range`| `from_seq`, `to_seq`                                           | Server is permissive: already-received seqs are deduped |
| `switch_section`  | `section_id`, `reason` (`voice_command`/`user_click`/`programmatic`) | **Sprint-06 additive (ADR-0016 amendment).** Swaps the ASR prompt for the next Whisper window. Server rejects with `bad_message` if `section_id ∉ template`. v1 clients that never send it are unaffected. |

## Binary audio frames

`[4-byte BE seq][opaque Opus bytes]`. The codec is Opus 16-kHz mono
VOIP profile, 20-ms frames. Limits:

- 5 ≤ size ≤ 8192 bytes.
- Frames > 8 KB or < 5 bytes → server emits `bad_message` + close.

## Server → client messages

| Type                  | Notes                                                       |
| --------------------- | ----------------------------------------------------------- |
| `session_started`     | First message after `start_session` accepted; carries `session_id`, `resumed` flag, `last_committed_seq`, `committed_audio_until_ms`, `model`, `language` |
| `partial`             | Provisional segment; may be revised on next window           |
| `final`               | Committed segment; `is_provisional: false`; `voice_command: null` reserved for sprint 05 |
| `voice_command`       | Reserved for sprint 05; not emitted in sprint 04             |
| `warning`             | Non-fatal; e.g., `low_confidence`, `high_latency`            |
| `heartbeat`           | Every 10 s                                                   |
| `token_expiring`      | T-60 s before JWT expiry; client should send `refresh_token` |
| `session_terminated`  | Reason: `normal`, `cap_reached`, `token_expired`, `worker_failure`, `force_finalize` |
| `error`               | See error catalogue                                          |

## Error catalogue

| Code                  | Recoverable | Cause                                     |
| --------------------- | ----------- | ----------------------------------------- |
| `bad_message`         | varies      | Wire/protocol violation                   |
| `unsupported_protocol`| no          | Subprotocol mismatch                      |
| `auth_invalid`        | no          | JWT verification failure                  |
| `pause_state_mismatch`| yes         | Audio sent while paused                   |
| `retransmit_too_large`| yes         | Range > 1500 frames (30 s)                |
| `session_not_found`   | no          | Uniform-failure for resume gate failures  |
| `rate_limited`        | yes         | Per-IP / per-user / per-tenant limit hit  |
| `worker_failed`       | no          | Inference worker died                     |
| `audio_decode_failed` | yes (≤ 5)   | Opus decode error; 5 consecutive → fatal  |
| `gpu_full`            | yes         | Per-worker session cap reached            |
| `gap_detected`        | yes         | Seq gap > 50 frames; client should retransmit |
| `high_latency`        | yes         | Per-window deadline missed                |
| `worker_overloaded`   | yes         | 3 consecutive deadline misses             |
| `low_confidence`      | yes         | Boundary uncertainty above threshold      |
| `token_expired`       | no          | JWT expired without refresh               |
| `internal`            | no          | Unhandled server error                    |

## Reconnection sequence

1. WS closes (any cause).
2. Client persists the IndexedDB ring (sprint-04 FE responsibility).
3. Client re-opens WS with the SAME bearer token (or refresh first).
4. Client sends `start_session{resume_session_id: <id>}`.
5. Server runs the resume gate (auth + state + time + worker + single-tab). On failure: `session_not_found`.
6. On success: `session_started{resumed: true, last_committed_seq, committed_audio_until_ms}`.
7. Client may send `retransmit_range{from_seq, to_seq}` to replay frames the server hadn't acked. Server dedupes ≤ HWM silently.

## Hand-off to sprint 5 (NLP) and sprint 14 (diarization)

- Sprint 5 fills `voice_command` on `final`; field shape locked here.
- Sprint 14 forks to `medical-dictation.v2` adding diarization fields.
  v1 clients reject v2 messages cleanly (`extra="forbid"`).
