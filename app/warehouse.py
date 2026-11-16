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
    "silver": ["dim_carrier_family", "carrier_partner_alloc"],
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


# --- family attribution on route-level queries ---------------------------------
# gold.route_carrier_month is keyed by OPERATING carrier. In Family view a
# route row belongs to: the marketing-carrier allocation where one exists
# (multi-partner regionals such as SkyWest, 2018+; one row per family with a
# share), else the authored family map, else the carrier itself. Queries over
# `gold.route_carrier_month r` add FAMILY_JOIN, key on FAMILY_KEY and multiply
# every additive measure by FAMILY_W.
FAMILY_JOIN = """
    LEFT JOIN silver.carrier_partner_alloc a
           ON a.flight_month = r.flight_month AND a.carrier = r.carrier
          AND a.origin = r.origin AND a.dest = r.dest
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = r.carrier"""
FAMILY_KEY = "COALESCE(a.family, f.family, r.carrier)"
FAMILY_NAME = "COALESCE(a.family_name, f.family_name, r.carrier_name)"
FAMILY_W = "COALESCE(a.share, 1)"


# --- "About the data" ---------------------------------------------------------
REPO_URL = "https://github.com/Tfields/t100-route-explorer"


def about_data():
    """Body of the 'About the data' page (pages/5_About_the_Data.py). The
    family lists are read from dim_carrier_family so they can't drift from
    what the charts actually use."""
    latest = q("SELECT MAX(flight_month) AS m FROM gold.carrier_month").m[0]
    dim = q("""SELECT carrier, family, family_name, merged_month, note
               FROM silver.dim_carrier_family ORDER BY family, carrier""")
    # "TWA, acquired by..." / "regional: Envoy Air (American Eagle), owned..."
    # -> "TWA (TW)" / "Envoy Air (MQ)"
    def member(r):
        name = re.sub(r"^regional:\s*", "", r.note)
        name = re.split(r"[,;]", re.sub(r"\s*\([^)]*\)", "", name))[0].strip()
        return f"{name} ({r.carrier})"
    fam_lines = []
    for fam, g in dim.groupby("family", sort=False):
        merged = g[(g.carrier != fam) & ~g.note.str.startswith("regional")]
        regional = g[g.note.str.startswith("regional")]
        line = f"- **{g.family_name.iloc[0].replace(' family', '')} ({fam})**"
        if len(merged):
            line += " — merged carriers: " + ", ".join(member(r) for r in merged.itertuples())
        if len(regional):
            line += ("; " if len(merged) else " — ") + "regional partners: " \
                    + ", ".join(member(r) for r in regional.itertuples())
        fam_lines.append(line)
    # Live figures for the estimate note: which regionals are split, and how
    # much of each family's recent seats rest on an estimated split.
    split = q("""
        SELECT c.carrier, MAX(c.carrier_name) AS carrier_name
        FROM gold.carrier_month c
        WHERE c.carrier IN (SELECT DISTINCT carrier FROM silver.carrier_partner_alloc)
          AND c.flight_month >= '2018-01-01'
        GROUP BY c.carrier ORDER BY SUM(c.seats) DESC""")
    split_names = ", ".join(split.carrier_name.str.replace(
        r"\s+(d/b/a|dba)\s.*$", "", regex=True).map(clean_name).head(6))
    est = q("""
        SELECT family, MAX(family_name) AS family_name,
               100.0 * SUM(seats_estimated) / SUM(seats) AS pct
        FROM gold.family_month
        WHERE flight_month > :cut GROUP BY family
        HAVING SUM(seats_estimated) > 0 ORDER BY SUM(seats) DESC""",
        cut=latest - pd.DateOffset(months=12))
    est_text = ", ".join(f"{r.family_name.replace(' family', '')} {r.pct:.1f}%"
                         for r in est.itertuples())
    region_lines = [
        f"- **{name}**: " + ", ".join(d["states"]) for name, d in REGIONS.items()
        if "countries" not in d]
    st.markdown(f"""
**Source.** U.S. Bureau of Transportation Statistics **T-100 segment data**:
the monthly traffic reports airlines file with the U.S. DOT. Every domestic
flight by U.S. carriers and every international flight touching the U.S. by
any carrier, January 1990 through **{latest:%B %Y}**. BTS publishes three to
four months behind. Airport coordinates are from OpenFlights. This is an
independent project, not affiliated with BTS or any airline.
[Source code and pipeline]({REPO_URL}).

**What is counted**
- **Nonstop segments, not passenger journeys.** A passenger connecting
  through a hub is counted once on each flight. Nothing here says where a
  trip began or ended, so a drop on one route can be travelers switching to
  a different routing.
- **Scheduled passenger service only.** Charters and all-cargo flights are
  excluded. Seats and departures are flights actually performed.
- **Load factor** is passengers divided by seats on the same flights. It is
  not the revenue-passenger-mile load factor airlines publish, though the
  two track closely on a single route.
- **Airport figures are departures.** Arrivals mirror them.
- **One-off flights are hidden by default** on Route Explorer: an airline
  with two or fewer departures on a route in a month (diversions, extra
  sections) is not shown as serving it unless you tick "Show one-off flights".

**Airlines are the operating carrier.** T-100 is filed by the airline that
flew the aircraft, not the one that sold the ticket. In "Carrier code" view a
SkyWest flight sold as United Express appears under SkyWest.

**Family view** stitches airlines into continuous histories:
{chr(10).join(fam_lines)}

- Before a merger, a family's numbers are the then-separate airlines added
  together, not what one airline actually flew.
- The regional partners listed above flew for one family throughout their
  T-100 history, so all of their traffic counts toward it.
- Regionals that fly for several majors ({split_names}) are attributed to
  the major that sold each flight, from January 2018. See the note below.\*
- Two steps in the family lines are reporting changes, not growth: October
  2002, when most regional airlines began filing T-100, and January 2018,
  when the multi-partner regionals start being attributed. Before 2018 those
  regionals stay under their own codes, so a major's share of a hub is
  understated for those years wherever they operated.

**\* How multi-partner regionals are split, and where it is an estimate.**
T-100 does not record which major a regional's flight was for. A second BTS
dataset, Marketing Carrier On-Time Performance, lists every domestic flight
since January 2018 with both the airline that sold it and the airline that
flew it. For each regional, route and month we count its flights for each
major and divide the T-100 seats, passengers and departures in those
proportions.
- **Exact** where a regional flew a route for only one major that month,
  which is most of its flying.
- **Estimated** where it flew the same route for two or more majors in the
  same month. The split by number of flights assumes each major's flights
  used the same size of aircraft and were equally full. Neither is
  guaranteed: the same aircraft type is fitted with different seat counts
  for different majors, and one major's flights can fill better than
  another's. Treat a family's seats and load factor on such routes as
  approximate.
- **Flights outside the on-time data** (it is domestic only, so regional
  flights to Canada or Mexico, plus a few unmatched routes) take the
  regional's mix of majors at the U.S. airport that month.
- Over the latest 12 months, the estimated portion of each family's seats is:
  {est_text or "none"}. Everything else is reported or exactly attributed.

**Regions** on Route Explorer are groupings made for this app, not an
official standard. Foreign regions are groups of countries; U.S. regions are
groups of states:
{chr(10).join(region_lines)}
""")


# --- airport regions ----------------------------------------------------------
# AUTHORED groups, so a "route" can be region-to-region (West Coast <-> Hawaii,
# Northeast <-> Europe). US regions are groups of states: the four Census
# regions as a base, split the way airline network people talk (Hawaii,
# Alaska, Texas, Florida alone; coast vs mountain; a Mid-Atlantic). The state
# comes from the BTS city name ("Honolulu, HI"). Foreign regions are groups of
# countries, matched on the country column; historical names (West Germany,
# U.S.S.R.) are mapped too. Every state and every country in the data is in
# exactly one region.
REGIONS = {
    "West Coast": {"states": ["CA", "OR", "WA"]},
    "Hawaii": {"states": ["HI"]},
    "Alaska": {"states": ["AK"]},
    "Mountain West": {"states": ["AZ", "CO", "ID", "MT", "NM", "NV", "UT", "WY"]},
    "Texas": {"states": ["TX"]},
    "Midwest": {"states": ["IA", "IL", "IN", "KS", "MI", "MN", "MO", "ND", "NE",
                           "OH", "SD", "WI"]},
    "Southeast": {"states": ["AL", "AR", "GA", "KY", "LA", "MS", "NC", "OK", "SC",
                             "TN"]},
    "Florida": {"states": ["FL"]},
    "Mid-Atlantic": {"states": ["DC", "DE", "MD", "VA", "WV"]},
    "Northeast": {"states": ["CT", "MA", "ME", "NH", "NJ", "NY", "PA", "RI", "VT"]},
    "Pacific territories": {
        "states": ["TT"],   # BTS code for Guam, N. Marianas, American Samoa
        "countries": ["Johnston Atoll", "Midway Islands", "Wake Island"]},
    "Canada": {"countries": ["Canada"]},
    "Mexico": {"countries": ["Mexico"]},
    "Central America": {"countries": [
        "Belize", "Costa Rica", "El Salvador", "Guatemala", "Honduras",
        "Nicaragua", "Panama"]},
    "Caribbean": {
        "states": ["PR", "VI"],   # Puerto Rico and the US Virgin Islands
        "countries": [
            "Anguilla", "Antigua and Barbuda", "Aruba", "Barbados", "Bermuda",
            "Bonaire, Sint Eustatius, and Saba", "British Virgin Islands",
            "Cayman Islands", "Cuba", "Curacao", "Dominica",
            "Dominican Republic", "Grenada", "Guadeloupe", "Haiti", "Jamaica",
            "Martinique", "Netherlands Antilles", "Saint Barthelemy",
            "Saint Kitts and Nevis", "Saint Lucia",
            "Saint Vincent and the Grenadines", "Sint Maarten", "The Bahamas",
            "Trinidad and Tobago", "Turks and Caicos Islands"]},
    "South America": {"countries": [
        "Argentina", "Bolivia", "Brazil", "Chile", "Colombia", "Ecuador",
        "French Guiana", "Guyana", "Paraguay", "Peru", "Suriname", "Uruguay",
        "Venezuela"]},
    "Europe": {"countries": [
        "Austria", "Belgium", "Berlin", "Bulgaria", "Croatia", "Cyprus",
        "Czech Republic", "Czechoslovakia", "Denmark", "Finland", "France",
        "Germany", "Greece", "Greenland", "Hungary", "Iceland", "Ireland",
        "Italy", "Latvia", "Luxembourg", "Malta", "Netherlands", "Norway",
        "Poland", "Portugal", "Romania", "Russia", "Serbia",
        "Serbia and Montenegro", "Spain", "Sweden", "Switzerland", "Turkey",
        "U.S.S.R.", "Ukraine", "United Kingdom", "West Germany", "Yugoslavia"]},
    "Middle East": {"countries": [
        "Azerbaijan", "Bahrain", "Iran", "Iraq", "Israel", "Jordan", "Kuwait",
        "Qatar", "Saudi Arabia", "Syria", "United Arab Emirates",
        "Uzbekistan"]},   # Azerbaijan/Uzbekistan: no better home (Thomas)
    "Africa": {"countries": [
        "Cape Verde", "Cote d'Ivoire", "Egypt", "Ethiopia", "Gabon", "Ghana",
        "Guinea", "Kenya", "Liberia", "Morocco", "Mozambique", "Namibia",
        "Nigeria", "Saint Helena, Ascension, and Tristan da Cunha", "Senegal",
        "Somalia", "South Africa", "Sudan", "The Gambia", "Togo", "Tunisia",
        "Zambia"]},
    "South Asia": {"countries": ["India", "Pakistan"]},
    "East Asia": {"countries": [
        "China", "Hong Kong", "Japan", "Macau", "South Korea", "Taiwan"]},
    "Southeast Asia": {"countries": [
        "Indonesia", "Malaysia", "Philippines", "Singapore", "Thailand"]},
    "Oceania": {"countries": [
        "Australia", "Cook Islands", "Federated States of Micronesia", "Fiji",
        "French Polynesia", "Kiribati", "Marshall Islands", "Nauru",
        "New Caledonia", "New Zealand", "Niue", "Palau", "Papua New Guinea",
        "Samoa", "Tonga"]},
}
REGION_PREFIX = "region:"
_STATE_REGION = {s: name for name, d in REGIONS.items() for s in d.get("states", [])}
_COUNTRY_REGION = {c: name for name, d in REGIONS.items() for c in d.get("countries", [])}


def is_region(place) -> bool:
    return isinstance(place, str) and place.startswith(REGION_PREFIX)


def place_name(place) -> str:
    """Display name: the region's name, or the airport code unchanged."""
    return place[len(REGION_PREFIX):] if is_region(place) else place


def region_label(name: str) -> str:
    """Picker label; US regions list their states, foreign ones just the name."""
    d = REGIONS[name]
    if "countries" in d:
        return f"Region — {name}"
    return f"Region — {name} ({', '.join(d['states'])})"


def region_of(city, country):
    """Region key for an airport from its BTS city name ('Honolulu, HI') and
    country, or None if it is in no region. State wins over country, since
    Puerto Rico / Guam rows carry their own 'country'."""
    name = None
    if isinstance(city, str) and ", " in city:
        name = _STATE_REGION.get(city.rsplit(", ", 1)[1])
    name = name or _COUNTRY_REGION.get(country)
    return REGION_PREFIX + name if name else None


def place_sql(side: str, place, param: str) -> str:
    """WHERE fragment matching `place` (airport code or region key) on the
    'origin' or 'dest' side of gold.route_carrier_month aliased r. Bind the
    values with place_params(place, param)."""
    if not is_region(place):
        return f"r.{side} = :{param}"
    d, parts = REGIONS[place_name(place)], []
    if d.get("states"):
        parts.append(f"split_part(r.{side}_city, ', ', 2) = ANY(:{param}_s)")
    if d.get("countries"):
        parts.append(f"r.{side}_country = ANY(:{param}_c)")
    return "(" + " OR ".join(parts) + ")"


def place_params(place, param: str) -> dict:
    if not is_region(place):
        return {param: place}
    d = REGIONS[place_name(place)]
    return {f"{param}_{k[0]}": d[k] for k in ("states", "countries") if d.get(k)}


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
    # Single-partner regionals folded into families (Carrier Families bands).
    "MQ": "#D3D9DD",      # Envoy / American Eagle - light silver
    "OH": "#7FA9D9",      # PSA - light blue
    "PT": "#5FB7B0",      # Piedmont - light teal
    "OW": "#D9A77E",      # Executive - tan
    "9E": "#F59E42",      # Endeavor / Pinnacle - orange
    "OH (1)": "#B07CC6",  # Comair - mauve
    "XJ": "#E3C567",      # Mesaba - sand
    "C5": "#7FD1E8",      # CommuteAir - light cyan
    "XE (1)": "#9C8ADE",  # ExpressJet - violet
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
