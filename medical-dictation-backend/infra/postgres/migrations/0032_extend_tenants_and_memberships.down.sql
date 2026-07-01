-- Reverse of 0032_extend_tenants_and_memberships.sql.

DROP TABLE IF EXISTS tenant_memberships;

DROP INDEX IF EXISTS tenants_slug_unique;

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_logo_size_chk;

ALTER TABLE tenants
    DROP COLUMN IF EXISTS legal_name,
    DROP COLUMN IF EXISTS slug,
    DROP COLUMN IF EXISTS logo_url,
    DROP COLUMN IF EXISTS logo_bytes,
    DROP COLUMN IF EXISTS logo_content_type,
    DROP COLUMN IF EXISTS contact_email,
    DROP COLUMN IF EXISTS phone_number,
    DROP COLUMN IF EXISTS website,
    DROP COLUMN IF EXISTS address_line1,
    DROP COLUMN IF EXISTS address_line2,
    DROP COLUMN IF EXISTS postal_code,
    DROP COLUMN IF EXISTS city,
    DROP COLUMN IF EXISTS state_or_region,
    DROP COLUMN IF EXISTS country,
    DROP COLUMN IF EXISTS tax_id,
    DROP COLUMN IF EXISTS registration_number,
    DROP COLUMN IF EXISTS is_active;
