-- 07_carrier_families.sql
-- Carrier families: stitch merged carriers into continuous histories.
--
-- silver.dim_carrier_family is AUTHORED reference data (curated here, not
-- derived from a source download) mapping historical T-100 carrier codes to
-- the surviving family. Codes not listed are their own family — the gold
-- views COALESCE to the code itself, so coverage is total.
--
-- Semantics: a family trend BEFORE a merger is the pro-forma combined view
-- (sum of the then-independent companies). merged_month is documentation
-- (approximate single-certificate/last-flight date), not used in joins.
--
-- Re-running this file refreshes the seed (TRUNCATE + INSERT).

CREATE TABLE IF NOT EXISTS silver.dim_carrier_family (
    carrier      TEXT PRIMARY KEY,   -- T-100 unique_carrier code
    family       TEXT NOT NULL,      -- surviving code
    family_name  TEXT NOT NULL,
    merged_month DATE,               -- NULL for the survivor
    note         TEXT
);

TRUNCATE silver.dim_carrier_family;
INSERT INTO silver.dim_carrier_family (carrier, family, family_name, merged_month, note) VALUES
 ('AA', 'AA', 'American Airlines family',  NULL,         'survivor'),
 ('TW', 'AA', 'American Airlines family',  '2001-12-01', 'TWA, acquired by American 2001'),
 ('US', 'AA', 'American Airlines family',  '2015-10-01', 'US Airways, merged into American (last US flight Oct 2015)'),
 ('HP', 'AA', 'American Airlines family',  '2007-09-01', 'America West, merged into US Airways (single certificate 2007), then American'),
 ('DL', 'DL', 'Delta Air Lines family',    NULL,         'survivor'),
 ('NW', 'DL', 'Delta Air Lines family',    '2010-01-01', 'Northwest, merged into Delta (single certificate Dec 2009)'),
 ('UA', 'UA', 'United Airlines family',    NULL,         'survivor'),
 ('CO', 'UA', 'United Airlines family',    '2012-03-01', 'Continental, merged into United (single certificate Nov 2011)'),
 ('CS', 'UA', 'United Airlines family',    '2012-03-01', 'Continental Micronesia ("Air Mike"), folded into United'),
 ('WN', 'WN', 'Southwest Airlines family', NULL,         'survivor'),
 ('FL', 'WN', 'Southwest Airlines family', '2014-12-01', 'AirTran, acquired by Southwest (last FL flight Dec 2014)'),
 ('AS', 'AS', 'Alaska Air Group family',   NULL,         'survivor'),
 ('VX', 'AS', 'Alaska Air Group family',   '2018-04-01', 'Virgin America, merged into Alaska (single certificate Jan 2018)'),
 ('HA', 'AS', 'Alaska Air Group family',   '2025-10-01', 'Hawaiian, acquired by Alaska Air Group Sep 2024; brand retained');

-- ===========================================================================
-- family_month: gold.carrier_month, but keyed by family - continuous lines
-- across mergers. Unmapped codes pass through as their own family.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.family_month AS
SELECT
    s.flight_month,
    COALESCE(f.family, s.carrier)                       AS family,
    MAX(COALESCE(f.family_name, s.carrier_name))        AS family_name,
    SUM(s.departures_performed)::bigint                 AS departures,
    SUM(s.seats)::bigint                                AS seats,
    SUM(s.passengers)::bigint                           AS passengers,
    ROUND(100.0 * SUM(s.passengers) / NULLIF(SUM(s.seats), 0), 1) AS load_factor_pct,
    COUNT(DISTINCT s.origin)                            AS airports_served,
    COUNT(DISTINCT s.origin || '>' || s.dest)           AS routes_served
FROM silver.fact_segment s
LEFT JOIN silver.dim_carrier_family f ON f.carrier = s.carrier
WHERE s.service_class = 'F'
GROUP BY s.flight_month, COALESCE(f.family, s.carrier);

CREATE INDEX IF NOT EXISTS ix_family_month_family ON gold.family_month (family, flight_month);
CREATE INDEX IF NOT EXISTS ix_family_month_month  ON gold.family_month (flight_month);

-- ===========================================================================
-- airport_family_month: gold.airport_carrier_month, keyed by family, with
-- the share metrics recomputed against family denominators.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.airport_family_month AS
WITH base AS (
    SELECT
        s.flight_month,
        s.origin                                        AS airport,
        COALESCE(f.family, s.carrier)                   AS family,
        MAX(COALESCE(f.family_name, s.carrier_name))    AS family_name,
        MAX(s.origin_city)                              AS city,
        MAX(s.origin_state)                             AS state,
        MAX(s.origin_country)                           AS country,
        MAX(s.origin_lat)                               AS latitude,
        MAX(s.origin_lon)                               AS longitude,
        SUM(s.departures_performed)::bigint             AS departures,
        SUM(s.seats)::bigint                            AS seats,
        SUM(s.passengers)::bigint                       AS passengers,
        COUNT(DISTINCT s.dest)                          AS nonstop_dests
    FROM silver.fact_segment s
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = s.carrier
    WHERE s.service_class = 'F'
    GROUP BY s.flight_month, s.origin, COALESCE(f.family, s.carrier)
)
SELECT
    base.*,
    ROUND(100.0 * passengers / NULLIF(seats, 0), 1)     AS load_factor_pct,
    ROUND(100.0 * seats / NULLIF(SUM(seats) OVER (PARTITION BY flight_month, family), 0), 2)
                                                        AS pct_of_family_system,
    ROUND(100.0 * seats / NULLIF(SUM(seats) OVER (PARTITION BY flight_month, airport), 0), 2)
                                                        AS pct_of_airport_seats
FROM base;

CREATE INDEX IF NOT EXISTS ix_afm_airport ON gold.airport_family_month (airport, flight_month);
CREATE INDEX IF NOT EXISTS ix_afm_family  ON gold.airport_family_month (family, flight_month);
CREATE INDEX IF NOT EXISTS ix_afm_month   ON gold.airport_family_month (flight_month);

-- ===========================================================================
-- Supersedes the gold.refresh_all() defined in 06_gold.sql: same entry
-- point, now covering the family views too.
-- ===========================================================================
CREATE OR REPLACE FUNCTION gold.refresh_all() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM silver.refresh_all();
    REFRESH MATERIALIZED VIEW gold.carrier_month;
    REFRESH MATERIALIZED VIEW gold.airport_carrier_month;
    REFRESH MATERIALIZED VIEW gold.route_carrier_month;
    REFRESH MATERIALIZED VIEW gold.family_month;
    REFRESH MATERIALIZED VIEW gold.airport_family_month;
END $$;
