-- Dev-only: make the signing-service's low-privilege roles loginable.
--
-- Migrations 0019/0020 create `app_public_verify` and `app_callback_writer`
-- WITHOUT LOGIN/PASSWORD on purpose — in production these are provisioned
-- with credentials from a secrets manager, never in SQL. For the local
-- docker-compose stack we give them a dev password so signing-service can
-- open its dedicated pools. Idempotent; safe to re-run.
ALTER ROLE app_public_verify  WITH LOGIN PASSWORD 'app_public_verify';
ALTER ROLE app_callback_writer WITH LOGIN PASSWORD 'app_callback_writer';
