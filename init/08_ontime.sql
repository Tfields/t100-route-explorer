-- 08_ontime.sql
-- Marketing-carrier attribution for regional airlines.
--
-- T-100 is filed by the OPERATING carrier, so a SkyWest flight sold as United
-- Express sits under SkyWest (OO) with nothing saying it flew for United.
-- BTS "Marketing Carrier On-Time Performance" (TranStats Table_ID 237,
-- January 2018 onward, domestic) is flight-level and carries both the
-- marketing and the operating airline. We use it to split each multi-partner
-- regional's T-100 traffic between the majors it flew for.
--
-- bronze.ontime_marketing is the ONE deliberate exception to "bronze is raw":
-- the source is ~665K flights x 119 columns per month (~330 MB CSV), and
-- T-100 is monthly, so ingest/load_ontime.py aggregates each month to
-- marketing x operating x origin x dest and discards the flights. Columns
-- stay TEXT like the rest of bronze; typing happens in silver.

CREATE TABLE IF NOT EXISTS bronze.ontime_marketing (
    year                TEXT,
    month               TEXT,
    mkt_unique_carrier  TEXT,   -- Marketing_Airline_Network: who sold the flight
    op_unique_carrier   TEXT,   -- Operating_Airline: same codes as T-100 carrier
    origin              TEXT,
    dest                TEXT,
    flights             TEXT,   -- scheduled flights (rows in the source)
    cancelled           TEXT,   -- of which cancelled (performed = flights - cancelled)
    diverted            TEXT,
    _batch_id           BIGINT REFERENCES meta.ingest_batch(batch_id),
    _source_file        TEXT,
    _loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ontime_marketing_period
    ON bronze.ontime_marketing (year, month);

-- ===========================================================================
-- silver.ontime_partner: typed. One row per month x operating carrier x
-- marketing carrier x origin x dest with PERFORMED departures.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS silver.ontime_partner AS
SELECT make_date(year::int, month::int, 1)      AS flight_month,
       op_unique_carrier                         AS op,
       mkt_unique_carrier                        AS mkt,
       origin, dest,
       SUM(flights::int - cancelled::int)::int   AS performed
FROM bronze.ontime_marketing
GROUP BY 1, 2, 3, 4, 5
HAVING SUM(flights::int - cancelled::int) > 0;

CREATE INDEX IF NOT EXISTS ix_ontime_partner_route
    ON silver.ontime_partner (op, origin, dest, flight_month);

-- ===========================================================================
-- silver.carrier_partner_alloc: for every T-100 route-month flown by a
-- multi-partner regional (2018+), the share of it attributed to each family.
--
-- Which carriers: operating carriers that the on-time data shows flying for
-- a marketing carrier other than themselves AND that are not already in the
-- authored map (those keep their static family).
--
-- Share = that family's performed departures / all performed departures, at
-- the most specific level the on-time data covers (match_level):
--   1 same route, same direction      4 the destination airport
--   2 same route, reverse direction   5 the carrier's whole month
--   3 the origin airport
-- Levels 2-5 catch T-100 rows the on-time data lacks (it is domestic only,
-- so e.g. a regional's flights to Canada take the US hub's partner mix).
--
-- estimated = TRUE when the route-month is split between more than one
-- family: applying a departure share to seats and passengers then assumes
-- each partner's flights had the same aircraft size and load factor. A
-- route-month with a single family is attributed whole (not an estimate),
-- even at a fallback level. Shares sum to 1 per route-month.
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS silver.carrier_partner_alloc AS
WITH split AS (
    SELECT DISTINCT op FROM silver.ontime_partner
    WHERE op <> mkt
      AND op NOT IN (SELECT carrier FROM silver.dim_carrier_family)
),
names AS (
    SELECT carrier, MAX(carrier_name) AS carrier_name
    FROM silver.fact_segment WHERE flight_month >= DATE '2018-01-01'
    GROUP BY carrier
),
r AS (
    SELECT DISTINCT flight_month, carrier, origin, dest
    FROM silver.fact_segment
    WHERE service_class = 'F' AND flight_month >= DATE '2018-01-01'
      AND carrier IN (SELECT op FROM split)
),
s_route AS (
    SELECT flight_month, op, origin, dest, mkt, SUM(performed) AS n
    FROM silver.ontime_partner GROUP BY 1, 2, 3, 4, 5
),
s_apt AS (
    SELECT flight_month, op, origin AS airport, mkt, SUM(performed) AS n
    FROM silver.ontime_partner GROUP BY 1, 2, 3, 4
),
s_car AS (
    SELECT flight_month, op, mkt, SUM(performed) AS n
    FROM silver.ontime_partner GROUP BY 1, 2, 3
),
k_route AS (SELECT DISTINCT flight_month, op, origin, dest FROM s_route),
k_apt   AS (SELECT DISTINCT flight_month, op, airport FROM s_apt),
k_car   AS (SELECT DISTINCT flight_month, op FROM s_car),
lv AS (
    SELECT r.*,
           CASE WHEN k1.op IS NOT NULL THEN 1
                WHEN k2.op IS NOT NULL THEN 2
                WHEN k3.op IS NOT NULL THEN 3
                WHEN k4.op IS NOT NULL THEN 4
                WHEN k5.op IS NOT NULL THEN 5 END AS match_level
    FROM r
    LEFT JOIN k_route k1 ON k1.flight_month = r.flight_month AND k1.op = r.carrier
                        AND k1.origin = r.origin AND k1.dest = r.dest
    LEFT JOIN k_route k2 ON k2.flight_month = r.flight_month AND k2.op = r.carrier
                        AND k2.origin = r.dest AND k2.dest = r.origin
    LEFT JOIN k_apt   k3 ON k3.flight_month = r.flight_month AND k3.op = r.carrier
                        AND k3.airport = r.origin
    LEFT JOIN k_apt   k4 ON k4.flight_month = r.flight_month AND k4.op = r.carrier
                        AND k4.airport = r.dest
    LEFT JOIN k_car   k5 ON k5.flight_month = r.flight_month AND k5.op = r.carrier
),
raw AS (
    SELECT lv.*, s.mkt, s.n FROM lv JOIN s_route s
        ON s.flight_month = lv.flight_month AND s.op = lv.carrier
       AND s.origin = lv.origin AND s.dest = lv.dest
    WHERE lv.match_level = 1
    UNION ALL
    SELECT lv.*, s.mkt, s.n FROM lv JOIN s_route s
        ON s.flight_month = lv.flight_month AND s.op = lv.carrier
       AND s.origin = lv.dest AND s.dest = lv.origin
    WHERE lv.match_level = 2
    UNION ALL
    SELECT lv.*, s.mkt, s.n FROM lv JOIN s_apt s
        ON s.flight_month = lv.flight_month AND s.op = lv.carrier
       AND s.airport = lv.origin
    WHERE lv.match_level = 3
    UNION ALL
    SELECT lv.*, s.mkt, s.n FROM lv JOIN s_apt s
        ON s.flight_month = lv.flight_month AND s.op = lv.carrier
       AND s.airport = lv.dest
    WHERE lv.match_level = 4
    UNION ALL
    SELECT lv.*, s.mkt, s.n FROM lv JOIN s_car s
        ON s.flight_month = lv.flight_month AND s.op = lv.carrier
    WHERE lv.match_level = 5
),
fam AS (
    SELECT raw.flight_month, raw.carrier, raw.origin, raw.dest, raw.match_level,
           COALESCE(f.family, raw.mkt)                               AS family,
           MAX(COALESCE(f.family_name, nm.carrier_name, raw.mkt))    AS family_name,
           SUM(raw.n)                                                AS n
    FROM raw
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = raw.mkt
    LEFT JOIN names nm ON nm.carrier = raw.mkt
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT flight_month, carrier, origin, dest, family, family_name,
       (n / SUM(n) OVER w)::numeric(9, 6)                  AS share,
       (COUNT(*) OVER w > 1)                               AS estimated,
       match_level
FROM fam
WINDOW w AS (PARTITION BY flight_month, carrier, origin, dest);

CREATE UNIQUE INDEX IF NOT EXISTS ux_carrier_partner_alloc
    ON silver.carrier_partner_alloc (carrier, origin, dest, flight_month, family);
CREATE INDEX IF NOT EXISTS ix_carrier_partner_alloc_month
    ON silver.carrier_partner_alloc (flight_month, carrier, origin, dest);

-- NOTE for databases built before this file existed: the two family views
-- below used to be defined in 07 without the allocation. IF NOT EXISTS won't
-- redefine them, so drop them once before running this file:
--   DROP MATERIALIZED VIEW gold.family_month, gold.airport_family_month;

-- ===========================================================================
-- family_month: gold.carrier_month, but keyed by family - continuous lines
-- across mergers. Family = the marketing-carrier allocation where one exists
-- (multi-partner regionals, 2018+), else the authored map, else the code
-- itself. Allocated rows are weighted by their share, so totals still add up
-- to the carrier views (to within rounding). seats_estimated = the part of
-- seats that came from an ESTIMATED split (see carrier_partner_alloc).
-- ===========================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.family_month AS
SELECT
    s.flight_month,
    COALESCE(a.family, f.family, s.carrier)             AS family,
    MAX(COALESCE(a.family_name, f.family_name, s.carrier_name)) AS family_name,
    ROUND(SUM(s.departures_performed * COALESCE(a.share, 1)))::bigint AS departures,
    ROUND(SUM(s.seats * COALESCE(a.share, 1)))::bigint  AS seats,
    ROUND(SUM(s.passengers * COALESCE(a.share, 1)))::bigint AS passengers,
    ROUND(100.0 * SUM(s.passengers * COALESCE(a.share, 1))
          / NULLIF(SUM(s.seats * COALESCE(a.share, 1)), 0), 1) AS load_factor_pct,
    COUNT(DISTINCT s.origin)                            AS airports_served,
    COUNT(DISTINCT s.origin || '>' || s.dest)           AS routes_served,
    COALESCE(ROUND(SUM(s.seats * a.share) FILTER (WHERE a.estimated)), 0)::bigint
                                                        AS seats_estimated
FROM silver.fact_segment s
LEFT JOIN silver.carrier_partner_alloc a
       ON a.flight_month = s.flight_month AND a.carrier = s.carrier
      AND a.origin = s.origin AND a.dest = s.dest
LEFT JOIN silver.dim_carrier_family f ON f.carrier = s.carrier
WHERE s.service_class = 'F'
GROUP BY s.flight_month, COALESCE(a.family, f.family, s.carrier);

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
        COALESCE(a.family, f.family, s.carrier)         AS family,
        MAX(COALESCE(a.family_name, f.family_name, s.carrier_name)) AS family_name,
        MAX(s.origin_city)                              AS city,
        MAX(s.origin_state)                             AS state,
        MAX(s.origin_country)                           AS country,
        MAX(s.origin_lat)                               AS latitude,
        MAX(s.origin_lon)                               AS longitude,
        ROUND(SUM(s.departures_performed * COALESCE(a.share, 1)))::bigint AS departures,
        ROUND(SUM(s.seats * COALESCE(a.share, 1)))::bigint AS seats,
        ROUND(SUM(s.passengers * COALESCE(a.share, 1)))::bigint AS passengers,
        COUNT(DISTINCT s.dest)                          AS nonstop_dests,
        COALESCE(ROUND(SUM(s.seats * a.share) FILTER (WHERE a.estimated)), 0)::bigint
                                                        AS seats_estimated
    FROM silver.fact_segment s
    LEFT JOIN silver.carrier_partner_alloc a
           ON a.flight_month = s.flight_month AND a.carrier = s.carrier
          AND a.origin = s.origin AND a.dest = s.dest
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = s.carrier
    WHERE s.service_class = 'F'
    GROUP BY s.flight_month, s.origin, COALESCE(a.family, f.family, s.carrier)
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
-- point, now covering the on-time allocation and the family views too.
-- ===========================================================================
CREATE OR REPLACE FUNCTION gold.refresh_all() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    -- ANALYZE after each upstream refresh: a refreshed matview keeps its old
    -- planner stats until autovacuum catches up, and with stale (small) row
    -- estimates the allocation joins went nested-loop -- 4.5 h instead of
    -- minutes after the 104-month on-time backfill.
    PERFORM silver.refresh_all();
    ANALYZE silver.fact_segment;
    REFRESH MATERIALIZED VIEW silver.ontime_partner;
    ANALYZE silver.ontime_partner;
    REFRESH MATERIALIZED VIEW silver.carrier_partner_alloc;
    ANALYZE silver.carrier_partner_alloc;
    REFRESH MATERIALIZED VIEW gold.carrier_month;
    REFRESH MATERIALIZED VIEW gold.airport_carrier_month;
    REFRESH MATERIALIZED VIEW gold.route_carrier_month;
    REFRESH MATERIALIZED VIEW gold.family_month;
    REFRESH MATERIALIZED VIEW gold.airport_family_month;
END $$;
