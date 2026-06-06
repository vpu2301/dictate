# Glossary

Terms used across the platform. New terms are added in the sprint that
introduces them.

| Term | Meaning |
| ---- | ------- |
| **Tenant**            | A clinic or organisation. Isolated end-to-end via row-level security. |
| **RLS**               | Row-Level Security — Postgres policy filtering rows by `current_setting('app.tenant_id')`. |
| **`tenant_connection`** | The single sanctioned helper that scopes a DB connection to a tenant (ADR-0004). |
| **KEK / DEK**         | Key Encryption Key / Data Encryption Key — envelope encryption (Sprint 03). |
| **KEP**               | Кваліфікований електронний підпис — Ukrainian Qualified Electronic Signature (Sprint 09). |
| **ASR**               | Automatic Speech Recognition. |
| **NLP**               | Natural Language Processing. |
| **Encounter**         | A clinical visit record (Sprint 11). |
| **Dictation session** | A real-time WebSocket dictation flow (Sprint 04). |
| **Audit chain**       | Hash-chained append-only audit log (Sprint 02). |
| **JCS**               | JSON Canonicalization Scheme (RFC 8785) — used for stable hashing of structured data. |
| **OTLP**              | OpenTelemetry Line Protocol — gRPC + HTTP transport for telemetry. |
| **Distroless**        | Minimal container base image without a shell or package manager (ADR-0002). |
| **Secret[T]**         | Typed wrapper that refuses to leak via repr / str / format / JSON / pickle (ADR-0003). |
| **Conventional Commits** | Commit-message format (`feat:`, `fix:`, `chore:`, …). Enforced by `commitizen`. |
| **PII**               | Personally Identifiable Information. |
| **PHI**               | Protected Health Information — strict subset of PII regulated by HIPAA-equivalents. |
| **Problem Details**   | RFC 9457 — JSON envelope for HTTP error responses with `type` / `title` / `status` / `detail` / `instance`. |
| **`urn:uuid:`**       | URN form used for the `instance` field in our Problem Details — ties the user-visible error to the trace. |
| **JWT**               | JSON Web Token. Compact, signed authentication artefact (RFC 7519). RS256-only in our system. |
| **JWKS**              | JSON Web Key Set — Keycloak's public-key endpoint that `libs/auth.JwksCache` consumes. |
| **`kid`**             | Key ID — JWT header field that selects which JWKS entry verifies the signature. |
| **RS256**             | RSA + SHA-256 signature — the only JWT alg `libs/auth.verify_token` accepts. |
| **Tenant context**    | The `app.tenant_id` Postgres setting; the predicate every RLS policy reads from. Set per-transaction by `tenant_connection`. |
| **RLS policy types**  | PERMISSIVE (allow if any policy matches) vs RESTRICTIVE (must match in addition). We use both for defence in depth. |
| **Argon2id**          | Memory-hard password hashing function — chosen for recovery-code hashing (Sprint 16+). |
| **TOTP**              | Time-based One-Time Password (RFC 6238) — second-factor primitive. MFA disabled in sprint 02 pilot. |
| **SSO**               | Single Sign-On — one Keycloak session, many client tokens. |
| **Refresh rotation**  | Keycloak issues a new refresh token on every refresh and invalidates the old. Replay → sec event. |
| **Refresh replay**    | Presentation of a previously-rotated refresh token — always anomalous. Triggers `RefreshReplayDetected` alert. |
| **Brute-force lockout** | Keycloak's per-account rate limit on failed logins. Sprint 02 default: 5 fail / 60 s. |
| **`audit_writer` role**| Postgres role with `INSERT, SELECT, UPDATE` on `audit.events`. The UPDATE is for `SELECT FOR UPDATE` row locks; the immutability trigger blocks actual updates. (ADR-0008) |
| **ContextVar**        | Python stdlib per-Task scoped variable used to carry verified claims through a request without explicit passing (`libs/auth.context`). |
| **`FOR UPDATE`**      | SQL row-lock clause. Used in `AuditWriter` to serialise last-seq reads alongside the per-tenant advisory lock. |
| **SERIALIZABLE**      | Postgres strictest isolation level; rejected for audit writes in favour of READ COMMITTED + advisory lock (ADR-0008). |
| **Permission matrix** | `docs/auth/permissions.csv` — the source-of-truth for `(role, action, target_kind) → allowed`. The `requires()` dep consults the matching `ALLOW` dict in `libs/auth.perms`. |
| **`authz.denied`**    | Audit event kind emitted by the `requires()` dep on every 403. Severity `sec`. |
| **VAD**               | Voice Activity Detection — splits an audio clip into speech regions. Silero v4 is the sprint-03 default. |
| **KEK_master / KEK_tenant / DEK_object** | The three-layer key hierarchy in `libs/crypto`. See ADR-0011. |
| **AAD**               | Additional Authenticated Data — bytes input to AES-GCM that aren't encrypted but are bound to the tag. We use `tenant_id.bytes \|\| caller_aad`. |
| **AEAD**              | Authenticated Encryption with Associated Data. AES-256-GCM is the AEAD we use everywhere. |
| **GCM**               | Galois/Counter Mode — the AES mode that produces both ciphertext and a 16-byte authentication tag. |
| **fp16**              | 16-bit floating point — Whisper's preferred CUDA compute type, 2× faster than fp32 with no quality loss on this model. |
| **faster-whisper**    | CTranslate2-backed Whisper inference library. Sprint 03's chosen engine (ADR-0009). |
| **Silero VAD**        | Open-source neural VAD model. Used to find speech regions before Whisper. |
| **Beam search**       | Whisper decoding strategy that explores N candidate transcripts in parallel; default beam_size=5. |
| **Initial prompt**    | A short text fed to Whisper at the start of a chunk to bias the transcript toward in-domain vocabulary (e.g., cardiology terminology). |
| **Realtime factor**   | audio_duration / infer_seconds. > 1 means faster-than-realtime. |
| **WER**               | Word Error Rate — Levenshtein-based metric (insertions + deletions + substitutions) / ref-word-count. Lower is better. |
| **Magic-byte sniff**  | Comparing the first few bytes of a file against known format signatures (e.g., `RIFF…WAVE` for WAV). |
| **Pre-signed URL**    | A short-TTL S3 URL that grants time-bounded GET access to a single object without ambient credentials. |
| **Idempotency key**   | A stable identifier (we use the job UUID) that lets a duplicate delivery be detected and skipped. |
| **Consumer group**    | Redis Streams primitive that lets multiple consumers share work on one stream with at-least-once semantics. |
| **`XAUTOCLAIM`**      | Redis Streams command that reassigns pending messages to a new consumer once they've been idle longer than a threshold. |
| **DLQ**               | Dead Letter Queue — sibling stream where messages land after exceeding their retry budget. |
| **CTranslate2**       | Inference engine library that compiles transformer models for CPU/GPU; faster-whisper builds on it. |
| **`pcm_s16le`**       | 16-bit little-endian PCM — the default WAV codec; one of the allow-listed codecs in sprint 03. |
| **WebSocket subprotocol** | Sec-WebSocket-Protocol header value identifying the application-layer contract on top of WS framing. `medical-dictation.v1` is the sprint-04 contract. |
| **Partial vs final**  | A `partial` segment may be revised on the next window; a `final` is immutable. Sprint-04 commitment policy graduates partials → finals. |
| **IndexedDB ring buffer** | Browser-side ring of recent audio frames that lets the frontend replay after a network drop. Frontend owns the implementation. |
| **Sliding window**    | The streaming Whisper strategy: each window is `window_s = 4.0` of audio; consecutive windows overlap by `overlap_s = 2.0`. |
| **Finalize**          | End-of-session action: flush windower, encrypt + upload audio, persist transcript. |
| **Retransmit range**  | `RetransmitRange{from_seq, to_seq}` — client asks the server to consider that range as the source of truth; server dedupes already-received seqs. |
| **Sequence number**   | 4-byte BE header on every binary audio frame; the server uses it for ordering, dedup, and gap detection. |
| **Heartbeat**         | Application-level keep-alive emitted by the server every 10 s and required from the client every 35 s. |
| **Token-expiring proactive refresh** | Server emits `token_expiring` at T-60 s before the bearer's `exp`; client posts a fresh token before expiry. |
| **RTF (realtime factor)** | `audio_seconds / wall_seconds`. > 1 means faster than realtime. |
| **tmpfs ring buffer** | Per-session in-memory ring backed by `/run/dictation/<sid>/audio.bin` (mode 0700, 0600 file), AES-CTR encrypted with an ephemeral DEK. |
| **`no_speech_prob`**  | Whisper's per-segment confidence that the audio is non-speech. > 0.6 → commitment policy drops the segment. |
| **Boundary uncertainty** | Normalised Levenshtein distance between two consecutive windows' overlap-region transcriptions; > 0.30 → server emits `low_confidence` warning. |
| **Cross-tab guard**   | Server-side rejection of a second WS connection to the same session_id while a first one is still attached. |
| **BroadcastChannel**  | Browser API the frontend uses to coordinate between tabs of the same origin BEFORE attempting a server-side resume. |
| **Opus VOIP**         | The Opus codec in its 16-kHz voice profile, 20-ms frames; the wire codec for binary audio frames in sprint-04. |
| **GPU full**          | Wire error `gpu_full` — per-worker session cap (4) reached; recoverable, client retries. |
| **Worker failure**    | Wire error `worker_failed` — inference process crashed; non-recoverable; client recovers via batch path. |
| **Idle timeout**      | 35-s silence from the client closes the WS; session enters `reconnecting`. |
| **Abandoned**         | Terminal state after 30 min in `reconnecting` with no resume. |
| **Paused / Resume**   | Client-initiated suspension of audio flow; transcript preserved. |
| **Reconnecting**      | Live state where the WS is gone but the session is recoverable within the abandon window. |
| **AES-CTR**           | AES in Counter mode — used for the tmpfs ring buffer (unauthenticated; the envelope at rest is AEAD GCM). |
| **Pipeline stage**    | A single transformation in the sprint-05 NLP chain. 6 stages: voice_commands → punctuation → number_norm → date_norm → abbreviation → confidence. |
| **Idempotence key**   | A SHA-256 hash over (input, context, pipeline_version, snapshot fingerprint). The orchestrator caches by this key in Redis; same input + same context → byte-equal output. |
| **Abbreviation snapshot** | Immutable per-request view of the merged (tenant + global) abbreviation dictionary. Read once at request entry; admin edits in-flight don't affect the current request. |
| **Voice command intent** | The semantic label (`newparagraph`, `period`, `section.diagnosis`, …) carried by a `CommandSlot`. Distinct from the wire `Operation` the frontend executes. |
| **FSM matcher**       | The voice-command detector. Three gates: pause-before, confidence, edit-distance. Sprint-05 ships a longest-match scan over the catalogue; a trie is reserved for sprint-17 if the catalogue grows. |
| **Edit-distance tolerance** | The matcher accepts at most 1 substitution per phrase, with Levenshtein distance ≤ 2 — defends against Whisper one-letter errors. |
| **Pause-before**      | Required silence before a command head fires. Defaults from 200 ms (newline) to 350 ms (sections / stop_dictation). |
| **Voice-command false-positive rate** | Fraction of fired commands the clinician undoes within 600 ms. Alert > 5% / 1 hour. |
| **Undo rate**         | Same as voice-command false-positive rate. Tracked via the `voice_command.undone` audit kind (frontend-emitted). |
| **Confidence span**   | A character range in the post-processed text with a `high_concern` or `moderate` label, derived from per-word Whisper probabilities. |
| **Reference date**    | The anchor for relative-date parsing. Caller-supplied; server fills from `now()` with a `missing_reference_date` warning if omitted. |
| **Ambiguous date**    | A date that fails Python's calendar validation (e.g., `31.04.2026`). Passed through unchanged; emits `Warning{code="ambiguous_date"}` for sprint-08 clinical rules. |
| **Specialty context** | `ProcessingContext.specialty` — used by the abbreviation stage's domain filter to disambiguate (e.g., `ІМ` in a cardiology session). |
| **Direction (compact / expand / either)** | Per-row policy in the abbreviation dictionary. `compact` writes the abbreviation; `expand` writes the expansion; `either` passes through. |
| **Tenant override**   | A row in `abbreviation_dictionary` with `tenant_id IS NOT NULL` — wins on collision with a global rule on the same `(language, expanded, abbreviated)`. |
| **Pipeline version**  | Constant in code (`PIPELINE_VERSION` = `"nlp-v1.0.0"`) participating in the idempotence cache key. Bump invalidates every cached result. |
