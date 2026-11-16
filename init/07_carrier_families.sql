-- 07_carrier_families.sql
-- Carrier families: the authored map that stitches merged carriers (and
-- single-partner regionals) into continuous histories.
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
-- Regional partners: T-100 is reported by OPERATING carrier, so a major's
-- regional feed (American Eagle, Delta Connection, ...) sits under the
-- regional's own code. Regionals that flew for ONE family through their whole
-- T-100 history are mapped here too (note starts 'regional', merged_month
-- NULL) so family seats/shares include that feed. Each was checked against
-- its hub footprint by era. Multi-partner regionals (SkyWest OO, Republic YX,
-- Mesa YV, ExpressJet/ASA EV, GoJet G7, Air Wisconsin ZW, Compass, Trans
-- States, Chautauqua, Shuttle America) are deliberately NOT mapped: T-100
-- cannot say which major a given flight was for. Note most regionals only
-- start reporting T-100 in Oct 2002, so family lines step up that month.
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
 ('HA', 'AS', 'Alaska Air Group family',   '2025-10-01', 'Hawaiian, acquired by Alaska Air Group Sep 2024; brand retained'),
 ('MQ',     'AA', 'American Airlines family', NULL, 'regional: Envoy Air (American Eagle), owned by American'),
 ('OH',     'AA', 'American Airlines family', NULL, 'regional: PSA Airlines, US Airways Express then American Eagle; owned by US Airways/American'),
 ('PT',     'AA', 'American Airlines family', NULL, 'regional: Piedmont Airlines, US Airways Express then American Eagle; owned by US Airways/American'),
 ('OW',     'AA', 'American Airlines family', NULL, 'regional: Executive Airlines (American Eagle, San Juan), owned by American; ended 2013'),
 ('9E',     'DL', 'Delta Air Lines family',   NULL, 'regional: Endeavor Air (ex-Pinnacle), Northwest Airlink then Delta Connection; owned by Delta since 2013'),
 ('OH (1)', 'DL', 'Delta Air Lines family',   NULL, 'regional: Comair, Delta Connection, owned by Delta; ended 2012'),
 ('XJ',     'DL', 'Delta Air Lines family',   NULL, 'regional: Mesaba, Northwest Airlink then Delta Connection; ended 2011'),
 ('C5',     'UA', 'United Airlines family',   NULL, 'regional: CommuteAir, Continental Connection then United Express'),
 ('XE (1)', 'UA', 'United Airlines family',   NULL, 'regional: ExpressJet (Continental Express, then United Express); a few % of 2007-08 flying was for Delta or own-brand; ended 2011'),
 ('QX',     'AS', 'Alaska Air Group family',  NULL, 'regional: Horizon Air, owned by Alaska Air Group; ~6% of 2004-07 flying was Frontier JetExpress at DEN');

-- The family-keyed gold views (gold.family_month, gold.airport_family_month)
-- and gold.refresh_all() live in 08_ontime.sql: they also depend on the
-- marketing-carrier allocation built there.
