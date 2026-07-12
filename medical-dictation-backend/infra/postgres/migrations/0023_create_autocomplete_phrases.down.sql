DROP TABLE IF EXISTS autocomplete_phrases;
-- NOTE: tenant_writer is NOT dropped here — it is a cluster-global role
-- bootstrapped in infra/postgres/init.sql (0023's up does not create it,
-- see the Sprint A1 note there); dropping it fails while any database in
-- the cluster still has grants for it.
DROP TYPE  IF EXISTS autocomplete_source;
