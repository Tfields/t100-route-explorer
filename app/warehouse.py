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
import re
import threading
import urllib.request

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

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
        # Download to a temp name and rename, so an interrupted download
        # (e.g. a redeploy mid-fetch) never leaves a truncated file behind.
        tmp = path.with_suffix(".parquet.part")
        urllib.request.urlretrieve(f"{PARQUET_URL.rstrip('/')}/{name}.parquet", tmp)
        tmp.replace(path)
    return path


@st.cache_resource
def engine():
    if BACKEND != "duckdb":
        return create_engine(DSN, pool_pre_ping=True)

    # The views live in a small DuckDB FILE next to the Parquet, not in
    # ':memory:'. Every fresh connection to ':memory:' is its own empty
    # database, so the old single pinned connection was a single point of
    # failure: once it was replaced (after an error, or by a different
    # library version's pooling) every query failed with "table does not
    # exist" until the app was rebooted. With a file, any connection sees the
    # views, so each query simply opens its own (NullPool) and closes it.
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"duckdb:///{(PARQUET_DIR / 't100_views.duckdb').as_posix()}",
                        poolclass=NullPool)
    with eng.connect() as c:
        for schema, tables in DUCKDB_TABLES.items():
            c.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            for name in tables:
                path = _local_parquet(name)
                c.execute(text(
                    f"CREATE OR REPLACE VIEW {schema}.{name} AS "
                    f"SELECT * FROM read_parquet('{path.as_posix()}')"))
        c.commit()
    return eng


# One query at a time. Streamlit runs each rerun/session in its own thread and
# a slider drag fires overlapping reruns; sharing a duckdb connection across
# threads corrupted it (TransactionException, segfaults - reproduced with 6
# threads). Each query now has its own connection, and the lock stays as
# cheap insurance: queries are short and cached.
_QUERY_LOCK = threading.Lock()


@st.cache_data(ttl=3600, show_spinner=False)
def q(sql: str, **params) -> pd.DataFrame:
    """Query the warehouse into a DataFrame. Results cached for an hour
    (the data only changes on monthly loads)."""
    with _QUERY_LOCK:
        with engine().connect() as c:
            df = pd.read_sql(text(sql), c, params=params)
    # Postgres DATE arrives as python date objects; charts and .dt need datetimes.
    if "flight_month" in df.columns:
        df["flight_month"] = pd.to_datetime(df["flight_month"])
    if "carrier_name" in df.columns:
        df["carrier_name"] = df["carrier_name"].map(clean_name)
    return df


# BTS carrier names carry legal suffixes ("Southwest Airlines Co.", "Korean
# Air Lines Co. Ltd."); strip them so legends and pickers read cleanly.
_SUFFIX = re.compile(r"(,?\s+(Inc|Co|Corp|Corporation|LLC|Ltd|Limited|Plc|LP)\.?)+$")


def clean_name(name):
    return _SUFFIX.sub("", name) if isinstance(name, str) else name


# --- dark-surface chart styling (theme pinned in .streamlit/config.toml) -----
TOTAL_LINE = "#D9DCE1"   # the "All selected" / combined line
# Categorical colors for series with no brand identity (hub airports), in
# fixed order. First 8 are a validated dark-mode set; 9-10 are neutral greys
# that lean on the direct end-of-line labels.
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181",
                 "#0ca30c", "#9085e9", "#e66767", "#9a9a94", "#d0cfc6"]
MAP_LINE = "rgba(255,138,101,0.40)"
MAP_DOT = "#FF8A65"


def month_hover(fig, yfmt=".1f", suffix=""):
    """One shared hover box per month: 'Mar 2025' header, one formatted value
    per series, so lines can be compared at a glance."""
    fig.update_traces(hovertemplate="%{y:" + yfmt + "}" + suffix)
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(hoverformat="%b %Y")
    return fig


def label_line_ends(fig):
    """Direct label at each line's last point (neutral text, not the series
    color) so identity never rides on color alone. Labels toggle with their
    line via legendgroup."""
    import plotly.graph_objects as go
    for tr in list(fig.data):
        pts = [(x, y) for x, y in zip(tr.x, tr.y) if y is not None and y == y]
        if not pts:
            continue
        x, y = pts[-1]
        fig.add_trace(go.Scatter(
            x=[x], y=[y], mode="text", text=[f" {tr.name}"],
            textposition="middle right", textfont=dict(size=12, color=TOTAL_LINE),
            cliponaxis=False, hoverinfo="skip", showlegend=False,
            legendgroup=tr.legendgroup))
    fig.update_layout(margin=dict(r=60))
    return fig


def style_map(fig, fit=False):
    """Dark map styling. Default view is North America - the lower 48 centered
    with Alaska, Hawaii, the Caribbean and Central America in frame - rather
    than the whole globe; longer routes run off the edge (drag/scroll to
    follow them). fit=True zooms to the plotted points instead."""
    fig.update_geos(projection_type="natural earth", resolution=50,
                    bgcolor="rgba(0,0,0,0)", showframe=False,
                    showland=True, landcolor="#232733",
                    showocean=True, oceancolor="#0E1117",
                    showlakes=True, lakecolor="#0E1117",
                    showcountries=True, countrycolor="#5A6170",
                    showsubunits=True, subunitcolor="#3A404D",
                    coastlinecolor="#6B7280")
    if fit:
        fig.update_geos(fitbounds="locations")
    else:
        fig.update_geos(lonaxis_range=[-170, -55], lataxis_range=[7, 72])
    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)")
    return fig


# Brand-ish colors for the recognizable carriers; anything unlisted falls
# back to plotly's default palette. Family codes reuse the survivor's color.
CARRIER_COLORS = {
    "UA": "#2F7FD6",  # United blue (lightened for the dark surface)
    "DL": "#C8102E",  # Delta red
    "AA": "#9DA6AB",  # American silver/grey
    "AS": "#4FB548",  # Alaska green
    "HA": "#9463C4",  # Hawaiian pualani purple (lightened)
    "WN": "#F9B612",  # Southwest yellow
    "B6": "#6F8FF5",  # JetBlue blue (navy is invisible on dark)
    "NK": "#FFE300",  # Spirit yellow
    "F9": "#14A06A",  # Frontier green (lightened)
    "G4": "#F78D2D",  # Allegiant orange
    "OO": "#5B8DB8",  # SkyWest steel blue
    "QX": "#9CCB3B",  # Horizon light green
    "VX": "#D5006D",  # Virgin America magenta
    "CO": "#B08D57",  # Continental gold
    "CS": "#5C7A29",  # Continental Micronesia olive (CVD-checked next to UA/CO)
    "NW": "#E08A80",  # Northwest: light red, clearly apart from DL
    "US": "#4A7CC0",  # US Airways blue (lightened)
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


def month_gaps(df: pd.DataFrame, by=None) -> pd.DataFrame:
    """Insert a blank (NaN) row for every month missing inside each `by`
    group's first..last span, so plotly breaks the line there instead of
    drawing a straight segment across months with no service. Expects one
    row per group per month."""
    if df.empty:
        return df
    parts = []
    for key, g in (df.groupby(by, sort=False) if by else [(None, df)]):
        full = pd.date_range(g.flight_month.min(), g.flight_month.max(), freq="MS")
        if len(full) != len(g):
            g = (g.set_index("flight_month").reindex(full)
                  .rename_axis("flight_month").reset_index())
            if by:
                g[by] = key
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def roll12(df: pd.DataFrame, by: str, col: str) -> pd.DataFrame:
    """12-month rolling mean of col within each `by` group (de-seasonalize)."""
    df = df.sort_values("flight_month").copy()
    df[col + "_12m"] = df.groupby(by)[col].transform(lambda s: s.rolling(12).mean())
    return df
