-- 06_gold.sql
-- GOLD: business-ready aggregates over silver, sized for direct charting.
--
-- Conventions (see CLAUDE.md for rationale):
--   * Scheduled passenger service only (service_class = 'F'): 99.3% of all
--     passengers. Charters (L) and cargo (G/P) are excluded so "utilization"
--     means passenger operations.
--   * Departure-side convention: an airport's activity = flights departing
--     it. Arrivals are symmetric at monthly grain; counting both directions
--     would double-count.
--   * Domestic + international are combined (a hub's international bank
--     counts toward its utilization).
--   * Carrier CODES break at mergers (NW->DL 2010, CO->UA 2012, US->AA 2015):
--     step changes in per-carrier trends are real corporate events.
--
-- Refresh: SELECT gold.refresh_all();  -- refreshes silver first, then gold.
-- Changing a definition needs DROP MATERIALIZED VIEW + re-run of this file.

-- ===========================================================================
-- carrier_month: one row per carrier per month - system totals.
-- Use to define "major airlines" (top-N by seats) and as trend denominators.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.carrier_month AS
SELECT
    flight_month,
    carrier,
    MAX(carrier_name)                                   AS carrier_name,
    SUM(departures_performed)::bigint                   AS departures,
    SUM(seats)::bigint                                  AS seats,
    SUM(passengers)::bigint                             AS passengers,
    ROUND(100.0 * SUM(passengers) / NULLIF(SUM(seats), 0), 1) AS load_factor_pct,
    COUNT(DISTINCT origin)                              AS airports_served,
    COUNT(DISTINCT origin || '>' || dest)               AS routes_served
FROM silver.fact_segment
WHERE service_class = 'F'
GROUP BY flight_month, carrier;

CREATE INDEX IF NOT EXISTS ix_carrier_month_carrier ON gold.carrier_month (carrier, flight_month);
CREATE INDEX IF NOT EXISTS ix_carrier_month_month   ON gold.carrier_month (flight_month);

-- ===========================================================================
-- airport_carrier_month: the hub-utilization workhorse.
-- Grain: airport x carrier x month, with the two hub share metrics:
--   pct_of_carrier_system - this airport's share of the carrier's monthly
--                           seats ("how much of a hub is this FOR the airline")
--   pct_of_airport_seats  - the carrier's share of the airport's monthly
--                           seats ("how dominant is the airline AT the airport")
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.airport_carrier_month AS
WITH base AS (
    SELECT
        flight_month,
        origin                                          AS airport,
        carrier,
        MAX(carrier_name)                               AS carrier_name,
        MAX(origin_city)                                AS city,
        MAX(origin_state)                               AS state,
        MAX(origin_country)                             AS country,
        MAX(origin_lat)                                 AS latitude,
        MAX(origin_lon)                                 AS longitude,
        SUM(departures_performed)::bigint               AS departures,
        SUM(seats)::bigint                              AS seats,
        SUM(passengers)::bigint                         AS passengers,
        COUNT(DISTINCT dest)                            AS nonstop_dests
    FROM silver.fact_segment
    WHERE service_class = 'F'
    GROUP BY flight_month, origin, carrier
)
SELECT
    base.*,
    ROUND(100.0 * passengers / NULLIF(seats, 0), 1)     AS load_factor_pct,
    ROUND(100.0 * seats / NULLIF(SUM(seats) OVER (PARTITION BY flight_month, carrier), 0), 2)
                                                        AS pct_of_carrier_system,
    ROUND(100.0 * seats / NULLIF(SUM(seats) OVER (PARTITION BY flight_month, airport), 0), 2)
                                                        AS pct_of_airport_seats
FROM base;

CREATE INDEX IF NOT EXISTS ix_acm_airport ON gold.airport_carrier_month (airport, flight_month);
CREATE INDEX IF NOT EXISTS ix_acm_carrier ON gold.airport_carrier_month (carrier, flight_month);
CREATE INDEX IF NOT EXISTS ix_acm_month   ON gold.airport_carrier_month (flight_month);

-- ===========================================================================
-- route_carrier_month: carrier x route x month with both endpoints' geo -
-- feeds hub spoke maps (lines out of a hub, sized by seats) with no joins.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.route_carrier_month AS
SELECT
    flight_month,
    carrier,
    MAX(carrier_name)                                   AS carrier_name,
    origin,
    MAX(origin_city)                                    AS origin_city,
    MAX(origin_country)                                 AS origin_country,
    MAX(origin_lat)                                     AS origin_lat,
    MAX(origin_lon)                                     AS origin_lon,
    dest,
    MAX(dest_city)                                      AS dest_city,
    MAX(dest_country)                                   AS dest_country,
    MAX(dest_lat)                                       AS dest_lat,
    MAX(dest_lon)                                       AS dest_lon,
    MAX(distance_miles)                                 AS distance_miles,
    SUM(departures_performed)::bigint                   AS departures,
    SUM(seats)::bigint                                  AS seats,
    SUM(passengers)::bigint                             AS passengers,
    ROUND(100.0 * SUM(passengers) / NULLIF(SUM(seats), 0), 1) AS load_factor_pct
FROM silver.fact_segment
WHERE service_class = 'F'
GROUP BY flight_month, carrier, origin, dest;

CREATE INDEX IF NOT EXISTS ix_rcm_carrier ON gold.route_carrier_month (carrier, flight_month);
CREATE INDEX IF NOT EXISTS ix_rcm_route   ON gold.route_carrier_month (origin, dest);
CREATE INDEX IF NOT EXISTS ix_rcm_month   ON gold.route_carrier_month (flight_month);

-- ===========================================================================
-- One call to rebuild everything derived, in dependency order.
-- ===========================================================================
CREATE OR REPLACE FUNCTION gold.refresh_all() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM silver.refresh_all();
    REFRESH MATERIALIZED VIEW gold.carrier_month;
    REFRESH MATERIALIZED VIEW gold.airport_carrier_month;
    REFRESH MATERIALIZED VIEW gold.route_carrier_month;
END $$;
