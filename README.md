# T-100 Postgres warehouse — setup

Medallion-structured Postgres for BTS T-100 traffic data. This is **step 1: the
database + bronze landing zone**. Ingestion (loading CSVs) is the next step.

## Layout

One database (`t100`), medallion layers as **schemas**:

| schema   | purpose                                            | state now        |
|----------|----------------------------------------------------|------------------|
| `bronze` | raw, as-loaded, every column `TEXT`                | tables created   |
| `silver` | typed / cleaned / conformed + dimensions           | built (matviews) |
| `gold`   | business-ready aggregates                           | built (matviews) |
| `meta`   | `ingest_batch` — one row per load, for lineage      | table created    |

### Why schemas, not separate databases
Cross-layer SQL stays in one connection, you can transact bronze→silver, and you
still get clean namespacing and per-layer grants. Separate databases would force
FDW/dblink for every transform.

## Bronze design notes
- **One `t100_segment` and one `t100_market` table.** Domestic and international
  share identical column sets, so they go in the same table separated by
  `_data_scope` (`'domestic'` / `'international'`). Split later only if you want.
- **Segment vs market are different grains** and stay separate: segment carries
  capacity/ops fields (`seats`, `departures_*`, `payload`, `ramp_to_ramp`,
  `air_time`, aircraft type) that market doesn't have.
- **Everything is `TEXT`.** No casting or validation in bronze — that's silver's
  job. Column names match the TranStats prezipped headers (lowercased).
- **Metadata columns** are `_`-prefixed so they never collide with source fields:
  `_batch_id` (→ `meta.ingest_batch`), `_data_scope`, `_source_file`, `_loaded_at`.
- **Lookups** (`bronze.l_*`) land the BTS `L_*` decode tables as `(code, description)`;
  they become conformed dimensions in silver.

## Run it

### Option A — Docker (clean local instance)
```bash
docker compose up -d
# connects on localhost:5432, db=t100, user=t100, pass=t100
```
Init scripts in `init/` run automatically, in order, on first boot.

On macOS without Docker Desktop, Colima provides the engine (free, CLI-only,
no admin prompts after Homebrew itself):
```bash
brew install colima docker docker-compose
colima start          # boots a userspace Linux VM; docker CLI targets it
```

### Option B — existing Postgres
```bash
createdb t100
psql -d t100 -v ON_ERROR_STOP=1 -f init/01_schemas.sql
psql -d t100 -v ON_ERROR_STOP=1 -f init/02_meta.sql
psql -d t100 -v ON_ERROR_STOP=1 -f init/03_bronze.sql
```
All scripts are idempotent (`IF NOT EXISTS`), so re-running is safe.

## Files
```
docker-compose.yml
init/
  01_schemas.sql   schemas, roles (t100_etl / t100_ro), grants
  02_meta.sql      meta.ingest_batch load-tracking table
  03_bronze.sql    bronze.t100_segment, bronze.t100_market, bronze.l_* lookups
  04_geo.sql       bronze.airports (OpenFlights geographic reference)
  05_silver.sql    silver matviews: dim_airport, fact_segment, fact_market
  06_gold.sql      gold matviews: carrier/airport-carrier/route-carrier month
  07_carrier_families.sql  merger map (dim) + family-keyed gold views
ingest/
  load_t100.py     header-driven, batch-tracked loader
  backfill.sh      one-time historical pull (loop years x flavors)
  requirements.txt
.github/workflows/
  ingest.yml       monthly scheduled run (+ manual trigger)
```

## Ingestion

There is **no JSON/REST API** for raw monthly T-100, and — verified live,
July 2026 — **no stable static files either**. The candidate mechanisms, and
what investigating them actually showed:

1. **PREZIP "static" files** (`https://transtats.bts.gov/PREZIP/`) — looks like
   a bulk-file archive, but the T-100 zips there are just cached outputs of
   *other users'* form submissions: numeric request-id prefixes, arbitrary
   column subsets and year ranges (one sampled file was a 10-column, 2012-only
   extract generated in 2015). Loading one blindly would replace good data
   with junk. **Not usable.**
2. **Open data portals** (catalog.data.gov / geodata.bts.gov) — host a "T-100
   Domestic Market and Segment" dataset, but it's annual airport-level totals:
   the wrong grain for a warehouse fact table. **Not usable.**
3. **On-demand form** (`DL_SelectFields.aspx`) — the column-picker UI. It's
   ASP.NET WebForms, so scripting it means harvesting `__VIEWSTATE` /
   `__EVENTVALIDATION` from a GET and POSTing them back. That turns out to be
   entirely workable with stdlib `urllib` + a cookie jar, and the POST response
   is the zip itself. **This is what the loader does.**

### The loader: `ingest/load_t100.py`
Header-driven and batch-tracked. Per run it:
1. opens a `meta.ingest_batch` row (`started`) in its own committed txn, so a
   later failure still leaves a durable `failed` record;
2. fetches the CSV — replays the TranStats form (default), or takes an
   explicit `--url` / local `--file`; zips are unwrapped (and the
   `Documentation.csv` some downloads include is skipped);
3. reads the CSV header, normalises names, **drops BTS's trailing empty column**
   (older files only — 2026 form output is clean; both are handled);
4. `COPY`s into a TEMP staging table (all text);
5. inserts the columns that intersect the bronze table, stamping
   `_batch_id` / `_data_scope` / `_source_file`;
6. closes the batch (`loaded`, `row_count`, year range).

Form downloads are **one year per request**, so the default `--mode
replace-years` deletes only this scope's rows for the years present in the
file before inserting — rerunning a load converges those years to BTS's
latest revision, with no dedup logic. `--mode replace-scope` (wipe the whole
scope) suits full-history reloads; `--mode append` skips deletes entirely.

```bash
pip install -r ingest/requirements.txt
export PGHOST=localhost PGUSER=t100 PGPASSWORD=t100 PGDATABASE=t100

python ingest/load_t100.py domestic_segment                 # latest year
python ingest/load_t100.py domestic_segment --year 2023     # a specific year
python ingest/load_t100.py domestic_market --month 1        # single month

# or load a file you downloaded by hand:
python ingest/load_t100.py domestic_market --file ~/Downloads/T100_market.zip
```

### How the form replay works (and why it's OK)
`DL_SelectFields.aspx` is a classic ASP.NET WebForms page. The loader:
1. GETs the table's page (each flavor has a `Table_ID`: domestic segment 259,
   international segment 261, domestic market 258, international market 260 —
   encoded in the URL with ROT13 extended over `[a-z0-9]`, so `gnoyr_VQ=FIM`
   means `table_ID=259`), keeping the session cookies;
2. scrapes the hidden `__VIEWSTATE` / `__EVENTVALIDATION` tokens and the list
   of field checkboxes from the HTML;
3. POSTs everything back with every field checked, *Prezipped File* checked,
   `cboGeography=All` and the requested `cboYear`;
4. streams the response, which is the zip itself (`Content-Disposition:
   attachment`).

This is the same request a browser makes — one download per flavor-year per
month is well within polite use of a public data service. The fragility risk
(BTS redesigns the page) is contained: the loader fails loudly if the form
doesn't parse, and `meta.ingest_batch` keeps the durable `failed` record.

### Scheduling (pick one)
- **GitHub Actions** — `.github/workflows/ingest.yml` runs monthly + on demand.
  Put `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE` in repo secrets. Best when
  the DB is reachable from the runner (managed/cloud Postgres).
- **cron on the DB host** — simplest for a private DB:
  ```cron
  # 09:00 on the 5th each month
  0 9 5 * * cd /path/to/t100 && PGHOST=localhost PGUSER=t100 PGPASSWORD=… PGDATABASE=t100 \
    /usr/bin/python3 ingest/load_t100.py domestic_segment >> /var/log/t100.log 2>&1
  ```

BTS releases monthly with a ~30-45 day lag; a monthly run over `--year latest`
and `--year prev` plus `replace-years` means you always converge to the latest
data, revisions included — no catch-up logic needed. (History further back is
a one-time backfill: loop `--year` over 1990..current.)

### Lookups
The `bronze.l_*` decode tables are two-column `code,description` downloads
with their own pages on TranStats; finding their Table_IDs and extending
`SOURCES` covers them. Left out here to keep step 2 focused on the fact tables.

### Airport geography
`bronze.airports` lands OpenFlights `airports.dat` (names, IATA/ICAO codes,
lat/lon, timezones) via `load_t100.py airports` — the file has no header row,
so its 14 column names live in `SOURCES` instead of being read from the file.
It has no domestic/international scope, so each load fully replaces the table.
BTS doesn't publish coordinates, which is why this comes from a second source;
the `iata` column joins to T-100 `origin`/`dest`. Expect a LEFT JOIN in silver:
OpenFlights covers >99.6% of T-100 *passengers* but only about half of the
distinct airport codes (T-100 reports many tiny airfields OpenFlights lacks).

## Silver
Built as **materialized views** (`init/05_silver.sql`) — the transform is pure
SQL over bronze, so refreshing is just re-running it:
```sql
SELECT gold.refresh_all();   -- run after any bronze load (~4 min, silver+gold)
```
`silver.fact_segment` / `silver.fact_market` give you ISO month dates
(`flight_month`), typed measures with units in the names (`passengers`,
`seats`, `distance_miles`, `freight_lb`), decoded aircraft type and service
class names, and denormalized geography (city/state/country + lat/lon) —
while the surrogate-ID and FIPS/WAC code columns from the raw files are
dropped. Reporting-entity splits are summed away, so the segment grain is
month x scope x carrier x route x aircraft type x class.

## Gold
Chart-ready aggregates (`init/06_gold.sql`), scheduled passenger service only,
departure-side counting:
- `gold.carrier_month` — per-carrier system totals (seats, pax, load factor,
  airports/routes served); use top-N by seats to define "major carriers".
- `gold.airport_carrier_month` — airport x carrier x month with the two hub
  metrics: `pct_of_carrier_system` (how much of a hub the airport is for the
  airline) and `pct_of_airport_seats` (how dominant the airline is there).
- `gold.route_carrier_month` — routes with endpoint lat/lon for spoke maps.
- `gold.family_month` / `gold.airport_family_month` — same metrics keyed by
  **carrier family** (`silver.dim_carrier_family` merger map: TWA/US Airways/
  America West→American, Northwest→Delta, Continental→United, AirTran→
  Southwest, Virgin America/Hawaiian→Alaska), for continuous trend lines
  across mergers. Pre-merger values are pro-forma combined.

## Visualizations
- **Notebook** (`notebooks/hub_utilization_eda.ipynb`): Jupyter + plotly EDA.
  Launch: `.venv/bin/jupyter lab notebooks/hub_utilization_eda.ipynb`
- **Streamlit app** (`app/`): interactive Route Explorer (load factors by
  month on any route) + Hub Utilization pages.
  Launch: `.venv/bin/streamlit run app/Route_Explorer.py` → localhost:8501.
  Connection via `$T100_DSN` (defaults to the local Docker Postgres).
