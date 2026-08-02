-- Reverse 0060. The synthetic backfill dies with the columns; there is no
-- captured contact data to preserve on the way down.

ALTER TABLE patients
    DROP COLUMN IF EXISTS phone,
    DROP COLUMN IF EXISTS email,
    DROP COLUMN IF EXISTS address_street,
    DROP COLUMN IF EXISTS address_house,
    DROP COLUMN IF EXISTS address_zip,
    DROP COLUMN IF EXISTS address_city,
    DROP COLUMN IF EXISTS address_country;
