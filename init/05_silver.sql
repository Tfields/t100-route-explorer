-- 05_silver.sql
-- SILVER: typed, cleaned, analyst-friendly views over bronze.
--
-- Design decisions (see CLAUDE.md):
--   * Materialized views, not tables: the transform is pure SQL over bronze,
--     so "refresh" == re-run it. After any bronze load, call
--         SELECT silver.refresh_all();
--   * ISO month dates: year+month become flight_month DATE (first of month).
--     Bronze's TEXT year/month sort lexicographically ('9' > '12'); this fixes
--     that class of bug once, here, for everyone downstream.
--   * IDs are swapped for meaning: the *_airport_id/_seq_id/_city_market_id/
--     airline_id/fips/wac columns are dropped; codes that carry meaning
--     (carrier, origin/dest, aircraft type, service class) stay AND gain
--     their decoded names (from bronze.l_* lookups + inline BTS name fields).
--   * Geography is denormalized in (city/state/country, lat/lon via
--     OpenFlights) — LEFT JOINs, because OpenFlights covers >99.6% of
--     passengers but only ~half of T-100's distinct (mostly tiny) airports.
--   * Grain: bronze splits rows by carrier entity/region/config (reporting
--     artifacts that double-count grain-alikes); silver SUMs them away.
--     fact_segment: month x scope x carrier x origin x dest x aircraft type x class
--     fact_market:  month x scope x carrier x origin x dest x class
--   * Units are in the column names (miles, lb, minutes).
--
-- NOTE: CREATE ... IF NOT EXISTS does not update an existing definition.
-- If you change a view here: DROP MATERIALIZED VIEW silver.<name>; re-run file.

-- Safe numeric cast: '' and non-numeric text (BTS sentinels) become NULL.
CREATE OR REPLACE FUNCTION silver.to_num(t TEXT) RETURNS numeric
LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN t ~ '^-?[0-9]+\.?[0-9]*$' THEN t::numeric END $$;

-- ===========================================================================
-- dim_airport: one row per IATA code, conformed from OpenFlights.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS silver.dim_airport AS
SELECT DISTINCT ON (iata)
       iata,
       NULLIF(icao, '\N')                    AS icao,
       name                                  AS airport_name,
       city,
       country,
       silver.to_num(latitude)               AS latitude,
       silver.to_num(longitude)              AS longitude,
       silver.to_num(altitude_ft)::int       AS altitude_ft,
       NULLIF(tz_database, '\N')             AS tz_database
FROM bronze.airports
WHERE iata <> '\N' AND iata ~ '^[A-Z0-9]{3}$'
ORDER BY iata, silver.to_num(airport_id);   -- deterministic pick on rare dupes

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_airport_iata
    ON silver.dim_airport (iata);

-- ===========================================================================
-- fact_segment: nonstop leg months, typed + decoded + geolocated.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS silver.fact_segment AS
SELECT
    make_date(s.year::int, s.month::int, 1)          AS flight_month,
    s._data_scope                                    AS data_scope,
    s.unique_carrier                                 AS carrier,
    MAX(s.unique_carrier_name)                       AS carrier_name,
    s.origin,
    MAX(s.origin_city_name)                          AS origin_city,
    MAX(NULLIF(s.origin_state_abr, ''))              AS origin_state,
    MAX(COALESCE(NULLIF(s.origin_country_name, ''), ao.country)) AS origin_country,
    MAX(ao.latitude)                                 AS origin_lat,
    MAX(ao.longitude)                                AS origin_lon,
    s.dest,
    MAX(s.dest_city_name)                            AS dest_city,
    MAX(NULLIF(s.dest_state_abr, ''))                AS dest_state,
    MAX(COALESCE(NULLIF(s.dest_country_name, ''), ad.country))   AS dest_country,
    MAX(ad.latitude)                                 AS dest_lat,
    MAX(ad.longitude)                                AS dest_lon,
    s.aircraft_type,
    MAX(lat.description)                             AS aircraft_type_name,
    s.class                                          AS service_class,
    MAX(lsc.description)                             AS service_class_name,
    SUM(silver.to_num(s.departures_scheduled))::bigint AS departures_scheduled,
    SUM(silver.to_num(s.departures_performed))::bigint AS departures_performed,
    SUM(silver.to_num(s.seats))::bigint              AS seats,
    SUM(silver.to_num(s.passengers))::bigint         AS passengers,
    SUM(silver.to_num(s.freight))::bigint            AS freight_lb,
    SUM(silver.to_num(s.mail))::bigint               AS mail_lb,
    SUM(silver.to_num(s.payload))::bigint            AS payload_lb,
    MAX(silver.to_num(s.distance))                   AS distance_miles,
    SUM(silver.to_num(s.ramp_to_ramp))::bigint       AS ramp_to_ramp_min,
    SUM(silver.to_num(s.air_time))::bigint           AS air_time_min
FROM bronze.t100_segment s
LEFT JOIN silver.dim_airport       ao  ON ao.iata = s.origin
LEFT JOIN silver.dim_airport       ad  ON ad.iata = s.dest
LEFT JOIN bronze.l_aircraft_type   lat ON lat.code = s.aircraft_type
LEFT JOIN bronze.l_service_class   lsc ON lsc.code = s.class
WHERE s.year ~ '^[0-9]{4}$' AND s.month ~ '^[0-9]{1,2}$'
GROUP BY 1, 2, 3, s.origin, s.dest, s.aircraft_type, s.class;

CREATE INDEX IF NOT EXISTS ix_fact_segment_month   ON silver.fact_segment (flight_month);
CREATE INDEX IF NOT EXISTS ix_fact_segment_route   ON silver.fact_segment (origin, dest);
CREATE INDEX IF NOT EXISTS ix_fact_segment_carrier ON silver.fact_segment (carrier);

-- ===========================================================================
-- fact_market: on-flight O&D months (demand only - no capacity/aircraft).
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS silver.fact_market AS
SELECT
    make_date(m.year::int, m.month::int, 1)          AS flight_month,
    m._data_scope                                    AS data_scope,
    m.unique_carrier                                 AS carrier,
    MAX(m.unique_carrier_name)                       AS carrier_name,
    m.origin,
    MAX(m.origin_city_name)                          AS origin_city,
    MAX(NULLIF(m.origin_state_abr, ''))              AS origin_state,
    MAX(COALESCE(NULLIF(m.origin_country_name, ''), ao.country)) AS origin_country,
    MAX(ao.latitude)                                 AS origin_lat,
    MAX(ao.longitude)                                AS origin_lon,
    m.dest,
    MAX(m.dest_city_name)                            AS dest_city,
    MAX(NULLIF(m.dest_state_abr, ''))                AS dest_state,
    MAX(COALESCE(NULLIF(m.dest_country_name, ''), ad.country))   AS dest_country,
    MAX(ad.latitude)                                 AS dest_lat,
    MAX(ad.longitude)                                AS dest_lon,
    m.class                                          AS service_class,
    MAX(lsc.description)                             AS service_class_name,
    SUM(silver.to_num(m.passengers))::bigint         AS passengers,
    SUM(silver.to_num(m.freight))::bigint            AS freight_lb,
    SUM(silver.to_num(m.mail))::bigint               AS mail_lb,
    MAX(silver.to_num(m.distance))                   AS distance_miles
FROM bronze.t100_market m
LEFT JOIN silver.dim_airport     ao  ON ao.iata = m.origin
LEFT JOIN silver.dim_airport     ad  ON ad.iata = m.dest
LEFT JOIN bronze.l_service_class lsc ON lsc.code = m.class
WHERE m.year ~ '^[0-9]{4}$' AND m.month ~ '^[0-9]{1,2}$'
GROUP BY 1, 2, 3, m.origin, m.dest, m.class;

CREATE INDEX IF NOT EXISTS ix_fact_market_month   ON silver.fact_market (flight_month);
CREATE INDEX IF NOT EXISTS ix_fact_market_route   ON silver.fact_market (origin, dest);
CREATE INDEX IF NOT EXISTS ix_fact_market_carrier ON silver.fact_market (carrier);

-- ===========================================================================
-- One call to rebuild silver after bronze loads.
-- ===========================================================================
CREATE OR REPLACE FUNCTION silver.refresh_all() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW silver.dim_airport;
    REFRESH MATERIALIZED VIEW silver.fact_segment;
    REFRESH MATERIALIZED VIEW silver.fact_market;
END $$;
