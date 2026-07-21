-- 04_geo.sql
-- Geographic reference data. Same bronze rules as 03_bronze.sql: every source
-- column TEXT, no casting or validation here — the '\N' null sentinels and any
-- encoding quirks land as-is and get cleaned in SILVER, where this becomes a
-- conformed dim_airport joined to the T-100 facts (route maps, distances).

-- OpenFlights airports.dat: fixed 14-column CSV with NO header row. Column
-- names and order below follow the OpenFlights documentation; the loader keeps
-- them in a SOURCES entry instead of reading a header.
CREATE TABLE IF NOT EXISTS bronze.airports (
    airport_id   TEXT,   -- OpenFlights internal id (unique per row)
    name         TEXT,
    city         TEXT,
    country      TEXT,
    iata         TEXT,   -- 3-letter code; joins to t100 origin/dest ('\N' when none)
    icao         TEXT,
    latitude     TEXT,   -- decimal degrees
    longitude    TEXT,
    altitude_ft  TEXT,
    utc_offset   TEXT,   -- hours from UTC
    dst          TEXT,   -- daylight-saving rule (E/A/S/O/Z/N/U)
    tz_database  TEXT,   -- tz database name, e.g. America/Chicago
    type         TEXT,   -- airport/station/port/unknown
    source       TEXT,
    -- metadata (no _data_scope: reference data has no domestic/international split)
    _batch_id    BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file TEXT,
    _loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
