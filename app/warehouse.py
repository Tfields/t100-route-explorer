"""Shared data access for the Streamlit app (the notebook's q(), plus caching).

Two backends, picked by $T100_BACKEND:
- "postgres" (default): connects to $T100_DSN, same as always.
- "duckdb": no live database. Registers gold.* + silver.dim_carrier_family
  as DuckDB views over static Parquet files (see export/export_gold.py),
  downloading them from $T100_PARQUET_URL on first use if not already on
  disk. This is the free-hosting path — Streamlit Community Cloud has no
  database of its own, so the "warehouse" ships as files instead.
Either way `q()` is unchanged: page SQL doesn't know which backend it's on.
"""
import os
import pathlib
import urllib.request

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

BACKEND = os.environ.get("T100_BACKEND", "postgres")
DSN = os.environ.get("T100_DSN", "postgresql+psycopg2://t100:t100@localhost:5432/t100")

# schema -> tables exported as Parquet by export/export_gold.py. This is the
# app's full data surface (grep-verified against app/*.py, app/pages/*.py).
DUCKDB_TABLES = {
    "gold": ["carrier_month", "family_month", "airport_carrier_month",
             "airport_family_month", "route_carrier_month"],
    "silver": ["dim_carrier_family"],
}
PARQUET_URL = os.environ.get("T100_PARQUET_URL")  # e.g. a GitHub Release base URL
PARQUET_DIR = pathlib.Path(os.environ.get(
    "T100_PARQUET_DIR",
    pathlib.Path(__file__).resolve().parent.parent / "export" / "out"))


def _local_parquet(name: str) -> pathlib.Path:
    """Path to a table's Parquet file, fetching it from $T100_PARQUET_URL
    into PARQUET_DIR on first use if it isn't already there."""
    path = PARQUET_DIR / f"{name}.parquet"
    if not path.exists():
        if not PARQUET_URL:
            raise FileNotFoundError(
                f"{path} not found and $T100_PARQUET_URL isn't set to fetch it")
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(f"{PARQUET_URL.rstrip('/')}/{name}.parquet", path)
    return path


@st.cache_resource
def engine():
    if BACKEND != "duckdb":
        return create_engine(DSN, pool_pre_ping=True)

    # StaticPool pins the engine to one physical connection for its whole
    # lifetime. Without it, SQLAlchemy's default pool can hand back a
    # different duckdb connection per .connect() call, and each fresh
    # connection to ':memory:' is its own empty database — the views built
    # below would silently vanish for later queries.
    eng = create_engine("duckdb:///:memory:", poolclass=StaticPool)
    with eng.connect() as c:
        for schema, tables in DUCKDB_TABLES.items():
            c.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            for name in tables:
                path = _local_parquet(name)
                c.execute(text(
                    f"CREATE VIEW {schema}.{name} AS "
                    f"SELECT * FROM read_parquet('{path.as_posix()}')"))
        c.commit()
    return eng


@st.cache_data(ttl=3600, show_spinner=False)
def q(sql: str, **params) -> pd.DataFrame:
    """Query the warehouse into a DataFrame. Results cached for an hour
    (the data only changes on monthly loads)."""
    with engine().connect() as c:
        df = pd.read_sql(text(sql), c, params=params)
    # Postgres DATE arrives as python date objects; charts and .dt need datetimes.
    if "flight_month" in df.columns:
        df["flight_month"] = pd.to_datetime(df["flight_month"])
    return df


# Brand-ish colors for the recognizable carriers; anything unlisted falls
# back to plotly's default palette. Family codes reuse the survivor's color.
CARRIER_COLORS = {
    "UA": "#005DAA",  # United blue
    "DL": "#C8102E",  # Delta red
    "AA": "#9DA6AB",  # American silver/grey
    "AS": "#4FB548",  # Alaska green
    "HA": "#7B4BA8",  # Hawaiian pualani purple
    "WN": "#F9B612",  # Southwest yellow
    "B6": "#003876",  # JetBlue navy
    "NK": "#FFE300",  # Spirit yellow
    "F9": "#046A38",  # Frontier green
    "G4": "#F78D2D",  # Allegiant orange
    "OO": "#5B8DB8",  # SkyWest steel blue
    "QX": "#9CCB3B",  # Horizon light green
    "VX": "#D5006D",  # Virgin America magenta
    "CO": "#B08D57",  # Continental gold
    "CS": "#5C7A29",  # Continental Micronesia olive (CVD-checked next to UA/CO)
    "NW": "#8B1A1A",  # Northwest dark red
    "US": "#1B3E6F",  # US Airways dark blue
    "HP": "#00857D",  # America West teal
    "TW": "#A0522D",  # TWA
    "FL": "#71C5E8",  # AirTran light blue
    "SY": "#F26522",  # Sun Country orange
}


def carrier_color_map(df, code_col="carrier", name_col="carrier_name"):
    """color_discrete_map for charts that color by carrier NAME, built from
    the df's code->name pairs so it works in both carrier and family mode."""
    pairs = df[[code_col, name_col]].drop_duplicates()
    return {name: CARRIER_COLORS[code]
            for code, name in pairs.itertuples(index=False)
            if code in CARRIER_COLORS}


def month_window(container, months, default_start="2022-01-01"):
    """Range select_slider over the given months, defaulting to Jan 2022
    (or the first available month) through the latest.

    format_func goes through pd.Timestamp() so a non-scalar raises TypeError,
    which AppTest's range-serialization fallback expects."""
    if len(months) < 2:
        return months[0], months[0]
    start = pd.Timestamp(default_start)
    lo = next((m for m in months if m >= start), months[0])
    return container.select_slider(
        "Months", months, value=(lo, months[-1]),
        format_func=lambda m: pd.Timestamp(m).strftime("%b %Y"))


def roll12(df: pd.DataFrame, by: str, col: str) -> pd.DataFrame:
    """12-month rolling mean of col within each `by` group (de-seasonalize)."""
    df = df.sort_values("flight_month").copy()
    df[col + "_12m"] = df.groupby(by)[col].transform(lambda s: s.rolling(12).mean())
    return df
