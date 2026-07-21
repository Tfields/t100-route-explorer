#!/usr/bin/env python3
"""
load_t100.py — ingest a BTS T-100 file into the bronze layer.

Pipeline per run (single DB transaction for the data):
  1. open a meta.ingest_batch row (status='started')
  2. fetch the CSV — replay the TranStats download form (default), or take an
     explicit --url / local --file
  3. read the CSV header, normalise column names, drop the trailing empty col
  4. COPY the CSV into a TEMP staging table (all TEXT, exact CSV columns)
  5. for the chosen mode, write into the bronze table with batch metadata
  6. close the batch row (status='loaded', row_count, period range)
On error the batch row is marked 'failed' with the message, and no partial
data is left behind (the data transaction is rolled back).

Design choices:
  * Header-driven: we don't hard-code column order. Whatever columns the file
    has, we create matching staging columns and insert the intersection with
    the bronze table. This tolerates BTS's trailing-empty-column quirk and
    minor schema drift.
  * Default mode 'replace-years': form downloads are one year per request, so
    each load deletes just this scope's rows for the years present in the
    file, then inserts. Re-running converges to BTS's latest revision of those
    years. 'replace-scope' wipes the whole scope first (for full-history
    reloads); 'append' skips all deletes.

Connection: standard libpq env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD,
PGDATABASE) or --dsn.
"""

import argparse
import csv
import http.cookiejar
import io
import os
import re
import sys
import tempfile
import zipfile
from urllib.parse import urlencode
from urllib.request import urlopen, Request, build_opener, HTTPCookieProcessor

import psycopg2

# --- known T-100 flavors -----------------------------------------------------
# table_id = the TranStats Table_ID behind DL_SelectFields.aspx (verified live
# against each page's heading). There are NO stable static T-100 files:
# /PREZIP/ only caches other users' form extracts (arbitrary columns/years),
# so the loader replays the download form instead — see fetch_from_form().
SOURCES = {
    "domestic_segment":      {"table": "bronze.t100_segment", "scope": "domestic",      "table_id": 259},  # T-100 Domestic Segment (U.S. Carriers)
    "international_segment":  {"table": "bronze.t100_segment", "scope": "international",  "table_id": 261},  # T-100 International Segment (All Carriers)
    "domestic_market":       {"table": "bronze.t100_market",  "scope": "domestic",      "table_id": 258},  # T-100 Domestic Market (U.S. Carriers)
    "international_market":   {"table": "bronze.t100_market",  "scope": "international",  "table_id": 260},  # T-100 International Market (All Carriers)

    # Geographic reference (OpenFlights airports.dat): fixed columns, NO header
    # row — "columns" below supplies the names the header would. scope=None:
    # no domestic/international split, so every load fully replaces the table.
    "airports": {"table": "bronze.airports", "scope": None,
                 "columns": ["airport_id", "name", "city", "country", "iata",
                             "icao", "latitude", "longitude", "altitude_ft",
                             "utc_offset", "dst", "tz_database", "type", "source"],
                 "url": "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"},

    # BTS L_* decode lookups: headered (code,description) CSVs from
    # Download_Lookup.asp. The query strings are the site's own obfuscated
    # forms (value = ROT13 of the lookup name), taken verbatim from the
    # DL_SelectFields page links — don't "fix" their odd casing.
    "l_aircraft_type":    {"table": "bronze.l_aircraft_type",    "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_NVePeNSg_glcR"},
    "l_aircraft_config":  {"table": "bronze.l_aircraft_config",  "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_NVePeNSg_PbaSVT"},
    "l_aircraft_group":   {"table": "bronze.l_aircraft_group",   "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_NVePeNSg_Tebhc"},
    "l_service_class":    {"table": "bronze.l_service_class",    "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_fReiVPR_PYNff"},
    "l_carrier_group_new": {"table": "bronze.l_carrier_group_new", "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_PNeeVRe_Tebhc_aRj"},
    "l_data_source":      {"table": "bronze.l_data_source",      "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_QNgN_fbhePR"},
    "l_airport_id":       {"table": "bronze.l_airport_id",       "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_NVecbeg_VQ"},
    "l_city_market_id":   {"table": "bronze.l_city_market_id",   "scope": None,
                           "url": "https://transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_PVgl_ZNeXRg_VQ"},
}

# --- TranStats form replay ----------------------------------------------------
# DL_SelectFields.aspx is ASP.NET WebForms: GET the page for its __VIEWSTATE /
# __EVENTVALIDATION tokens plus the list of field checkboxes, then POST it all
# back with every field and 'Prezipped File' checked. The response body IS the
# zip (Content-Disposition attachment) — one year per request (cboYear).
# Query strings are obfuscated with ROT13 extended over [a-z0-9] ("gnoyr_VQ"
# decodes to "table_ID"); _vq_encode() turns a table id into that scheme.
DL_FORM = "https://transtats.bts.gov/DL_SelectFields.aspx"
DB_NAME_QS = "QO_fu146_anzr=Nv4%20Pn44vr45"   # "db_short_name=Air Carriers"
_VQ = "abcdefghijklmnopqrstuvwxyz0123456789"
_UA = {"User-Agent": "Mozilla/5.0 (t100-loader/1.0)"}


def _vq_encode(s: str) -> str:
    return "".join(_VQ[(_VQ.index(c) + 13) % 36] if c in _VQ else c for c in s)


def fetch_from_form(table_id: int, year: str = "latest", month: str = None,
                    timeout: int = 600, retries: int = 2) -> tuple[str, bytes]:
    """Replay the TranStats download form; return (label, raw zip bytes).
    Retries the whole GET+POST on network errors (stalled reads happen, e.g.
    around laptop sleep); server-side errors (bad year, layout change) don't."""
    for attempt in range(retries + 1):
        try:
            return _fetch_from_form_once(table_id, year, month, timeout)
        except OSError as e:   # socket.timeout, URLError, ConnectionError, ...
            if attempt == retries:
                raise
            print(f"[retry] fetch attempt {attempt + 1} failed ({e}); retrying",
                  file=sys.stderr)


def _fetch_from_form_once(table_id, year, month, timeout) -> tuple[str, bytes]:
    url = f"{DL_FORM}?gnoyr_VQ={_vq_encode(str(table_id)).upper()}&{DB_NAME_QS}"
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    html = opener.open(Request(url, headers=_UA), timeout=timeout).read().decode("utf-8", "replace")

    hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)"[^>]*value="([^"]*)"', html))
    fields = [n for n in re.findall(r'type="checkbox" name="([^"]+)"', html)
              if not n.startswith("chk")]
    years = re.findall(r'<option[^>]*value="(\d{4})"', html)
    if not (hidden.get("__VIEWSTATE") and fields and years):
        raise RuntimeError("TranStats form page didn't parse — has the layout changed?")

    if year in (None, "latest"):
        year = max(years)
    elif year == "prev":
        year = str(int(max(years)) - 1)
    if year not in years:
        raise RuntimeError(f"year {year} not offered by the form ({min(years)}..{max(years)})")

    form = dict(hidden)
    form.update({"btnDownload": "Download", "chkDownloadZip": "on",
                 "cboGeography": "All", "cboYear": year,
                 "cboPeriod": month or "All"})
    for f in fields:   # the form rejects a download with no variables selected
        form[f] = "on"
    resp = opener.open(Request(url, data=urlencode(form).encode(),
                               headers={**_UA, "Referer": url}), timeout=timeout)
    raw = resp.read()
    if raw[:2] != b"PK":
        err = re.search(rb"alert\('([^']*)'\)", raw)
        raise RuntimeError("form replay returned HTML, not a zip: "
                           + (err.group(1).decode() if err else "unknown error"))
    m = re.search(r"filename=([^;]+)", resp.headers.get("Content-Disposition", ""))
    return (m.group(1).strip() if m else f"table{table_id}_{year}.zip"), raw


def normalise(name: str) -> str:
    """lowercase, trim, non-alnum -> underscore. Empty -> '' (caller drops)."""
    n = name.strip().strip('"').strip().lower()
    n = re.sub(r"[^0-9a-z]+", "_", n).strip("_")
    return n


def unzip_first_csv(label: str, raw: bytes) -> tuple[str, bytes]:
    """If raw is a zip, pull the data .csv member out; else pass through.
    Form downloads can also ship a Documentation.csv of field descriptions —
    skip it (fall back to the largest member if that leaves nothing)."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            csvs = [i for i in z.infolist() if i.filename.lower().endswith(".csv")]
            data = [i for i in csvs if not i.filename.lower().startswith("documentation")] or csvs
            pick = max(data, key=lambda i: i.file_size)
            return f"{label}::{pick.filename}", z.read(pick.filename)
    return label, raw


def fetch_csv_bytes(url: str = None, file: str = None) -> tuple[str, bytes]:
    """Return (source_label, raw_csv_bytes) from a local file or an explicit url."""
    if file:
        raw = open(file, "rb").read()
        label = os.path.basename(file)
    else:
        req = Request(url, headers=_UA)
        with urlopen(req, timeout=300) as r:   # nosec - public gov data
            raw = r.read()
        label = url.rsplit("/", 1)[-1]
    return unzip_first_csv(label, raw)


def load(args):
    src = SOURCES[args.source]
    table = src["table"]
    scope = src["scope"]

    # Resolve the CSV: explicit --file/--url wins, then the TranStats form
    # replay (T-100 flavors), then the source's fixed URL (reference tables).
    if args.file or args.url:
        source_label, csv_bytes = fetch_csv_bytes(url=args.url, file=args.file)
    elif "table_id" in src:
        label, raw = fetch_from_form(src["table_id"], year=args.year, month=args.month)
        source_label, csv_bytes = unzip_first_csv(label, raw)
    else:
        source_label, csv_bytes = fetch_csv_bytes(url=src["url"])

    # Decode up front: BTS files are UTF-8, but a few lookup CSVs (e.g.
    # L_AIRPORT_ID) ship as Windows-1252 — fall back rather than fail mid-read.
    try:
        decoded = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = csv_bytes.decode("cp1252")
    reader = csv.reader(io.StringIO(decoded, newline=""))
    if "columns" in src:
        # Headerless fixed-schema source: SOURCES supplies the column names.
        keep = list(enumerate(src["columns"]))
    else:
        # Read header + normalise; track which CSV columns map to real names.
        raw_header = next(reader)
        norm = [normalise(h) for h in raw_header]
        # keep index -> name only for non-empty names (drops trailing empty col)
        keep = [(i, nm) for i, nm in enumerate(norm) if nm]
    csv_cols = [nm for _, nm in keep]

    conn = (psycopg2.connect(args.dsn, options="-c client_encoding=UTF8")
            if args.dsn else psycopg2.connect(options="-c client_encoding=UTF8"))

    # 1) open the batch row in its OWN committed transaction, so a later failure
    #    still leaves a durable 'failed' record instead of rolling the row away.
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO meta.ingest_batch
               (target_table, source_file, data_scope, status, notes)
               VALUES (%s, %s, %s, 'started', %s) RETURNING batch_id""",
            (table, source_label, scope, f"mode={args.mode} year={args.year}"),
        )
        batch_id = cur.fetchone()[0]
    conn.autocommit = False  # data work below is one atomic transaction

    try:
        with conn.cursor() as cur:
            # 2) staging temp table with exactly the CSV columns (all text)
            cols_ddl = ", ".join(f'"{c}" text' for c in csv_cols)
            cur.execute(f"CREATE TEMP TABLE _stage ({cols_ddl}) ON COMMIT DROP")

            # 3) COPY the CSV in. We re-emit a clean CSV (only kept columns) so
            #    the trailing empty column and any header oddities are handled.
            buf = io.StringIO()
            w = csv.writer(buf)
            keep_idx = [i for i, _ in keep]
            for row in reader:
                w.writerow([row[i] if i < len(row) else "" for i in keep_idx])
            buf.seek(0)
            copy_sql = f'COPY _stage ({", ".join(chr(34)+c+chr(34) for c in csv_cols)}) FROM STDIN WITH (FORMAT csv)'
            cur.copy_expert(copy_sql, buf)
            cur.execute("SELECT count(*) FROM _stage")
            row_count = cur.fetchone()[0]

            # 4) intersect staging cols with the bronze table's real columns
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = split_part(%s,'.',1)
                     AND table_name  = split_part(%s,'.',2)
                     AND column_name NOT LIKE '\\_%%'""",
                (table, table),
            )
            bronze_cols = {r[0] for r in cur.fetchall()}
            insert_cols = [c for c in csv_cols if c in bronze_cols]
            if not insert_cols:
                raise RuntimeError("no CSV columns matched the bronze table - wrong source/file?")

            # 5) replace-years (default): drop only this scope's rows for the
            #    years present in the file, since form downloads are per-year.
            #    replace-scope wipes the whole scope (full-history reloads).
            #    Scope-less reference tables (scope=None) have no year/scope to
            #    key on, so either replace mode swaps the whole table.
            if scope is None and args.mode != "append":
                cur.execute(f"DELETE FROM {table}")
            elif args.mode == "replace-years":
                if "year" not in insert_cols:
                    raise RuntimeError("replace-years needs a 'year' column in the file")
                cur.execute("SELECT DISTINCT year FROM _stage")
                file_years = [r[0] for r in cur.fetchall()]
                if file_years:
                    cur.execute(f"DELETE FROM {table} WHERE _data_scope = %s AND year = ANY(%s)",
                                (scope, file_years))
            elif args.mode == "replace-scope":
                cur.execute(f"DELETE FROM {table} WHERE _data_scope = %s", (scope,))

            col_list = ", ".join(f'"{c}"' for c in insert_cols)
            meta_cols = ["_batch_id", "_source_file"] if scope is None \
                else ["_batch_id", "_data_scope", "_source_file"]
            meta_vals = [batch_id, source_label] if scope is None \
                else [batch_id, scope, source_label]
            cur.execute(
                f"""INSERT INTO {table} ({col_list}, {", ".join(meta_cols)})
                    SELECT {col_list}, {", ".join(["%s"] * len(meta_vals))} FROM _stage""",
                meta_vals,
            )

            # 6) period range for logging (best-effort; columns are text)
            yr_lo = yr_hi = None
            if "year" in insert_cols:
                cur.execute("SELECT min(year), max(year) FROM _stage WHERE year ~ '^[0-9]+$'")
                yr_lo, yr_hi = cur.fetchone()

            cur.execute(
                """UPDATE meta.ingest_batch
                   SET status='loaded', row_count=%s, finished_at=now(),
                       reporting_year = CASE WHEN %s = %s THEN %s::int ELSE NULL END,
                       notes = notes || %s
                   WHERE batch_id = %s""",
                (row_count, yr_lo, yr_hi, yr_lo,
                 f"; years {yr_lo}-{yr_hi}; cols {len(insert_cols)}", batch_id),
            )
        conn.commit()
        print(f"[ok] batch {batch_id}: loaded {row_count} rows into {table} "
              f"(scope={scope}, source={source_label})")
    except Exception as e:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE meta.ingest_batch SET status='failed', finished_at=now(), error_message=%s WHERE batch_id=%s",
                (str(e)[:2000], batch_id),
            )
        conn.commit()
        print(f"[fail] batch {batch_id}: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description="Load a BTS T-100 file into bronze.")
    p.add_argument("source", choices=sorted(SOURCES), help="which T-100 flavor")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--file", help="local CSV or ZIP to load (skips download)")
    g.add_argument("--url", help="explicit download URL (skips the form replay)")
    p.add_argument("--year", default="latest",
                   help="form fetch: 'latest' (default), 'prev', or a year like 2024")
    p.add_argument("--month", help="form fetch: 1-12 for one month (default: all months)")
    p.add_argument("--mode", choices=["replace-years", "replace-scope", "append"],
                   default="replace-years")
    p.add_argument("--dsn", help="libpq DSN; else uses PG* env vars")
    load(p.parse_args())


if __name__ == "__main__":
    main()
