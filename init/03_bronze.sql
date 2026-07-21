-- 03_bronze.sql
-- Bronze = faithful landing of the source CSVs. Rules:
--   * every source column is TEXT (no casting, no validation here)
--   * column names match the TranStats prezipped headers (lowercased; PG folds case)
--   * domestic + international share one table, separated by _data_scope.
--     Their column sets are NEARLY identical: domestic files carry the
--     *_state_* fields, international files carry the *_country* fields
--     (both sets exist here; each scope leaves the other's columns NULL)
--   * metadata columns are prefixed with _ so they never collide with source fields
--
-- Casting, null-cleaning, and lookup joins all happen in SILVER, not here.

-- ===========================================================================
-- T-100 SEGMENT  (nonstop leg grain: carrier x O&D x aircraft type x class)
-- Adds capacity/operations fields that markets don't have.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS bronze.t100_segment (
    departures_scheduled   TEXT,
    departures_performed   TEXT,
    payload                TEXT,
    seats                  TEXT,
    passengers             TEXT,
    freight                TEXT,
    mail                   TEXT,
    distance               TEXT,
    ramp_to_ramp           TEXT,
    air_time               TEXT,
    unique_carrier         TEXT,
    airline_id             TEXT,
    unique_carrier_name    TEXT,
    unique_carrier_entity  TEXT,
    region                 TEXT,
    carrier                TEXT,
    carrier_name           TEXT,
    carrier_group          TEXT,
    carrier_group_new      TEXT,
    origin_airport_id      TEXT,
    origin_airport_seq_id  TEXT,
    origin_city_market_id  TEXT,
    origin                 TEXT,
    origin_city_name       TEXT,
    origin_state_abr       TEXT,   -- domestic files only
    origin_state_fips      TEXT,
    origin_state_nm        TEXT,
    origin_country         TEXT,   -- international files only
    origin_country_name    TEXT,
    origin_wac             TEXT,
    dest_airport_id        TEXT,
    dest_airport_seq_id    TEXT,
    dest_city_market_id    TEXT,
    dest                   TEXT,
    dest_city_name         TEXT,
    dest_state_abr         TEXT,
    dest_state_fips        TEXT,
    dest_state_nm          TEXT,
    dest_country           TEXT,
    dest_country_name      TEXT,
    dest_wac               TEXT,
    aircraft_group         TEXT,
    aircraft_type          TEXT,
    aircraft_config        TEXT,
    year                   TEXT,
    quarter                TEXT,
    month                  TEXT,
    distance_group         TEXT,
    class                  TEXT,
    data_source            TEXT,
    -- metadata ---------------------------------------------------------------
    _batch_id              BIGINT REFERENCES meta.ingest_batch(batch_id),
    _data_scope            TEXT,
    _source_file           TEXT,
    _loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- T-100 MARKET  (on-flight O&D grain: carrier x O&D x class)
-- No departures/seats/aircraft/ramp_to_ramp/air_time — demand side only.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS bronze.t100_market (
    passengers             TEXT,
    freight                TEXT,
    mail                   TEXT,
    distance               TEXT,
    unique_carrier         TEXT,
    airline_id             TEXT,
    unique_carrier_name    TEXT,
    unique_carrier_entity  TEXT,
    region                 TEXT,
    carrier                TEXT,
    carrier_name           TEXT,
    carrier_group          TEXT,
    carrier_group_new      TEXT,
    origin_airport_id      TEXT,
    origin_airport_seq_id  TEXT,
    origin_city_market_id  TEXT,
    origin                 TEXT,
    origin_city_name       TEXT,
    origin_state_abr       TEXT,   -- domestic files only
    origin_state_fips      TEXT,
    origin_state_nm        TEXT,
    origin_country         TEXT,   -- international files only
    origin_country_name    TEXT,
    origin_wac             TEXT,
    dest_airport_id        TEXT,
    dest_airport_seq_id    TEXT,
    dest_city_market_id    TEXT,
    dest                   TEXT,
    dest_city_name         TEXT,
    dest_state_abr         TEXT,
    dest_state_fips        TEXT,
    dest_state_nm          TEXT,
    dest_country           TEXT,
    dest_country_name      TEXT,
    dest_wac               TEXT,
    year                   TEXT,
    quarter                TEXT,
    month                  TEXT,
    distance_group         TEXT,
    class                  TEXT,
    data_source            TEXT,
    -- metadata ---------------------------------------------------------------
    _batch_id              BIGINT REFERENCES meta.ingest_batch(batch_id),
    _data_scope            TEXT,
    _source_file           TEXT,
    _loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- LOOKUPS (BTS L_* decode tables). All share a (code, description) shape, so
-- one table per lookup keeps bronze faithful while staying easy to refresh.
-- These become conformed dimensions in silver.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS bronze.l_carrier (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_airport_id (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_city_market_id (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_aircraft_type (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_aircraft_config (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_aircraft_group (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_world_area_code (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_service_class (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_data_source (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_carrier_group_new (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bronze.l_distance_group_500 (
    code TEXT, description TEXT,
    _batch_id BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT, _loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- Amendments for databases created before the country columns were added
-- (no-ops on a fresh install, where the CREATEs above already include them).
-- ===========================================================================
ALTER TABLE bronze.t100_segment ADD COLUMN IF NOT EXISTS origin_country      TEXT;
ALTER TABLE bronze.t100_segment ADD COLUMN IF NOT EXISTS origin_country_name TEXT;
ALTER TABLE bronze.t100_segment ADD COLUMN IF NOT EXISTS dest_country        TEXT;
ALTER TABLE bronze.t100_segment ADD COLUMN IF NOT EXISTS dest_country_name   TEXT;
ALTER TABLE bronze.t100_market  ADD COLUMN IF NOT EXISTS origin_country      TEXT;
ALTER TABLE bronze.t100_market  ADD COLUMN IF NOT EXISTS origin_country_name TEXT;
ALTER TABLE bronze.t100_market  ADD COLUMN IF NOT EXISTS dest_country        TEXT;
ALTER TABLE bronze.t100_market  ADD COLUMN IF NOT EXISTS dest_country_name   TEXT;
