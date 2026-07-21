-- 01_schemas.sql
-- Medallion layers live as schemas inside one database, not separate databases.
-- That keeps cross-layer SQL (bronze -> silver -> gold) in a single connection,
-- lets you transact across layers, and still gives clean namespacing + per-layer grants.

CREATE SCHEMA IF NOT EXISTS bronze;   -- raw, as-loaded, all text
CREATE SCHEMA IF NOT EXISTS silver;   -- typed, cleaned, conformed (later step)
CREATE SCHEMA IF NOT EXISTS gold;     -- business-ready aggregates (later step)
CREATE SCHEMA IF NOT EXISTS meta;     -- ingestion bookkeeping / load tracking

-- ---------------------------------------------------------------------------
-- Roles (optional but recommended). Group roles you can GRANT to login users.
--   t100_etl : read/write everywhere  -> used by the ingestion + transform jobs
--   t100_ro  : read-only              -> used by analysts / BI on gold (and below)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't100_etl') THEN
    CREATE ROLE t100_etl NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't100_ro') THEN
    CREATE ROLE t100_ro NOLOGIN;
  END IF;
END$$;

-- Usage on schemas
GRANT USAGE ON SCHEMA bronze, silver, gold, meta TO t100_etl, t100_ro;
GRANT CREATE ON SCHEMA bronze, silver, gold, meta TO t100_etl;

-- ETL: full read/write on existing + future tables
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA bronze, silver, gold, meta TO t100_etl;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA bronze, silver, gold, meta TO t100_etl;

-- Read-only: SELECT on existing + future tables
GRANT SELECT ON ALL TABLES IN SCHEMA bronze, silver, gold, meta TO t100_ro;

-- Make the grants apply to objects created later, too (owner = current user t100)
ALTER DEFAULT PRIVILEGES IN SCHEMA bronze, silver, gold, meta
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO t100_etl;
ALTER DEFAULT PRIVILEGES IN SCHEMA bronze, silver, gold, meta
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO t100_etl;
ALTER DEFAULT PRIVILEGES IN SCHEMA bronze, silver, gold, meta
  GRANT SELECT ON TABLES TO t100_ro;
