# CLAUDE.md — T-100 Postgres warehouse

Project memory for Claude Code. Read this before doing anything; it captures
decisions already made so you don't re-derive or contradict them.

## What this is
A medallion-structured Postgres warehouse for BTS T-100 airline traffic data.
All three layers are built and tested: bronze (landing + ingestion), silver
(typed/decoded/geolocated matviews), gold (chart-ready aggregates). Next work:
**visualizations** (local EDA on hub utilization).

## Stack
- Postgres 16 (Docker via `docker-compose.yml`, or any existing PG). On this
  Mac the container runtime is **Colima + docker CLI** (chosen over Docker
  Desktop: free, no GUI, no extra admin prompts, fully scriptable; the compose
  file works unchanged)
- Python loader, `psycopg2-binary` only (stdlib `urllib` for downloads).
  CI uses 3.12; the Mac's system 3.9 works too (venv at `.venv/`)
- Layers are **schemas in one database** (`bronze`, `silver`, `gold`, `meta`)

## Layout
```
docker-compose.yml          local Postgres 16 (db/user/pass = t100)
init/01_schemas.sql         schemas, roles (t100_etl/t100_ro), grants
init/02_meta.sql            meta.ingest_batch (load tracking / lineage)
init/03_bronze.sql          bronze.t100_segment, bronze.t100_market, bronze.l_*
init/04_geo.sql             bronze.airports (OpenFlights geographic reference)
init/05_silver.sql          silver matviews: dim_airport, fact_segment, fact_market
init/06_gold.sql            gold matviews: carrier_month, airport_carrier_month,
                            route_carrier_month (+ gold.refresh_all())
init/07_carrier_families.sql silver.dim_carrier_family (authored merger map) +
                            gold.family_month, gold.airport_family_month
ingest/load_t100.py         header-driven, batch-tracked loader
ingest/backfill.sh          one-time historical pull (loop years x flavors)
ingest/requirements.txt
notebooks/hub_utilization_eda.ipynb  local EDA (Jupyter+plotly; deps in notebooks/requirements.txt)
app/Route_Explorer.py       Streamlit app entry (run: .venv/bin/streamlit run app/Route_Explorer.py)
app/pages/2_Hub_Utilization.py  second page; app/warehouse.py shared cached query helper
app/pages/4_Carrier_Families.py family mergers + route churn (see STREAMLIT APP)
.github/workflows/ingest.yml monthly scheduled run
```

## Commands
```bash
# one-time machine setup (macOS) — the Homebrew install prompts for an admin
# password, so it's a human step; everything after it is scriptable:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install colima docker docker-compose
colima start                              # userspace Linux VM; docker CLI talks to it

# bring up DB (init/*.sql auto-run on first boot)
docker compose up -d                      # localhost:5432, db=t100, user=t100, pass=t100

# or apply to an existing PG
psql -d t100 -v ON_ERROR_STOP=1 -f init/01_schemas.sql   # then 02, 03

# loader (uses PG* env vars: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE)
.venv/bin/python ingest/load_t100.py domestic_segment              # latest year, via form replay
.venv/bin/python ingest/load_t100.py domestic_segment --year 2023  # a specific year
.venv/bin/python ingest/load_t100.py domestic_market --file ~/Downloads/some_t100.zip
.venv/bin/python ingest/load_t100.py airports                     # OpenFlights geo reference
.venv/bin/python ingest/load_t100.py l_aircraft_type              # (any of the l_* lookups)

# after bronze loads, rebuild silver + gold (matviews; ~4 min full history):
docker exec t100_postgres psql -U t100 -d t100 -c "SELECT gold.refresh_all();"
```

## Domain facts (don't re-derive)
- T-100 has 4 flavors: {domestic, international} x {segment, market}.
- **Segment** = nonstop leg grain (carrier x O&D x aircraft type x class); has
  capacity/ops fields (seats, departures_*, payload, ramp_to_ramp, air_time).
- **Market** = on-flight O&D grain (carrier x O&D x class); demand only, no
  capacity/aircraft fields.
- Domestic and international column sets are **nearly** identical, not
  identical (earlier assumption, corrected): domestic files carry `*_state_*`
  fields, international files carry `*_country*` fields. Bronze has both;
  each scope leaves the other's columns NULL.
- `L_*` decode lookups download as plain headered CSVs from
  `Download_Lookup.asp?Y11x72=<ROT13 of lookup name>` (URLs pinned verbatim in
  `SOURCES`). L_AIRPORT_ID ships as Windows-1252, not UTF-8 — the loader
  falls back to cp1252 when UTF-8 decoding fails.
- Older source CSVs ship a **trailing empty column** (trailing comma); 2026
  form output doesn't. The loader handles both. Download zips may also include
  a `Documentation.csv` of field descriptions; the loader skips it.
- There is **no API and no stable static files** (verified live 2026-07-03).
  `/PREZIP/` is just a cache of *other users'* form extracts — arbitrary
  columns/years, some from 2015; never load those. data.gov / geodata.bts.gov
  "T-100" datasets are annual airport-level aggregates — wrong grain.
  The only full-grain source is **replaying the `DL_SelectFields.aspx` form
  POST** (harvest `__VIEWSTATE`, check every field + 'Prezipped File'); the
  response body is the zip. Downloads are **one year per request** (`cboYear`).
- TranStats obfuscates query strings with ROT13 extended over `[a-z0-9]`
  (`gnoyr_VQ` = `table_ID`). Verified Table_IDs: domestic segment **259**,
  international segment **261**, domestic market **258**, international
  market **260** (all "U.S. Carriers" domestically, "All Carriers" internationally).
- **Airport geography** comes from OpenFlights `airports.dat` (loader source
  `airports`, default URL = the OpenFlights GitHub raw file): 14 fixed columns,
  **no header row** (names live in `SOURCES`), `\N` as the null sentinel (kept
  as-is in bronze). Join key to T-100 is `iata` = `origin`/`dest`. Coverage is
  ~50% of *distinct* T-100 airport codes but **>99.6% of passengers** — T-100
  includes thousands of tiny airfields OpenFlights lacks, so silver must
  LEFT JOIN and never inner-join geography onto the facts.

## Hard rules
- **Bronze is raw**: every column TEXT, no casting/validation. All cleaning,
  typing, and lookup joins happen in **silver**, never bronze.
- **One `t100_segment` + one `t100_market` table**, domestic/international
  separated by the `_data_scope` column. Don't split into per-scope tables.
- **Loader stays header-driven** — never hard-code column order; it inserts the
  intersection of CSV columns and bronze columns.
- **Default load mode is `replace-years`** (delete that scope's rows for the
  years present in the file, reload them). Form downloads are per-year, so each
  run converges the loaded years to BTS's latest revision — no dedup logic.
  `replace-scope` (wipe the whole scope) remains for full-history reloads.
  (The old `replace-scope` default assumed one file = full history; that died
  with the switch to per-year form downloads.)
- Metadata columns are `_`-prefixed (`_batch_id`, `_data_scope`, `_source_file`,
  `_loaded_at`); never collide source columns with these.
- SQL is idempotent (`IF NOT EXISTS`). Keep it that way.
- **Never commit secrets.** The `t100`/`t100` creds are local-only; real
  deployments set a real password and use env/secrets, not the compose default.

## Open items
1. **Monthly run on this machine.** GitHub Actions can't reach a laptop-local
   DB; schedule the four `--year latest` + `--year prev` loads via launchd/cron
   locally (or run them by hand after each BTS release), followed by
   `SELECT gold.refresh_all();`.
2. **Remaining unwired lookups** (low value): `l_carrier` (carrier names come
   inline in the facts), `l_world_area_code`, `l_distance_group_500` — the
   `Download_Lookup.asp` pattern covers them if ever needed.

## SILVER (built — init/05_silver.sql)
Materialized views over bronze; rebuild with `SELECT silver.refresh_all();`
after any bronze load. Changing a view definition needs DROP + re-run of the
file (IF NOT EXISTS won't update it).
- `silver.dim_airport` — one row per IATA from OpenFlights (typed lat/lon, tz).
- `silver.fact_segment` — grain: **flight_month (ISO date, first of month) x
  scope x carrier x origin x dest x aircraft_type x service_class**. Bronze's
  entity/region/config splits are SUMmed away. IDs dropped; codes keep their
  decoded names (`aircraft_type_name`, `service_class_name` — 100% decoded);
  city/state/country + lat/lon denormalized in (country/geo ~97.5% of rows,
  >99.6% of passengers; gaps = tiny airports OpenFlights lacks). Units in
  column names: `distance_miles`, `freight_lb`, `air_time_min`.
- `silver.fact_market` — same shape minus capacity/aircraft.
- `silver.to_num(text)` — safe numeric cast; BTS sentinels/blanks → NULL.
- Known source quirk kept as-is: ~0.04% of segment rows report
  passengers > seats (charters/misreports). Silver types data, it doesn't
  overrule the source.

## GOLD (built — init/06_gold.sql)
Chart-ready matviews; rebuild everything derived with
`SELECT gold.refresh_all();` (refreshes silver first, then gold).
Conventions: **service class F only** (scheduled passenger = 99.3% of pax;
charters/cargo excluded), **departure-side counting** (arrivals symmetric),
domestic+international combined. Carrier codes step at mergers (NW→DL 2010,
CO→UA 2012, US→AA 2015) — real events, not bugs.
- `gold.carrier_month` (73K rows) — system totals per carrier: seats, pax,
  LF, airports/routes served. Defines "major carriers" via top-N by seats.
- `gold.airport_carrier_month` (1.55M rows) — hub workhorse. Share metrics:
  `pct_of_carrier_system` (airport's share of carrier seats — hub weight) and
  `pct_of_airport_seats` (carrier's share of airport seats — dominance).
- `gold.route_carrier_month` (6.1M rows) — routes with both endpoints'
  lat/lon for spoke maps.
- **Carrier families** (init/07): `silver.dim_carrier_family` is an AUTHORED
  seed (TRUNCATE+INSERT on re-run) mapping merged codes to survivors —
  AA⊃{TW,US,HP}, DL⊃{NW}, UA⊃{CO,CS}, WN⊃{FL}, AS⊃{VX,HA}. Unlisted codes
  map to themselves via COALESCE, so coverage is total and
  SUM(family) == SUM(carrier) exactly (verified). `gold.family_month` and
  `gold.airport_family_month` mirror the carrier views with continuous
  lines across mergers (`pct_of_family_system` replaces per-code share);
  pre-merger values are pro-forma combined. merged_month is documentation
  only. 07 supersedes 06's gold.refresh_all() to include the family views.

## STREAMLIT APP (app/)
Four pages (shared `month_window()` helper in warehouse.py: month-range
select_slider defaulting Jan 2022 → latest on every page except
Carrier Families, which defaults to full history):
**Route Explorer** (carrier-code vs family radio [family mode
aggregates the route query via COALESCE(f.family, r.carrier)];
origin/dest/carrier pickers — defaults HNL⇄LAX with AS/DL/UA/AA/WN/HA
preselected on first visit; carrier multiselect is keyed by CODE with
names via format_func so the selection survives route changes AND the
mode toggle; month-range select_slider (not years) — monthly load
factor + capacity-vs-demand + **share-gap section** [LF vs seat-share bubbles;
bar chart has a radio toggle between two same-sign gap metrics:
LF−route-avg (size-independent) and pax-share−seat-share (size-weighted,
zero-sum; share_gap = seat_share × LF_gap / route_LF), over the selected
year window] + route map;
"combine both directions" recomputes LF on summed totals; **carrier filter is
sticky across route changes** — remembered in session_state, intersected with
the new route's carriers, "all selected" remembered as no-filter) and
**Hub Utilization** (carrier-code vs family radio,
hub-weight + fortress charts windowed by month_window [smoothing runs
on full history first, then windows], spoke map with month slider
clamped to the window); and **Destinations** (all nonstops from one
airport — defaults SEA; carrier multiselect auto-selects ALL carriers
and intentionally re-selects all on airport change, no stickiness;
spoke map + top-15 dests stacked by carrier + dests-per-month trend +
best/worst performers section [carrier×route LF ranking with seats/month
floor + ≥3-months-served to exclude one-offs, dom/intl radio, diverging
bars around scope-avg LF, companion volume-vs-LF scatter; NOTE: T-100
has self-loop rows (origin=dest, e.g. SEA→SEA) — excluded here] +
momentum section [last-12-months-ending-at-window-end vs the 12 before:
YoY LF-delta diverging bars computed ONLY over calendar months served
in both years (seasonal like-for-like), and new/dropped carrier-route
tables (≥2 months in one period, 0 in the other, seats/mo floor);
shares the scope/floor/N controls with the performers section]).
**Carrier Families** (4_Carrier_Families.py — family selectbox over the 5
authored families, ordered by last-12-mo seats; month_window with
default_start=1990 so it opens on FULL history; metrics row + merger-timeline
expander from dim_carrier_family; stacked area of member-code seats/mo with
white 1px seams between bands [DL vs NW reds are CVD-close, ΔE 9.2 — seams,
legend and hover are the mandated secondary encoding; **px.area gotcha:**
fillcolor derives from the line color, so setting line=white alone greys the
fills out — pin tr.fillcolor to the brand color first, then whiten the line; CS added to
CARRIER_COLORS as olive #5C7A29, validated next to UA/CO] and family
routes_served line, both with dotted merger vlines [add_vline needs
str(date), not Timestamp]; **route churn** = undirected city pairs
(LEAST/GREATEST, self-loops excluded), gap-aware in SQL via LAG/LEAD: an
"add" = first month after a >366-day gap (so annual seasonal routes don't
churn), an "end" = last month before one; adds censored before 1991
(start of history), ends censored within 12 mo of latest month; yearly
diverging bars seagreen above / indianred below; recent-moves tables =
12-mo-to-window-end vs the prior 12 (≥2 months in one, 0 in the other,
seats/mo floor slider applied to the city pair both-directions), with
operating member codes via STRING_AGG).
Streamlit 1.50 API split: st.dataframe takes width='stretch' but
st.plotly_chart still needs use_container_width=True (width= is
swallowed into plotly config kwargs and warns).
Maps use Scattergeo resolution=50 (1:50m) with subunits + lakes.
warehouse.py also has CARRIER_COLORS (brand colors by code: UA blue,
DL red, AA grey, AS green, HA purple, WN yellow, ...) +
carrier_color_map(df) building a name-keyed color_discrete_map that
works in carrier AND family mode; top-dests stacked bar lumps beyond
top-10 carriers into grey 'Other' to keep the legend readable. Data access via
`app/warehouse.py`: `q()` is `st.cache_data`-cached (1h TTL), engine from
`$T100_DSN` env var (defaults to localhost) so hosting later is a config
change. **Gotcha found by test:** Postgres DATE → python `date` objects in
pandas (object dtype, no `.dt`); `q()` converts `flight_month` to datetime.
**AppTest gotcha:** a range select_slider whose format_func raises
AttributeError on a tuple breaks AppTest serialization (framework only
catches TypeError/ValueError before its per-element fallback) — wrap
values, e.g. `pd.Timestamp(m).strftime(...)`.
Verified with `streamlit.testing.v1.AppTest` (both pages + an interaction,
0 exceptions) and a headless server boot (health ok). Run:
`.venv/bin/streamlit run app/Route_Explorer.py` → http://localhost:8501.

## VISUALIZATIONS (started — notebooks/)
Tooling chosen: **Jupyter + plotly** in `.venv` (`pip install -r
notebooks/requirements.txt`; launch with `.venv/bin/jupyter lab`).
`hub_utilization_eda.ipynb` covers: majors by seats, hub weight
(pct_of_carrier_system), fortress dominance (pct_of_airport_seats),
dehubbing gallery (CVG/STL/PIT/MEM/CLE), ATL spoke map. All queries hit gold;
12-mo rolling means de-seasonalize trends. Executed end-to-end 2026-07-06
(8 figures, 0 errors).

## HOSTING (in progress — free-tier plan)
Target: **Streamlit Community Cloud (free) + gold as Parquet + DuckDB** — no
hosted database. Laptop stays the warehouse; after each monthly refresh,
export gold and publish the files to a GitHub Release the app downloads on
cold start. Free managed Postgres was ruled out by measurement: gold is
1.9 GB in PG vs 0.5–1 GB free-tier caps (Neon/Supabase/Aiven, checked
2026-07-18).
- `export/export_gold.py` (duckdb postgres extension, `postgres_query()` —
  matviews aren't visible to plain postgres_scanner). Exports **gold's 5
  matviews + silver.dim_carrier_family** (the one non-gold table the app
  queries — merger map joined on every family-mode query; grep-verified
  against app/*.py + app/pages/*.py as the app's complete data surface).
  Verified 2026-07-19: **83 MB total zstd Parquet**, ~15 s
  (route_carrier_month 6.09M rows → 46.6 MB vs 1.3 GB in PG). Row counts and
  sum(passengers) match PG exactly; types land as DATE/VARCHAR/BIGINT, PG
  numeric → DOUBLE (fine). ORDER BY carrier/airport/route before writing —
  clustering is what makes zstd+dictionary encoding hit ~20×. Output files
  are named `<table>.parquet` regardless of source schema (gold/silver both
  land flat in `export/out/`) — `warehouse.py`'s DuckDB backend re-adds the
  schema by creating `gold.*`/`silver.*` views over them.
- `export/out/` is gitignored; files ship via GitHub Releases (2 GB/file cap).
- **DuckDB backend built and verified 2026-07-20** (`app/warehouse.py`,
  `T100_BACKEND=duckdb` + `T100_PARQUET_DIR` for local files or
  `T100_PARQUET_URL` to fetch from a Release on first use). `q()` and all
  page SQL are unchanged — confirmed via `sqlalchemy`'s `duckdb-engine`
  dialect, which accepts the app's exact SQL surface (named `:param`
  binding, `STRING_AGG(DISTINCT … ORDER BY …)`, `FILTER`, `LAG`/`LEAD`
  windows, `date_trunc`, `INTERVAL`, `LEAST`/`GREATEST`) with zero rewrites.
  **Gotcha (cost real debugging time):** `create_engine("duckdb:///:memory:")`
  without `poolclass=StaticPool` intermittently loses the views created
  during setup — SQLAlchemy's default pool can hand back a different
  physical duckdb connection per `.connect()`, and each fresh connection to
  `:memory:` is its own empty database. Symptom was `CatalogException:
  Table … does not exist` on some pages but not others, order-dependent —
  looked like a missing-table bug, was actually a pooling bug. `StaticPool`
  pins the engine to one physical connection for its process lifetime; fine
  here since the app is read-only. Verified with all 4 pages under
  `streamlit.testing.v1.AppTest` (0 exceptions) + two widget interactions
  (Route Explorer origin change and carrier/family mode toggle, both
  re-query cleanly) + a real headless server boot on all 4 routes (200s,
  clean log).
- **Repo + Release published 2026-07-21**: local dir git-init'd, pushed to
  `https://github.com/Tfields/t100-route-explorer` (public). Re-ran
  `export/export_gold.py` first (confirmed `max(flight_month) = 2026-04-01`
  post-April-load) — 83.4 MB total, same table sizes as the 07-19 export.
  Assets published to Release tag `gold-2026-04`; `T100_PARQUET_URL =
  https://github.com/Tfields/t100-route-explorer/releases/download/gold-2026-04`
  (per-table `<name>.parquet`, verified resolves via signed blob redirect).
  **Gotcha:** `gh release create <tag> <files...>` (assets in the same
  command) intermittently 422'd with `ReleaseAsset.name already exists` and
  the release vanished entirely on failure (not left in a partial state) —
  worked around by creating the release with no assets, then
  `gh release upload <tag> <file> --clobber` per file in a loop.
- **Not yet done: the Community Cloud deploy itself** (share.streamlit.io UI,
  no CLI/API) — connect repo `Tfields/t100-route-explorer`, branch `main`,
  main file `app/Route_Explorer.py`; set secrets `T100_BACKEND=duckdb` and
  `T100_PARQUET_URL` (value above).

## Monthly incremental load — first real run (2026-07-20)
Ran the routine `README.md`/open-item #1 describes (by hand, not yet
cron/launchd): all 4 flavors × `--year latest` + `--year prev`, under
`caffeinate -i -w <pid>` (attach to the actual load-script PID, not `$$` of
a throwaway wrapper shell — the latter exits immediately and caffeinate
dies with it, silently providing no protection). 8/8 batches `loaded`, zero
failures (batch_id 235–242): domestic/international segment+market, latest
(2026, now includes **April**: BTS's release lag stepped from 3 to 4
months) and prev (2025, revision catch-up). `gold.refresh_all()` rerun
after; `gold.route_carrier_month` / `silver.fact_segment` both max out at
flight_month 2026-04-01. Spot check SEA→LAX Apr 2026: AS/DL/OO/F9/UA load
factors 66–86% — realistic.

## Verified state
Full stack verified end-to-end on this machine 2026-07-03/04:
- Colima + docker compose up: init scripts created all schemas/tables on first
  boot. Form-replay loads succeeded for **all four flavors**, `--year latest`
  (2026: months 1–3, matching BTS's ~3-month lag) and `--year prev` (2025:
  months 1–12).
- **Historical backfill complete** (2026-07-04): all 4 flavors × 1990–2026,
  no missing years (checked against generate_series), zero failed batches.
  **24.3M rows**: segment 13,877,353 / market 10,391,159. 140 form requests,
  ~45 s each; 5 transient network failures (laptop sleep stalled the socket),
  all clean — fetch fails before any DB write — and re-pulled successfully.
  Lessons baked in: `fetch_from_form` now retries network errors once or
  twice; run long pulls under `caffeinate -i -w <pid>` so the Mac stays awake.
- `replace-years` idempotency: re-running the same load left counts unchanged
  (112,745 → 112,745), batch ledger records both runs.
- OpenFlights airports loaded 2026-07-04: 7,698 rows (all unique ids, 6,072
  with real IATA codes), full-replace idempotency verified, passenger-weighted
  join coverage to segments 99.94% domestic / 99.66% international.
- 8 `L_*` lookups loaded 2026-07-04 via Download_Lookup.asp (451 aircraft
  types, 14 service classes, 6,884 airport ids, ...). International years
  1990–2026 re-pulled (74/74 clean) after adding the country columns; 100% of
  intl rows now carry origin/dest country names.
- Silver built 2026-07-04 (2m47s): fact_segment 13.72M rows, fact_market
  10.17M, dim_airport 6,072; months span 1990-01-01..2026-03-01. Spot check
  JFK→LHR Jun 2025: per-carrier/aircraft load factors 84–94% — realistic.
- Gold built 2026-07-06 (50s). Spot checks textbook-correct: ATL 73% Delta,
  Love Field 98% Southwest, CLT 69% American; Delta/CVG dehubbing visible
  (10.9M seats 2000 → 1.0M 2015).
- Family views built 2026-07-07 (30s). Verified: totals invariant exact;
  UA family 2010 = 120.0M seats vs 65.1M UA-code-only (CO folded in);
  AS family 2025 = AS 44.5M + HA 14.3M; DL family at MSP continuous across
  the NW merger (66% share 2005 → 49% 2011 → 57% 2025).
- Earlier (pre-form-replay) DB tests also covered the failure path: durable
  `failed` row in `meta.ingest_batch`, no partial data.
- Segment grain note for silver: rows repeat per carrier×O&D×month with
  different `aircraft_type` (and can differ only in `aircraft_config` /
  `unique_carrier_entity`) — the fact grain must include them.
- Gotcha demonstrated: bronze `month` is TEXT, so `max(month)`='9'
  lexicographically. Cast in silver; don't "fix" bronze.

## This machine (as of 2026-07-03)
arm64 Mac, set up and working: Homebrew, Colima (`colima start` after reboot),
docker CLI + compose plugin (wired via `cliPluginsExtraDirs` in
`~/.docker/config.json`), `t100_postgres` container on localhost:5432, venv at
`.venv/` (system Python 3.9 + psycopg2). No psql on the host — use
`docker exec t100_postgres psql -U t100 -d t100`.
