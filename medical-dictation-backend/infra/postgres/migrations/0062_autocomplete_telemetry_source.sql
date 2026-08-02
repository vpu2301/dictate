-- Sprint 15: Layer C telemetry rides the sprint-10 pipeline (ADR-0036).
-- A `source` discriminator separates trie-suggestion telemetry
-- ('autocomplete', the historical default) from Layer C generative ghost
-- text ('layer_c'). ALTER on the partitioned parent propagates to every
-- partition; the constant DEFAULT is metadata-only (no rewrite).
--
-- layer_c rows carry NO phrase_id/snippet_id (completions are not corpus
-- rows) — the roll-up's phrase counters filter on source, and the
-- acceptance-rate gauge reads layer_c rows only.

ALTER TABLE autocomplete_telemetry
    ADD COLUMN source TEXT NOT NULL DEFAULT 'autocomplete';

ALTER TABLE autocomplete_telemetry
    ADD CONSTRAINT autocomplete_telemetry_source_chk
    CHECK (source IN ('autocomplete', 'layer_c'));
