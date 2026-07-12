-- Reverse 0036: drop the partition-creation function and the two
-- partitions it backfilled. NOTE: dropping the partitions discards any
-- telemetry rows collected in July/August 2026.

DROP FUNCTION IF EXISTS autocomplete_create_telemetry_partition(date, date);
DROP TABLE IF EXISTS autocomplete_telemetry_2026_07;
DROP TABLE IF EXISTS autocomplete_telemetry_2026_08;
