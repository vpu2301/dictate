ALTER TABLE autocomplete_telemetry
    DROP CONSTRAINT IF EXISTS autocomplete_telemetry_source_chk;

ALTER TABLE autocomplete_telemetry
    DROP COLUMN IF EXISTS source;
