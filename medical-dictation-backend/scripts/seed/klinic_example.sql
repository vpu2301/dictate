-- Standalone: create the example clinic "klinic" and link the current login
-- account (Dev Admin A, admin@tenant-a.example) to it as owner.
--
-- Apply against a running dev DB WITHOUT a full reseed:
--   psql "postgresql://postgres:postgres@localhost:5432/medical_dictation" \
--        -f scripts/seed/klinic_example.sql
--
-- Idempotent — safe to run repeatedly. Requires migration 0032 to be applied
-- (make migrate-up).

BEGIN;

INSERT INTO tenants (
    id, name, display_name, legal_name, slug, locale, timezone, status, is_active,
    contact_email, phone_number, website,
    address_line1, city, country
) VALUES (
    '0000c111-0000-0000-0000-000000000001',
    'klinic', 'Klinic', 'Klinic Medical Center LLC', 'klinic', 'uk', 'Europe/Kyiv', 'active', true,
    'hello@klinic.example', '+380 44 111 1111', 'https://klinic.example',
    '5 Sichovykh Striltsiv St', 'Kyiv', 'Ukraine'
)
ON CONFLICT (id) DO UPDATE SET
    display_name  = EXCLUDED.display_name,
    legal_name    = EXCLUDED.legal_name,
    slug          = EXCLUDED.slug,
    contact_email = EXCLUDED.contact_email,
    phone_number  = EXCLUDED.phone_number,
    website       = EXCLUDED.website,
    address_line1 = EXCLUDED.address_line1,
    city          = EXCLUDED.city,
    country       = EXCLUDED.country,
    is_active     = EXCLUDED.is_active;

-- Owner membership for the current login account (Dev Admin A).
INSERT INTO tenant_memberships (tenant_id, user_sub, role, status)
VALUES ('0000c111-0000-0000-0000-000000000001', '0a000000-0000-0000-0000-00000000000a', 'owner', 'active')
ON CONFLICT (tenant_id, user_sub) DO NOTHING;

COMMIT;

-- Show the result.
SELECT t.display_name, t.slug, m.role AS my_role
FROM tenant_memberships m
JOIN tenants t ON t.id = m.tenant_id
WHERE m.user_sub = '0a000000-0000-0000-0000-00000000000a'
ORDER BY t.display_name;
