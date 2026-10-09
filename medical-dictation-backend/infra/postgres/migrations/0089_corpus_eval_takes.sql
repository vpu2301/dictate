-- 0089: corpus_eval_takes — server-side persistence for the WER eval
-- recorder (closes the "takes exist only in this tab" gap: the console
-- recorder produced session-local audio and a manually-committed ZIP; now
-- takes land here so a labelling job survives the tab, aggregates across
-- colleagues, and exports as one archive).
--
-- PRIVACY STANCE. These recordings are NOT patient audio and are stored in
-- plaintext deliberately: the recorder forbids free-form capture (scripted
-- lines only, enforced again server-side — the route refuses any take whose
-- script_id/text is not in the vendored eval script), and the corpus's
-- destination is the public-ish repo at eval/corpus/v1/ where it lives
-- unencrypted anyway. Envelope-encrypting a synthetic script on its way to a
-- git checkout would be theater. The PII regex sweep at commit time
-- (scripts/eval/check_corpus_pii.py) stays the backstop it always was.
--
-- The say/transcript/language/specialty/subset columns are a SNAPSHOT of the
-- script row at record time: if the script text is later edited, the audio
-- on file still matches the words that were actually read, so the export
-- stays internally consistent instead of silently pairing old audio with new
-- gold text.

BEGIN;

CREATE TABLE corpus_eval_takes (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    script_id     text        NOT NULL
        CONSTRAINT eval_take_script_id_chk CHECK (script_id ~ '^[a-z0-9][a-z0-9_-]{0,119}$'),
    script_version text       NOT NULL DEFAULT 'v1',
    recorded_by   uuid        NOT NULL,   -- users.sub, soft ref (0083 reviewed_by pattern)
    language      text        NOT NULL CHECK (language IN ('uk', 'en', 'de')),
    specialty     text        NOT NULL,
    subset        text,                    -- NULL = baseline line
    say           text        NOT NULL CHECK (char_length(say) BETWEEN 1 AND 500),
    transcript    text        NOT NULL CHECK (char_length(transcript) BETWEEN 1 AND 500),
    condition     text        NOT NULL CHECK (
        condition IN ('headset', 'laptop-mic', 'phone-speaker-distance', 'noisy')),
    duration_ms   integer     NOT NULL CHECK (duration_ms BETWEEN 300 AND 120000),
    sample_rate   integer     NOT NULL CHECK (sample_rate = 16000),
    audio_sha256  text        NOT NULL CHECK (audio_sha256 ~ '^[0-9a-f]{64}$'),
    -- 16 kHz mono PCM16 WAV ≈ 32 KB/s: 4 MiB caps a take at ~2 min, twice
    -- the duration ceiling above.
    audio_wav     bytea       NOT NULL CHECK (octet_length(audio_wav) BETWEEN 44 AND 4194304),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- The corpus layout holds ONE audio.wav per utterance id, so a tenant
    -- keeps one take per script line; re-recording replaces it.
    CONSTRAINT eval_take_one_per_line UNIQUE (tenant_id, script_id)
);

CREATE INDEX corpus_eval_takes_tenant_idx ON corpus_eval_takes (tenant_id, created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON corpus_eval_takes TO app_role;

ALTER TABLE corpus_eval_takes ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_takes FORCE ROW LEVEL SECURITY;

-- Plain tenant scoping: takes are tenant-local work product (unlike the
-- global corpus_candidates rows) — no SECURITY DEFINER path required.
CREATE POLICY eval_takes_tenant_all ON corpus_eval_takes
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

COMMIT;
