"""Route Explorer - monthly load factors, seats and passengers on any route.

Run:  .venv/bin/streamlit run app/Route_Explorer.py   (from the repo root)
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from warehouse import carrier_color_map, month_window, q, roll12

st.set_page_config(page_title="T-100 Route Explorer", page_icon="✈️", layout="wide")

st.title("✈️ Route Explorer")
latest = q("SELECT MAX(flight_month) AS m FROM gold.route_carrier_month").m[0]
st.caption(
    "Nonstop routes touching the U.S. — BTS T-100, scheduled passenger service, "
    f"monthly, 1990 through {latest:%B %Y}."
)

# --- route pickers ----------------------------------------------------------
airports = q("""
    SELECT origin AS code, MAX(origin_city) AS city
    FROM gold.route_carrier_month
    GROUP BY origin ORDER BY SUM(seats) DESC""")
airport_label = dict(zip(airports.code, airports.code + " — " + airports.city.fillna("")))

mode = st.radio("View by", ["Carrier code", "Family (merger-adjusted)"],
                horizontal=True, key="view_mode",
                help="Family stitches merged carriers (NW→DL, CO→UA, US→AA, "
                     "VX/HA→AS, ...) into continuous lines; pre-merger values "
                     "are the pro-forma combined view.")
FAMILY = mode.startswith("Family")
KEY = "COALESCE(f.family, r.carrier)" if FAMILY else "r.carrier"
NAME = "COALESCE(f.family_name, r.carrier_name)" if FAMILY else "r.carrier_name"

c1, c2, c3 = st.columns([2, 2, 1])
origin_codes = airports.code.tolist()
origin = c1.selectbox("Origin", airports.code,
                      index=origin_codes.index("HNL") if "HNL" in origin_codes else 0,
                      format_func=lambda c: airport_label.get(c, c))
dests = q("""
    SELECT dest AS code, MAX(dest_city) AS city
    FROM gold.route_carrier_month
    WHERE origin = :o GROUP BY dest ORDER BY SUM(seats) DESC""", o=origin)
dest_label = dict(zip(dests.code, dests.code + " — " + dests.city.fillna("")))
dest_codes = dests.code.tolist()
dest = c2.selectbox("Destination", dests.code,
                    index=(dest_codes.index("LAX")
                           if origin == "HNL" and "LAX" in dest_codes else 0),
                    format_func=lambda c: dest_label.get(c, c))
both_dirs = c3.checkbox("Combine both directions", value=True,
                        help="Include the return leg; measures are summed and "
                             "load factor recomputed on the combined totals.")

df = q(f"""
    SELECT r.flight_month, {KEY} AS carrier, MAX({NAME}) AS carrier_name,
           SUM(r.seats)::float AS seats, SUM(r.passengers)::float AS passengers,
           MAX(r.distance_miles)::float AS distance_miles,
           r.origin, r.dest,
           MAX(r.origin_lat)::float AS o_lat, MAX(r.origin_lon)::float AS o_lon,
           MAX(r.dest_lat)::float AS d_lat, MAX(r.dest_lon)::float AS d_lon
    FROM gold.route_carrier_month r
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = r.carrier
    WHERE (r.origin = :o AND r.dest = :d) OR (:b AND r.origin = :d AND r.dest = :o)
    GROUP BY r.flight_month, {KEY}, r.origin, r.dest
    ORDER BY r.flight_month""", o=origin, d=dest, b=both_dirs)

if df.empty:
    st.info("No scheduled passenger service recorded on this route.")
    st.stop()

# --- filters ----------------------------------------------------------------
f1, f2, f3 = st.columns([3, 2, 1])
copt = (df.groupby(["carrier", "carrier_name"], as_index=False).seats.sum()
          .sort_values("seats", ascending=False))
carrier_codes = copt.carrier.tolist()
carrier_label = dict(zip(copt.carrier, copt.carrier_name + " (" + copt.carrier + ")"))

# Sticky carrier filter, keyed by CODE so it survives both route changes and
# the carrier<->family toggle (family survivors keep their code). The option
# list changes per route/mode, which would reset the widget; remember the
# choice in session_state and carry over the codes still present. "All
# selected" is remembered as no filter. First visit: preselect the majors.
if "carrier_filter" not in st.session_state:
    st.session_state.carrier_filter = ["AS", "DL", "UA", "AA", "WN", "HA"]
mem = st.session_state.get("carrier_filter")   # None means no filter
default = carrier_codes if mem is None else \
    ([c for c in carrier_codes if c in mem] or carrier_codes)
picked = f1.multiselect("Carriers", carrier_codes, default=default,
                        format_func=lambda c: carrier_label.get(c, c))
st.session_state.carrier_filter = None if set(picked) == set(carrier_codes) else picked

months = df.flight_month.drop_duplicates().sort_values().tolist()
w0, w1 = month_window(f2, months)
smooth = f3.checkbox("12-mo smoothing", value=False,
                     help="Rolling average to remove seasonality.")

sel = df[df.carrier.isin(picked) & df.flight_month.between(w0, w1)]
if sel.empty:
    st.info("Nothing matches the current filters.")
    st.stop()

# --- headline metrics -------------------------------------------------------
last12 = sel[sel.flight_month > sel.flight_month.max() - pd.DateOffset(months=12)]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Distance", f"{sel.distance_miles.max():,.0f} mi")
m2.metric("Carriers shown", f"{sel.carrier.nunique()}")
m3.metric("Passengers, last 12 mo shown", f"{last12.passengers.sum():,.0f}")
m4.metric("Load factor, last 12 mo shown",
          f"{100 * last12.passengers.sum() / max(last12.seats.sum(), 1):.1f}%")

# --- load factor by month ---------------------------------------------------
per_carrier = (sel.groupby(["flight_month", "carrier_name"], as_index=False)
                  [["seats", "passengers"]].sum())
per_carrier["load_factor"] = 100 * per_carrier.passengers / per_carrier.seats
combined = per_carrier.groupby("flight_month", as_index=False)[["seats", "passengers"]].sum()
combined["carrier_name"] = "All selected"
combined["load_factor"] = 100 * combined.passengers / combined.seats

lf = pd.concat([per_carrier, combined], ignore_index=True)
ycol = "load_factor"
if smooth:
    lf = roll12(lf, "carrier_name", "load_factor")
    ycol = "load_factor_12m"

route_title = f"{origin} ⇄ {dest}" if both_dirs else f"{origin} → {dest}"
colors = carrier_color_map(sel)
fig = px.line(lf, x="flight_month", y=ycol, color="carrier_name",
              color_discrete_map=colors,
              title=f"Load factor by month — {route_title}",
              labels={ycol: "load factor %", "flight_month": "", "carrier_name": ""})
fig.update_yaxes(rangemode="tozero")
for tr in fig.data:  # make the combined line stand out
    if tr.name == "All selected":
        tr.update(line=dict(color="black", width=3, dash="dot"))
st.plotly_chart(fig, use_container_width=True)

# --- seats vs passengers ----------------------------------------------------
vol = combined.melt(id_vars="flight_month", value_vars=["seats", "passengers"],
                    var_name="measure", value_name="count")
if smooth:
    vol = roll12(vol, "measure", "count")
fig2 = px.line(vol, x="flight_month", y="count_12m" if smooth else "count",
               color="measure", title=f"Capacity vs demand — {route_title} (all selected carriers)",
               labels={"count": "per month", "count_12m": "per month (12-mo avg)",
                       "flight_month": "", "measure": ""})
st.plotly_chart(fig2, use_container_width=True)

# --- share gap: who's big vs who's full --------------------------------------
st.subheader("Share gap — size vs load factor by airline")
st.caption(f"Aggregated over the selected window ({w0:%b %Y} – {w1:%b %Y}). "
           "Left: each airline's slice of the route (x, bubble size = seats) "
           "against how full it flies (y). Right: pick the gap metric — the "
           "two always agree in sign, but the share gap weights the load-"
           "factor gap by airline size.")

agg = (sel.groupby(["carrier", "carrier_name"], as_index=False)
          [["seats", "passengers"]].sum())
agg = agg[agg.seats > 0]
agg["seat_share"] = 100 * agg.seats / agg.seats.sum()
agg["pax_share"] = 100 * agg.passengers / agg.passengers.sum()
agg["load_factor"] = 100 * agg.passengers / agg.seats
route_lf = 100 * agg.passengers.sum() / agg.seats.sum()
agg["lf_gap_pp"] = agg.load_factor - route_lf
agg["share_gap_pp"] = agg.pax_share - agg.seat_share

# Two views of the same signal: share_gap = seat_share * lf_gap / route_lf,
# so they share a sign but the share gap shrinks for small carriers.
GAP_METRICS = {
    "Load factor − route average": (
        "lf_gap_pp",
        "How much fuller or emptier each airline flies than the route "
        "overall. Size-independent: a tiny carrier can post a big gap."),
    "Passenger share − seat share": (
        "share_gap_pp",
        "Whether each airline carries more of the route's passengers than "
        "its share of seats. The same signal weighted by size — gaps sum "
        "to zero across the route."),
}

g1, g2 = st.columns(2)

figb = px.scatter(
    agg, x="seat_share", y="load_factor", size="seats", color="carrier_name",
    color_discrete_map=colors,
    text="carrier", size_max=55,
    title="Load factor vs share of route seats",
    labels={"seat_share": "% of route seats", "load_factor": "load factor %",
            "carrier_name": ""},
    hover_data={"seats": ":,.0f", "passengers": ":,.0f",
                "pax_share": ":.1f", "carrier": False})
figb.update_traces(textposition="top center")
figb.add_hline(y=route_lf, line_dash="dash", line_color="gray",
               annotation_text=f"route avg {route_lf:.1f}%")
g1.plotly_chart(figb, use_container_width=True)

choice = g2.radio("Gap metric", list(GAP_METRICS), horizontal=True,
                  key="gap_metric")
gap_col, gap_blurb = GAP_METRICS[choice]
gap = agg.sort_values(gap_col)
figg = px.bar(
    gap, x=gap_col, y="carrier_name", orientation="h",
    color=(gap[gap_col] > 0).map({True: "fuller than route avg",
                                  False: "emptier than route avg"}),
    color_discrete_map={"fuller than route avg": "seagreen",
                        "emptier than route avg": "indianred"},
    title=f"{choice} (pp)",
    labels={gap_col: "percentage points", "carrier_name": "", "color": ""})
figg.update_layout(legend_title_text="", legend=dict(orientation="h", y=-0.2))
g2.plotly_chart(figg, use_container_width=True)
g2.caption(gap_blurb)

# --- the route on a map -----------------------------------------------------
ends = sel.dropna(subset=["o_lat", "d_lat"])
if not ends.empty:
    r = ends.iloc[0]
    figm = go.Figure(go.Scattergeo(
        lon=[r.o_lon, r.d_lon], lat=[r.o_lat, r.d_lat],
        mode="lines+markers+text", text=[r.origin, r.dest],
        textposition="top center", line=dict(width=2, color="firebrick"),
        marker=dict(size=6, color="firebrick")))
    figm.update_geos(projection_type="natural earth", resolution=50,
                     showcountries=True, countrycolor="rgb(180,180,180)",
                     showsubunits=True, subunitcolor="rgb(215,215,215)",
                     showlakes=True, lakecolor="rgb(220,235,245)",
                     coastlinecolor="rgb(160,160,160)",
                     landcolor="rgb(243,243,238)", fitbounds="locations")
    figm.update_layout(height=350, margin=dict(l=0, r=0, t=10, b=0), showlegend=False)
    st.plotly_chart(figm, use_container_width=True)
else:
    st.caption("No coordinates for one endpoint (small airport not in OpenFlights).")
