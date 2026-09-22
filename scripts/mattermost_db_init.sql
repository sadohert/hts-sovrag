-- Run once by the mattermost-db-init service against the shared Postgres (ADR 0011).
-- Idempotent: safe on a fresh volume and on an existing development volume.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mattermost') THEN
    CREATE ROLE mattermost LOGIN PASSWORD 'mattermost';
  END IF;
END
$$;

SELECT 'CREATE DATABASE mattermost OWNER mattermost'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mattermost') \gexec

-- Postgres grants CONNECT and TEMPORARY on every database to PUBLIC by default. Without this
-- the mattermost role could open the sovrag database. The isolation runs one way: sovrag is
-- the container's superuser and can still read the mattermost database (ADR 0011).
REVOKE CONNECT, TEMPORARY ON DATABASE sovrag FROM PUBLIC;
