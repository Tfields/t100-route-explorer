"""Export gold + the one silver dim table the app needs, to zstd Parquet for
the hosted (DuckDB) app.

The public Streamlit app queries gold plus silver.dim_carrier_family (the
14-row merger map joined on every family-mode query) — nothing else. Run
after `SELECT gold.refresh_all();`, then upload export/out/*.parquet to the
GitHub Release the app downloads from.

Uses DuckDB's postgres extension with postgres_query() (matviews aren't
visible to the plain postgres_scanner table listing). Connection comes from
the same PG* env vars the loader uses, with the local-Docker defaults.
"""
import os
import pathlib
import sys
import time

import duckdb

OUT = pathlib.Path(__file__).resolve().parent / "out"

# "schema.table" -> ORDER BY. Sorting clusters repeated codes/months so
# Parquet dictionary+zstd encoding compresses far better than matview order.
# Output file is named after the table alone (warehouse.py's DuckDB backend
# maps schema.table -> export/out/table.parquet regardless of source schema).
TABLES = {
    "gold.carrier_month":         "carrier, flight_month",
    "gold.family_month":          "family, flight_month",
    "gold.airport_carrier_month": "carrier, airport, flight_month",
    "gold.airport_family_month":  "family, airport, flight_month",
    "gold.route_carrier_month":   "carrier, origin, dest, flight_month",
    "silver.dim_carrier_family":  "family, carrier",
}


def pg_dsn() -> str:
    e = os.environ.get
    return (f"host={e('PGHOST', 'localhost')} port={e('PGPORT', '5432')} "
            f"user={e('PGUSER', 't100')} password={e('PGPASSWORD', 't100')} "
            f"dbname={e('PGDATABASE', 't100')}")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{pg_dsn()}' AS pg (TYPE postgres, READ_ONLY)")

    total = 0
    for table, order in TABLES.items():
        name = table.split(".", 1)[1]
        dest = OUT / f"{name}.parquet"
        t0 = time.time()
        con.execute(f"""
            COPY (SELECT * FROM postgres_query('pg',
                    'SELECT * FROM {table} ORDER BY {order}'))
            TO '{dest}' (FORMAT parquet, COMPRESSION zstd)
        """)
        rows = con.execute(
            f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        size = dest.stat().st_size
        total += size
        print(f"{name:24s} {rows:>10,} rows  {size / 1e6:8.1f} MB  "
              f"({time.time() - t0:.0f}s)")
    print(f"{'TOTAL':24s} {'':>10s}       {total / 1e6:8.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
