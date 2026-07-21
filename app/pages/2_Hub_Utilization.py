"""Hub Utilization - the notebook's hub charts with interactive controls."""
import pathlib
import sys

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from warehouse import month_window, q, roll12

st.set_page_config(page_title="T-100 Hub Utilization", page_icon="🛫", layout="wide")

st.title("🛫 Hub Utilization")
st.caption("How the major carriers use their hubs, 1990–present. "
           "Scheduled passenger service, departures-side counting.")

# --- mode + airline picker ---------------------------------------------------
mode = st.radio("View by", ["Family (merger-adjusted)", "Carrier code"],
                horizontal=True,
                help="Family stitches merged carriers (NW→DL, CO→UA, US→AA, "
                     "VX/HA→AS, ...) into continuous lines; pre-merger values "
                     "are the pro-forma combined view.")
FAMILY = mode.startswith("Family")
ACM = "gold.airport_family_month" if FAMILY else "gold.airport_carrier_month"
KEY = "family" if FAMILY else "carrier"
NAME = "family_name" if FAMILY else "carrier_name"
SHARE = "pct_of_family_system" if FAMILY else "pct_of_carrier_system"
CM = "gold.family_month" if FAMILY else "gold.carrier_month"

top = q(f"""
    SELECT {KEY} AS key, MAX({NAME}) AS name
    FROM {CM}
    WHERE flight_month BETWEEN '2025-01-01' AND '2025-12-01'
    GROUP BY {KEY} ORDER BY SUM(seats) DESC LIMIT 12""")
label = dict(zip(top.key, top.name + " (" + top.key + ")"))

c1, c2, c3, c4 = st.columns([2, 1, 2, 1])
airline = c1.selectbox("Airline", top.key, format_func=lambda k: label.get(k, k))
top_n = c2.slider("Top airports", 3, 10, 6)
all_months = q(f"SELECT DISTINCT flight_month FROM {CM} ORDER BY 1").flight_month.tolist()
w0, w1 = month_window(c3, all_months)
smooth = c4.checkbox("12-mo smoothing", value=True)

hubs = q(f"""
    SELECT airport FROM {ACM}
    WHERE {KEY} = :k AND flight_month BETWEEN '2025-01-01' AND '2025-12-01'
    GROUP BY airport ORDER BY SUM(seats) DESC LIMIT :n""",
    k=airline, n=top_n).airport.tolist()

df = q(f"""
    SELECT flight_month, airport,
           {SHARE}::float AS system_share,
           pct_of_airport_seats::float AS airport_share
    FROM {ACM}
    WHERE {KEY} = :k AND airport = ANY(:h) ORDER BY flight_month""",
    k=airline, h=hubs)

ys, ya = "system_share", "airport_share"
if smooth:
    df = roll12(df, "airport", "system_share")
    df = roll12(df, "airport", "airport_share")
    ys, ya = "system_share_12m", "airport_share_12m"
suffix = " (12-mo avg)" if smooth else ""
# Smooth over full history first, then window, so the first months shown
# still have a real 12-month average behind them.
df = df[df.flight_month.between(w0, w1)]

# --- hub weight --------------------------------------------------------------
fig = px.line(df, x="flight_month", y=ys, color="airport",
              category_orders={"airport": hubs},
              title=f"{label[airline]} — share of system seats at today's "
                    f"top {top_n} airports{suffix}",
              labels={ys: "% of system seats", "flight_month": "", "airport": ""})
st.plotly_chart(fig, use_container_width=True)

# --- dominance ---------------------------------------------------------------
fig2 = px.line(df, x="flight_month", y=ya, color="airport",
               category_orders={"airport": hubs},
               title=f"{label[airline]} — share of each airport's total seats"
                     f"{suffix} (fortress metric)",
               labels={ya: "% of airport seats", "flight_month": "", "airport": ""})
st.plotly_chart(fig2, use_container_width=True)

# --- spoke map ---------------------------------------------------------------
st.subheader("Spoke map")
s1, s2 = st.columns([1, 3])
hub = s1.selectbox("Hub", hubs)
months = q(f"""
    SELECT DISTINCT r.flight_month
    FROM gold.route_carrier_month r
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = r.carrier
    WHERE {'COALESCE(f.family, r.carrier)' if FAMILY else 'r.carrier'} = :k
      AND r.origin = :h
    ORDER BY 1""", k=airline, h=hub).flight_month.tolist()
months = [m for m in months if w0 <= m <= w1] or months
month = s2.select_slider("Month", months, value=months[-1],
                         format_func=lambda m: f"{m:%b %Y}")

routes = q(f"""
    SELECT r.dest, MAX(r.dest_city) AS dest_city,
           MAX(r.dest_lat)::float AS d_lat, MAX(r.dest_lon)::float AS d_lon,
           MAX(r.origin_lat)::float AS o_lat, MAX(r.origin_lon)::float AS o_lon,
           SUM(r.seats)::float AS seats
    FROM gold.route_carrier_month r
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = r.carrier
    WHERE {'COALESCE(f.family, r.carrier)' if FAMILY else 'r.carrier'} = :k
      AND r.origin = :h AND r.flight_month = :m AND r.dest_lat IS NOT NULL
    GROUP BY r.dest ORDER BY seats DESC""", k=airline, h=hub, m=month)

figm = go.Figure()
for _, r in routes.iterrows():
    figm.add_trace(go.Scattergeo(
        lon=[r.o_lon, r.d_lon], lat=[r.o_lat, r.d_lat], mode="lines",
        line=dict(width=max(0.4, r.seats / 40000), color="rgba(178,34,34,0.35)"),
        hoverinfo="skip", showlegend=False))
if not routes.empty:
    figm.add_trace(go.Scattergeo(
        lon=routes.d_lon, lat=routes.d_lat, mode="markers",
        marker=dict(size=4, color="rgb(120,20,20)"),
        text=routes.dest + " — " + routes.dest_city.fillna("")
             + "<br>seats: " + routes.seats.astype(int).astype(str),
        hoverinfo="text", showlegend=False))
figm.update_geos(projection_type="natural earth", resolution=50,
                 showcountries=True, countrycolor="rgb(180,180,180)",
                 showsubunits=True, subunitcolor="rgb(215,215,215)",
                 showlakes=True, lakecolor="rgb(220,235,245)",
                 coastlinecolor="rgb(160,160,160)",
                 landcolor="rgb(243,243,238)")
figm.update_layout(height=550, margin=dict(l=0, r=0, t=30, b=0),
                   title=f"{label[airline]} nonstops from {hub}, {month:%B %Y} "
                         f"({len(routes)} destinations; width ~ seats)")
st.plotly_chart(figm, use_container_width=True)
