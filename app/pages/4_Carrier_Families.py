"""Carrier Families - merger histories and route churn for the five families."""
import pathlib
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from warehouse import CARRIER_COLORS, month_window, q, roll12

st.set_page_config(page_title="T-100 Carrier Families", page_icon="🧬", layout="wide")

st.title("🧬 Carrier Families")
st.caption("How the five merged airline families came together, and how their "
           "networks change today. Scheduled passenger service, 1990–present; "
           "routes are undirected city pairs.")

# --- family picker -----------------------------------------------------------
fams = q("""
    SELECT f.family,
           MAX(f.family_name) AS family_name,
           STRING_AGG(f.carrier, ' + '
                      ORDER BY f.merged_month NULLS FIRST, f.carrier)
               AS members
    FROM silver.dim_carrier_family f
    GROUP BY f.family
    ORDER BY (SELECT SUM(m.seats) FROM gold.family_month m
              WHERE m.family = f.family
                AND m.flight_month >= (SELECT MAX(flight_month)
                                       - INTERVAL '11 months'
                                       FROM gold.family_month)) DESC""")
fam_label = dict(zip(fams.family, fams.family_name + " (" + fams.members + ")"))

c1, c2, c3 = st.columns([2, 2, 1])
fam = c1.selectbox("Family", fams.family, format_func=lambda f: fam_label[f])

mem = q("""
    SELECT carrier, merged_month, note FROM silver.dim_carrier_family
    WHERE family = :f ORDER BY merged_month NULLS FIRST, carrier""", f=fam)
member_order = mem.carrier.tolist()
fam_colors = {c: CARRIER_COLORS[c] for c in member_order if c in CARRIER_COLORS}

months = q("""SELECT DISTINCT flight_month FROM gold.family_month
              WHERE family = :f ORDER BY 1""", f=fam).flight_month.tolist()
# Default to the full history: the point of this page is the long merger arc.
w0, w1 = month_window(c2, months, default_start="1990-01-01")
smooth = c3.checkbox("12-mo smoothing", value=True)

fm = q("""
    SELECT flight_month, seats::float AS seats, passengers::float AS passengers,
           routes_served, airports_served
    FROM gold.family_month WHERE family = :f ORDER BY flight_month""", f=fam)
cm = q("""
    SELECT c.flight_month, c.carrier, c.carrier_name, c.seats::float AS seats,
           c.passengers::float AS passengers, c.routes_served
    FROM gold.carrier_month c
    JOIN silver.dim_carrier_family f ON f.carrier = c.carrier
    WHERE f.family = :f ORDER BY c.flight_month""", f=fam)

latest = fm.flight_month.max()
active = cm[cm.flight_month == latest].carrier.tolist()
last12 = fm[fm.flight_month > latest - pd.DateOffset(months=12)]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Member codes", f"{len(member_order)}",
          f"{len(active)} still flying", delta_color="off")
m2.metric(f"Seats, 12 mo to {latest:%b %Y}", f"{last12.seats.sum() / 1e6:,.0f} M")
m3.metric("Routes now", f"{fm.iloc[-1].routes_served:,}")
m4.metric("Load factor, last 12 mo",
          f"{100 * last12.passengers.sum() / last12.seats.sum():.1f}%")

with st.expander("Merger timeline", expanded=False):
    tl = mem.copy()
    tl["Merged"] = tl.merged_month.map(
        lambda m: "—" if pd.isna(m) else f"{pd.Timestamp(m):%b %Y}")
    st.dataframe(tl.rename(columns={"carrier": "Code", "note": "History"})
                   [["Code", "Merged", "History"]],
                 hide_index=True, width="stretch")


def add_merger_lines(fig):
    """Dotted line + caption at each merger inside the window."""
    mm = mem.dropna(subset=["merged_month"])
    for m, grp in mm.groupby("merged_month"):
        if w0 <= pd.Timestamp(m) <= w1:
            fig.add_vline(x=str(m), line_dash="dot", line_color="gray",
                          annotation_text="/".join(grp.carrier) + f" → {fam}",
                          annotation_position="top left",
                          annotation_font_size=11,
                          annotation_font_color="gray")


# --- merger history ----------------------------------------------------------
st.subheader("Merger history")
g1, g2 = st.columns(2)

area = cm.copy()
ycol = "seats"
if smooth:
    area = roll12(area, "carrier", "seats").dropna(subset=["seats_12m"])
    ycol = "seats_12m"
suffix = " (12-mo avg)" if smooth else ""
area = area[area.flight_month.between(w0, w1)]
area[ycol] = area[ycol] / 1e6

figa = px.area(area, x="flight_month", y=ycol, color="carrier",
               category_orders={"carrier": member_order},
               color_discrete_map=fam_colors,
               hover_data={"carrier_name": True},
               title=f"{fam_label[fam]} — seats/month by member code{suffix}",
               labels={ycol: "seats per month (millions)",
                       "flight_month": "", "carrier": "",
                       "carrier_name": "operator"})
# White seams between stacked bands: the merged partners' brand colors can sit
# close (DL/NW reds), so identity never rides on the fill color alone.
# px.area derives fillcolor FROM the line color, so pin the fill first or the
# white seam washes the whole band out.
for tr in figa.data:
    tr.fillcolor = fam_colors.get(tr.name, tr.line.color)
    tr.line.update(width=1, color="white")
add_merger_lines(figa)
figa.update_layout(legend=dict(orientation="h", y=-0.18, x=0))
g1.plotly_chart(figa, use_container_width=True)

net = fm.copy()
rcol = "routes_served"
if smooth:
    net["fam"] = fam
    net = roll12(net, "fam", "routes_served").dropna(subset=["routes_served_12m"])
    rcol = "routes_served_12m"
net = net[net.flight_month.between(w0, w1)]
fign = px.line(net, x="flight_month", y=rcol,
               color_discrete_sequence=[fam_colors.get(fam, "#444444")],
               hover_data={"airports_served": True},
               title=f"{fam_label[fam]} — routes served{suffix}",
               labels={rcol: "directional routes with service",
                       "flight_month": "", "airports_served": "airports"})
fign.update_yaxes(rangemode="tozero")
add_merger_lines(fign)
g2.plotly_chart(fign, use_container_width=True)
g2.caption("Family view: pre-merger months count the then-independent members "
           "combined, so the line is continuous across mergers.")

# --- route activity ----------------------------------------------------------
st.subheader("Route activity — additions and exits")
st.caption("A route is **added** the first month the family serves the city "
           "pair after ≥ 12 months without it (so seasonal summer/winter "
           "routes don't churn every year), and **ended** on its last month "
           "before ≥ 12 months of silence. The last 12 months can't confirm "
           "exits yet, and the first year of data (1990) is start-censored — "
           "both are excluded.")
floor = st.slider("Min seats/month on the city pair (both directions)",
                  0, 3000, 500, step=100)

churn = q("""
    WITH rm AS (
        SELECT LEAST(r.origin, r.dest) AS a, GREATEST(r.origin, r.dest) AS b,
               r.flight_month, SUM(r.seats) AS seats
        FROM gold.route_carrier_month r
        LEFT JOIN silver.dim_carrier_family f ON f.carrier = r.carrier
        WHERE COALESCE(f.family, r.carrier) = :fam AND r.origin <> r.dest
        GROUP BY 1, 2, r.flight_month
        HAVING SUM(r.seats) >= :floor
    ),
    g AS (
        SELECT flight_month,
               LAG(flight_month)  OVER w AS prev_m,
               LEAD(flight_month) OVER w AS next_m
        FROM rm WINDOW w AS (PARTITION BY a, b ORDER BY flight_month)
    )
    SELECT date_trunc('year', flight_month)::date AS year,
           COUNT(*) FILTER (WHERE prev_m IS NULL
                               OR flight_month - prev_m > 366) AS added,
           COUNT(*) FILTER (WHERE (next_m IS NULL OR next_m - flight_month > 366)
                               AND flight_month <= :horizon) AS ended
    FROM g
    WHERE flight_month >= '1991-01-01'
    GROUP BY 1 ORDER BY 1""",
    fam=fam, floor=floor, horizon=latest - pd.DateOffset(months=12))
churn["year"] = pd.to_datetime(churn.year).dt.year
churn = churn[(churn.year >= w0.year) & (churn.year <= w1.year)]

long = pd.concat([
    churn.assign(what="added", n=churn.added),
    churn.assign(what="ended", n=-churn.ended)])
figc = px.bar(long, x="year", y="n", color="what",
              color_discrete_map={"added": "seagreen", "ended": "indianred"},
              hover_data={"what": False, "year": True},
              title=f"{fam_label[fam]} — city pairs added and ended per year",
              labels={"n": "city pairs (ended shown negative)", "year": "",
                      "what": ""})
figc.add_hline(y=0, line_color="gray", line_width=1)
add_merger_lines(figc)
figc.update_layout(legend=dict(orientation="h", y=-0.18, x=0), barmode="relative")
st.plotly_chart(figc, use_container_width=True)
if w1.year >= latest.year:
    st.caption(f"{latest.year} is partial — BTS data runs through "
               f"{latest:%b %Y}, and exits within the last 12 months are "
               "not yet confirmable.")

# --- recent adds & exits, in detail ------------------------------------------
st.subheader(f"Recent moves — 12 months to {w1:%b %Y} vs the year before")
cut12, cut24 = w1 - pd.DateOffset(months=12), w1 - pd.DateOffset(months=24)
rec = q("""
    SELECT LEAST(r.origin, r.dest) AS a, GREATEST(r.origin, r.dest) AS b,
           MAX(CASE WHEN r.origin < r.dest THEN r.origin_city
                    ELSE r.dest_city END)   AS a_city,
           MAX(CASE WHEN r.origin < r.dest THEN r.dest_city
                    ELSE r.origin_city END) AS b_city,
           CASE WHEN r.flight_month > :c12 THEN 'latest' ELSE 'prior' END
               AS period,
           COUNT(DISTINCT r.flight_month) AS months,
           SUM(r.seats)::float AS seats, SUM(r.passengers)::float AS passengers,
           STRING_AGG(DISTINCT r.carrier, ', ') AS carriers
    FROM gold.route_carrier_month r
    LEFT JOIN silver.dim_carrier_family f ON f.carrier = r.carrier
    WHERE COALESCE(f.family, r.carrier) = :fam AND r.origin <> r.dest
      AND r.flight_month > :c24 AND r.flight_month <= :w1
    GROUP BY 1, 2, 5""", fam=fam, c12=cut12, c24=cut24, w1=w1)

piv = rec.pivot_table(index=["a", "b", "a_city", "b_city"], columns="period",
                      values=["months", "seats", "passengers"], fill_value=0,
                      aggfunc="sum")
piv.columns = [f"{x}_{y}" for x, y in piv.columns]
piv = piv.reset_index()
for col in ("months_latest", "months_prior", "seats_latest", "seats_prior",
            "passengers_latest", "passengers_prior"):
    if col not in piv:
        piv[col] = 0.0
ops = rec[rec.period == "latest"].set_index(["a", "b"]).carriers.to_dict()
ops_prior = rec[rec.period == "prior"].set_index(["a", "b"]).carriers.to_dict()


def move_table(rows, period, operators):
    rows = rows.copy()
    rows["Route"] = rows.a + " ⇄ " + rows.b
    rows["Cities"] = (rows.a_city.fillna("?").str.split(",").str[0] + " — "
                      + rows.b_city.fillna("?").str.split(",").str[0])
    rows["Operated by"] = rows.apply(lambda r: operators.get((r.a, r.b), ""),
                                     axis=1)
    rows["Seats/mo"] = (rows[f"seats_{period}"]
                        / rows[f"months_{period}"]).round()
    rows["Months"] = rows[f"months_{period}"].astype(int)
    rows["LF %"] = (100 * rows[f"passengers_{period}"]
                    / rows[f"seats_{period}"]).round(1)
    return (rows.sort_values("Seats/mo", ascending=False)
                [["Route", "Cities", "Operated by", "Seats/mo", "Months", "LF %"]]
                .head(15))


new = piv[(piv.months_latest >= 2) & (piv.months_prior == 0)
          & (piv.seats_latest / piv.months_latest.clip(lower=1) >= floor)]
gone = piv[(piv.months_prior >= 2) & (piv.months_latest == 0)
           & (piv.seats_prior / piv.months_prior.clip(lower=1) >= floor)]

t1, t2 = st.columns(2)
t1.markdown(f"**Added** — flying now, not a year ago ({len(new)})")
if new.empty:
    t1.caption("None above the seats/month floor.")
else:
    t1.dataframe(move_table(new, "latest", ops), hide_index=True,
                 width="stretch")
t2.markdown(f"**Ended** — flew a year ago, gone now ({len(gone)})")
if gone.empty:
    t2.caption("None above the seats/month floor.")
else:
    t2.dataframe(move_table(gone, "prior", ops_prior), hide_index=True,
                 width="stretch")
