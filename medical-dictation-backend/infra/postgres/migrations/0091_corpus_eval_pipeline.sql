-- 0091: the eval corpus pipeline — authoring, publishing, scoring.
--
-- 0089 gave the recorder server-side takes. It stopped one step short of the
-- thing the recorder exists for: a WER number. Takes could only become a
-- measurement by downloading the ZIP, committing it into eval/corpus/v1/, and
-- running scripts/eval/run_wer.py on the GPU rig — three manual steps, so in
-- practice the recording happened and the WER never did.
--
-- This migration adds the three tables that close it:
--
--   corpus_eval_script_items    lines the tenant authored itself (with the
--                               same category/labels the vendored script
--                               carries), so the corpus can grow from the
--                               console instead of from a Python edit.
--   corpus_eval_snapshots(+_items)
--                               an immutable publication: the exact set of
--                               utterances, their gold text and their audio
--                               digests, frozen and PII-swept at publish
--                               time. A run scores a SNAPSHOT, never the
--                               live takes — otherwise a re-recording
--                               mid-run silently changes what was measured.
--   corpus_eval_runs(+_items)   one scoring pass over a snapshot: per
--                               utterance the ASR hypothesis, WER and CER;
--                               per run the aggregate and the model that
--                               produced it.
--
-- THE MODEL IS RECORDED, ALWAYS. Scoring runs through asr-service, which on a
-- developer laptop falls back to whisper `tiny` on CPU. Those numbers are
-- plumbing-only (docs/eval/wer-methodology.md); the release gate is large-v3
-- on the Linux/GPU rig. `corpus_eval_runs.model` is NOT NULL so no stored
-- number is ever separable from the engine that produced it.
--
-- PRIVACY. Same stance as 0089: scripted synthetic content, stored in
-- plaintext, destined for a git checkout where it lives unencrypted anyway.
-- What changes is that authored lines are text a human typed rather than text
-- vendored in the repo, so the PII sweep at the write boundary (and again over
-- the whole snapshot at publish) becomes load-bearing rather than a backstop.
-- Both sweeps are server-side; neither is skippable from a browser.

BEGIN;

-- ── authored script lines ─────────────────────────────────────────────
CREATE TABLE corpus_eval_script_items (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    script_id   text        NOT NULL
        CONSTRAINT eval_item_script_id_chk CHECK (script_id ~ '^[a-z0-9][a-z0-9_-]{0,119}$'),
    language    text        NOT NULL CHECK (language IN ('uk', 'en', 'de')),
    specialty   text        NOT NULL CHECK (char_length(specialty) BETWEEN 1 AND 64),
    -- NULL = a baseline line, exactly as in the vendored script. A non-NULL
    -- value is checked against the subset vocabulary in the service, not
    -- here: the six adversarial subsets are a product decision that moves
    -- faster than a migration.
    subset      text        CHECK (subset IS NULL OR char_length(subset) BETWEEN 1 AND 64),
    say         text        NOT NULL CHECK (char_length(say) BETWEEN 1 AND 500),
    transcript  text        NOT NULL CHECK (char_length(transcript) BETWEEN 1 AND 500),
    condition   text        CHECK (condition IS NULL OR condition IN
        ('headset', 'laptop-mic', 'phone-speaker-distance', 'noisy')),
    -- 'authored' — a line written first and read later, like every vendored
    -- one. 'adhoc'  — audio recorded first, its gold text written down
    -- afterwards. The distinction travels into the corpus metadata because a
    -- reader of the corpus must be able to tell a read script from a
    -- transcribed improvisation.
    source      text        NOT NULL CHECK (source IN ('authored', 'adhoc')),
    created_by  uuid        NOT NULL,   -- users.sub, soft ref (0089 pattern)
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT eval_item_one_per_script_id UNIQUE (tenant_id, script_id)
);

CREATE INDEX corpus_eval_script_items_tenant_idx
    ON corpus_eval_script_items (tenant_id, created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON corpus_eval_script_items TO app_role;
ALTER TABLE corpus_eval_script_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_script_items FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_script_items_tenant_all ON corpus_eval_script_items
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── published snapshots ───────────────────────────────────────────────
CREATE TABLE corpus_eval_snapshots (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    version         integer     NOT NULL CHECK (version > 0),
    utterance_count integer     NOT NULL CHECK (utterance_count > 0),
    total_duration_ms bigint    NOT NULL CHECK (total_duration_ms >= 0),
    -- The eval/corpus/v1 manifest fragment for exactly these utterances,
    -- with a SHA-256 per file. Stored so the export can be rebuilt byte-for-
    -- byte later and checked against build_corpus_manifest.py.
    manifest        jsonb       NOT NULL,
    manifest_sha256 text        NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    published_by    uuid        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT eval_snapshot_version_unique UNIQUE (tenant_id, version)
);

CREATE INDEX corpus_eval_snapshots_tenant_idx
    ON corpus_eval_snapshots (tenant_id, version DESC);

-- Deliberately no UPDATE and no DELETE: a published snapshot is the thing a
-- WER number refers to. Making it editable would make every stored score
-- unfalsifiable. Superseding one means publishing the next version.
GRANT SELECT, INSERT ON corpus_eval_snapshots TO app_role;
ALTER TABLE corpus_eval_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_snapshots_tenant_all ON corpus_eval_snapshots
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE TABLE corpus_eval_snapshot_items (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    snapshot_id  uuid        NOT NULL REFERENCES corpus_eval_snapshots(id) ON DELETE CASCADE,
    script_id    text        NOT NULL,
    language     text        NOT NULL,
    specialty    text        NOT NULL,
    subset       text,
    -- The gold text as it stood at publish time. The take carries its own
    -- snapshot of the same field (0089); this one is what the score was
    -- computed against, which is not automatically the same row later.
    transcript   text        NOT NULL,
    condition    text        NOT NULL,
    duration_ms  integer     NOT NULL,
    audio_sha256 text        NOT NULL CHECK (audio_sha256 ~ '^[0-9a-f]{64}$'),
    -- Soft reference: the take may be re-recorded or removed afterwards, and
    -- the snapshot must survive that. audio_sha256 is what detects it.
    take_id      uuid        NOT NULL,
    source       text        NOT NULL,
    CONSTRAINT eval_snapshot_item_unique UNIQUE (snapshot_id, script_id)
);

CREATE INDEX corpus_eval_snapshot_items_snapshot_idx
    ON corpus_eval_snapshot_items (snapshot_id, script_id);

GRANT SELECT, INSERT ON corpus_eval_snapshot_items TO app_role;
ALTER TABLE corpus_eval_snapshot_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_snapshot_items FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_snapshot_items_tenant_all ON corpus_eval_snapshot_items
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── scoring runs ──────────────────────────────────────────────────────
CREATE TABLE corpus_eval_runs (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    snapshot_id uuid        NOT NULL REFERENCES corpus_eval_snapshots(id) ON DELETE CASCADE,
    status      text        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'failed')),
    -- The ASR engine asr-service reported for these jobs. NOT NULL on
    -- purpose: a WER without its model is not a measurement.
    model       text        NOT NULL,
    started_by  uuid        NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    -- Aggregates, written once at completion: overall WER/CER weighted by
    -- reference words, plus the per-subset and per-language breakdowns the
    -- adversarial subsets exist to produce.
    summary     jsonb
);

CREATE INDEX corpus_eval_runs_tenant_idx ON corpus_eval_runs (tenant_id, started_at DESC);

GRANT SELECT, INSERT, UPDATE ON corpus_eval_runs TO app_role;
ALTER TABLE corpus_eval_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_runs_tenant_all ON corpus_eval_runs
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE TABLE corpus_eval_run_items (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id      uuid        NOT NULL REFERENCES corpus_eval_runs(id) ON DELETE CASCADE,
    script_id   text        NOT NULL,
    status      text        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'transcribing', 'scored', 'failed', 'skipped')),
    asr_job_id  uuid,
    -- The ASR output. Kept because a WER of 0.4 with no hypothesis is a
    -- number nobody can act on — the point of the corpus is to see WHAT the
    -- model got wrong.
    hypothesis  text,
    wer         double precision CHECK (wer IS NULL OR wer >= 0),
    cer         double precision CHECK (cer IS NULL OR cer >= 0),
    ref_words   integer     CHECK (ref_words IS NULL OR ref_words >= 0),
    -- Why this utterance has no score: an asr-service error kind, or
    -- 'language_unsupported' for the de lines batch ASR does not take.
    error       text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT eval_run_item_unique UNIQUE (run_id, script_id)
);

CREATE INDEX corpus_eval_run_items_run_idx ON corpus_eval_run_items (run_id, status);

GRANT SELECT, INSERT, UPDATE ON corpus_eval_run_items TO app_role;
ALTER TABLE corpus_eval_run_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_run_items FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_run_items_tenant_all ON corpus_eval_run_items
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

COMMIT;
