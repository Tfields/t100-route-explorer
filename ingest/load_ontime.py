#!/usr/bin/env python3
"""Load BTS Marketing Carrier On-Time Performance into bronze.ontime_marketing,
aggregated to month x marketing carrier x operating carrier x origin x dest.

Why aggregate at ingest (the one exception to "bronze is raw"): the source is
flight-level, ~665K rows x 119 columns a month (~35 MB zip, ~330 MB CSV - the
prezipped download ignores field selection), and its only use here is to
split monthly T-100 traffic by marketing carrier. Flights are counted and
discarded; nothing flight-level is stored.

Source: TranStats Table_ID 237, On-Time database, January 2018 onward, one
MONTH per form request. Same DL_SelectFields.aspx form replay as T-100.

  .venv/bin/python ingest/load_ontime.py --year 2025 --month 3
  .venv/bin/python ingest/load_ontime.py --from 2018-01 --to 2026-08   # backfill
  .venv/bin/python ingest/load_ontime.py --file some_month.zip          # local file

Re-loading a month replaces it (delete that year+month, insert), so reruns
converge on BTS's latest revision. Uses PG* env vars or --dsn.
"""
import argparse
import csv
import io
import sys
import time
import zipfile
from collections import defaultdict

import psycopg2

from load_t100 import fetch_from_form

TABLE = "bronze.ontime_marketing"
TABLE_ID = 237
DB_QS = "QO_fu146_anzr=b0-gvzr"          # "db_short_name=On-Time"
# CSV header names (they differ from the form's field names).
COLS = {"year": "Year", "month": "Month", "mkt": "Marketing_Airline_Network",
        "op": "Operating_Airline", "origin": "Origin", "dest": "Dest",
        "cancelled": "Cancelled", "diverted": "Diverted", "dup": "Duplicate"}


def open_csv(label: str, raw: bytes):
    """Yield a text stream over the flight CSV inside a download zip (or a
    bare CSV), without materialising the ~330 MB file as one string."""
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        # errors="replace": some months carry a stray non-UTF-8 byte in a
        # free-text column we don't read (2018-08 does).
        return io.TextIOWrapper(z.open(name), encoding="utf-8-sig",
                                errors="replace", newline="")
    return io.StringIO(raw.decode("utf-8-sig", errors="replace"), newline="")


def aggregate(stream):
    """-> ({(year, month, mkt, op, origin, dest): [flights, cancelled, diverted]},
    flight_rows). Rows flagged Duplicate='Y' are skipped."""
    reader = csv.reader(stream)
    header = [h.strip() for h in next(reader)]
    try:
        ix = {k: header.index(v) for k, v in COLS.items()}
    except ValueError as e:
        raise RuntimeError(f"on-time CSV header changed: {e}")
    agg, n = defaultdict(lambda: [0, 0, 0]), 0
    for row in reader:
        if len(row) <= ix["dup"] or row[ix["dup"]] == "Y":
            continue
        n += 1
        cell = agg[(row[ix["year"]], row[ix["month"]], row[ix["mkt"]],
                    row[ix["op"]], row[ix["origin"]], row[ix["dest"]])]
        cell[0] += 1
        cell[1] += row[ix["cancelled"]].startswith("1")
        cell[2] += row[ix["diverted"]].startswith("1")
    return agg, n


def load_one(conn, label: str, raw: bytes, note: str):
    conn.autocommit = True     # batch row survives a failed load
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO meta.ingest_batch
                       (target_table, source_file, data_scope, status, notes)
                       VALUES (%s, %s, 'domestic', 'started', %s) RETURNING batch_id""",
                    (TABLE, label, note))
        batch_id = cur.fetchone()[0]
    conn.autocommit = False
    try:
        agg, flight_rows = aggregate(open_csv(label, raw))
        if not agg:
            raise RuntimeError("no flight rows in file (month not published yet?)")
        periods = sorted({(k[0], k[1]) for k in agg})
        buf = io.StringIO()
        w = csv.writer(buf)
        for k, v in agg.items():
            w.writerow([*k, *v, batch_id, label])
        buf.seek(0)
        with conn.cursor() as cur:
            for y, m in periods:
                cur.execute(f"DELETE FROM {TABLE} WHERE year = %s AND month = %s", (y, m))
            cur.copy_expert(
                f"""COPY {TABLE} (year, month, mkt_unique_carrier, op_unique_carrier,
                    origin, dest, flights, cancelled, diverted, _batch_id, _source_file)
                    FROM STDIN WITH (FORMAT csv)""", buf)
            one = periods[0] if len(periods) == 1 else (None, None)
            cur.execute("""UPDATE meta.ingest_batch
                           SET status='loaded', row_count=%s, finished_at=now(),
                               reporting_year=%s, reporting_month=%s,
                               notes = notes || %s
                           WHERE batch_id=%s""",
                        (len(agg), one[0], one[1],
                         f"; {flight_rows} flights aggregated", batch_id))
        conn.commit()
        print(f"[ok] batch {batch_id}: {flight_rows:,} flights -> {len(agg):,} rows "
              f"({', '.join(f'{y}-{int(m):02d}' for y, m in periods)})", flush=True)
    except Exception as e:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("UPDATE meta.ingest_batch SET status='failed', finished_at=now(), "
                        "error_message=%s WHERE batch_id=%s", (str(e)[:2000], batch_id))
        conn.commit()
        print(f"[fail] batch {batch_id}: {e}", file=sys.stderr, flush=True)
        raise


def months(a: str, b: str):
    (y, m), (y1, m1) = (map(int, x.split("-")) for x in (a, b))
    while (y, m) <= (y1, m1):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--year"); p.add_argument("--month")
    p.add_argument("--from", dest="start", help="YYYY-MM (with --to): load a range")
    p.add_argument("--to", dest="end")
    p.add_argument("--file", help="local zip or CSV for one month (skips download)")
    p.add_argument("--pause", type=float, default=5, help="seconds between requests")
    p.add_argument("--skip-loaded", action="store_true",
                   help="with --from/--to: only fetch months not already in bronze")
    p.add_argument("--dsn")
    a = p.parse_args()
    opts = "-c client_encoding=UTF8"
    conn = psycopg2.connect(a.dsn, options=opts) if a.dsn else psycopg2.connect(options=opts)
    try:
        if a.file:
            load_one(conn, a.file.rsplit("/", 1)[-1], open(a.file, "rb").read(), "file")
            return
        if a.start and a.end:
            todo = list(months(a.start, a.end))
        elif a.year and a.month:
            todo = [(int(a.year), int(a.month))]
        else:
            p.error("give --year and --month, --from and --to, or --file")
        if a.skip_loaded:
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT year::int, month::int FROM {TABLE}")
                have = set(cur.fetchall())
            conn.rollback()
            todo = [ym for ym in todo if ym not in have]
            print(f"{len(todo)} month(s) to fetch", flush=True)
        failed = []
        for i, (y, m) in enumerate(todo):
            try:
                # TranStats has outages of several minutes where the form page
                # comes back unparseable; wait them out instead of burning
                # through the remaining months as failures.
                for attempt, wait in enumerate((30, 60, 120, 300, 600, None)):
                    try:
                        label, raw = fetch_from_form(TABLE_ID, year=str(y), month=str(m),
                                                     db_qs=DB_QS)
                        break
                    except Exception as e:
                        if wait is None:
                            raise
                        print(f"[wait] {y}-{m:02d} fetch failed ({e}); retry in {wait}s",
                              file=sys.stderr, flush=True)
                        time.sleep(wait)
                load_one(conn, label, raw, f"form year={y} month={m}")
            except Exception as e:      # keep going; a rerun of the month is safe
                failed.append(f"{y}-{m:02d}")
                print(f"[skip] {y}-{m:02d}: {e}", file=sys.stderr, flush=True)
            if i < len(todo) - 1:
                time.sleep(a.pause)
        if failed:
            sys.exit(f"failed months: {', '.join(failed)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
