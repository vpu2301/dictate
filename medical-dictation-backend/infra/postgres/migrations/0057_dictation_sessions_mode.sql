-- 0057: conversation mode (sprint 14, ADR-0034).
--
-- dictation_sessions.mode distinguishes single-voice dictation from
-- two-voice ambient-scribe conversation sessions. Orthogonal to
-- target_kind (what artefact the session produces): a conversation can
-- still target a clinical_note. Conversation sessions additionally
-- REQUIRE an encounter_id + a granted 'recording' consent for the
-- encounter's patient — enforced at start_session in dictation-service
-- (the consent predicate reads patient_consents under RLS); the CHECK
-- here backstops the invariant that a conversation row always carries
-- an encounter.

ALTER TABLE dictation_sessions
    ADD COLUMN mode TEXT NOT NULL DEFAULT 'dictation'
        CHECK (mode IN ('dictation', 'conversation'));

ALTER TABLE dictation_sessions
    ADD CONSTRAINT dictation_sessions_conversation_has_encounter
        CHECK (mode <> 'conversation' OR encounter_id IS NOT NULL);

-- Ops queries slice live sessions by mode (mode-aware capacity panels).
CREATE INDEX dictation_sessions_mode_idx
    ON dictation_sessions (tenant_id, mode, created_at DESC);
