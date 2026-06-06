-- Down migration: remove the dev tenants. Will cascade-fail if any users
-- still reference them (intentional — production must not lose tenant rows).
DELETE FROM tenants
 WHERE id IN ('00000000-0000-0000-0000-00000000000a',
              '00000000-0000-0000-0000-00000000000b');
