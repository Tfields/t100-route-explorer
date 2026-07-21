"""Destinations - every nonstop from one airport: map, top routes, trend."""
import pathlib
import sys

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from warehouse import carrier_color_map, month_window, q

st.set_page_config(page_title="T-100 Destinations", page_icon="🌐", layout="wide")

st.title("🌐 Destinations")
st.caption("Every nonstop destination served from an airport — BTS T-100, "
           "scheduled passenger service, departures side.")

# --- pickers -----------------------------------------------------------------
airports = q("""
    SELECT origin AS code, MAX(origin_city) AS city
    FROM gold.route_carrier_month
    GROUP BY origin ORDER BY SUM(seats) DESC""")
airport_label = dict(zip(airports.code, airports.code + " — " + airports.city.fillna("")))
codes = airports.code.tolist()

c1, c2 = st.columns([2, 3])
apt = c1.selectbox("Airport", airports.code,
                   index=codes.index("SEA") if "SEA" in codes else 0,
                   format_func=lambda c: airport_label.get(c, c))

months = q("""
    SELECT DISTINCT flight_month FROM gold.route_carrier_month
    WHERE origin = :a ORDER BY 1""", a=apt).flight_month.tolist()
w0, w1 = month_window(c2, months)

df = q("""
    SELECT flight_month, carrier, carrier_name, dest, dest_city, dest_country,
           dest_lat::float AS d_lat, dest_lon::float AS d_lon,
           origin_lat::float AS o_lat, origin_lon::float AS o_lon,
           seats::float AS seats, passengers::float AS passengers
    FROM gold.route_carrier_month
    WHERE origin = :a AND flight_month BETWEEN :w0 AND :w1
    ORDER BY flight_month""", a=apt, w0=w0, w1=w1)

if df.empty:
    st.info("No scheduled passenger service in this window.")
    st.stop()

# All carriers serving the airport in the window start selected; changing the
# airport or window re-selects everything relevant (no stickiness by design).
copt = (df.groupby(["carrier", "carrier_name"], as_index=False).seats.sum()
          .sort_values("seats", ascending=False))
carrier_label = dict(zip(copt.carrier, copt.carrier_name + " (" + copt.carrier + ")"))
picked = st.multiselect("Carriers", copt.carrier.tolist(),
                        default=copt.carrier.tolist(),
                        format_func=lambda c: carrier_label.get(c, c))
sel = df[df.carrier.isin(picked)]
if sel.empty:
    st.info("Nothing matches the current filters.")
    st.stop()

# --- headline metrics --------------------------------------------------------
per_dest = (sel.groupby(["dest"], as_index=False)
               .agg(dest_city=("dest_city", "max"),
                    d_lat=("d_lat", "max"), d_lon=("d_lon", "max"),
                    seats=("seats", "sum"), passengers=("passengers", "sum"),
                    carriers=("carrier", "nunique")))
per_dest["load_factor"] = 100 * per_dest.passengers / per_dest.seats

m1, m2, m3, m4 = st.columns(4)
m1.metric("Destinations", f"{len(per_dest):,}")
m2.metric("Carriers", f"{sel.carrier.nunique()}")
m3.metric("Passengers", f"{sel.passengers.sum():,.0f}")
m4.metric("Load factor", f"{100 * sel.passengers.sum() / sel.seats.sum():.1f}%")

# --- spoke map ---------------------------------------------------------------
ends = per_dest.dropna(subset=["d_lat", "d_lon"])
origin_pt = sel.dropna(subset=["o_lat", "o_lon"])
figm = go.Figure()
if not ends.empty and not origin_pt.empty:
    o = origin_pt.iloc[0]
    max_seats = ends.seats.max()
    for _, r in ends.iterrows():
        figm.add_trace(go.Scattergeo(
            lon=[o.o_lon, r.d_lon], lat=[o.o_lat, r.d_lat], mode="lines",
            line=dict(width=max(0.4, 3.5 * r.seats / max_seats),
                      color="rgba(178,34,34,0.35)"),
            hoverinfo="skip", showlegend=False))
    figm.add_trace(go.Scattergeo(
        lon=ends.d_lon, lat=ends.d_lat, mode="markers",
        marker=dict(size=5, color="rgb(120,20,20)"),
        text=(ends.dest + " — " + ends.dest_city.fillna("")
              + "<br>seats: " + ends.seats.map("{:,.0f}".format)
              + "<br>passengers: " + ends.passengers.map("{:,.0f}".format)
              + "<br>load factor: " + ends.load_factor.map("{:.1f}%".format)
              + "<br>carriers: " + ends.carriers.astype(str)),
        hoverinfo="text", showlegend=False))
    figm.update_geos(projection_type="natural earth", resolution=50,
                     showcountries=True, countrycolor="rgb(180,180,180)",
                     showsubunits=True, subunitcolor="rgb(215,215,215)",
                     showlakes=True, lakecolor="rgb(220,235,245)",
                     coastlinecolor="rgb(160,160,160)",
                     landcolor="rgb(243,243,238)")
    figm.update_layout(height=550, margin=dict(l=0, r=0, t=30, b=0),
                       title=f"Nonstops from {apt}, {w0:%b %Y} – {w1:%b %Y} "
                             f"({len(ends)} mapped destinations; width ~ seats)")
    st.plotly_chart(figm, use_container_width=True)
    unmapped = len(per_dest) - len(ends)
    if unmapped:
        st.caption(f"{unmapped} destination(s) lack coordinates "
                   "(small airports not in OpenFlights).")
else:
    st.caption("No coordinates available to draw the map.")

# --- top destinations & trend ------------------------------------------------
g1, g2 = st.columns(2)

top = per_dest.nlargest(15, "seats").dest.tolist()
# Cap the legend: top 10 carriers by seats keep their identity (and brand
# color), the long tail is lumped into a grey "Other".
keep = copt.head(10).carrier_name.tolist()
bar = sel[sel.dest.isin(top)].copy()
bar["carrier_name"] = bar.carrier_name.where(bar.carrier_name.isin(keep), "Other")
bar = bar.groupby(["dest", "carrier_name"], as_index=False).seats.sum()
bar_colors = {**carrier_color_map(sel), "Other": "#CFCFCF"}
figb = px.bar(bar, x="seats", y="dest", color="carrier_name", orientation="h",
              category_orders={"dest": top,
                               "carrier_name": keep + ["Other"]},
              color_discrete_map=bar_colors,
              title=f"Top destinations from {apt} by seats, by carrier",
              labels={"seats": "seats over window", "dest": "", "carrier_name": ""})
figb.update_layout(legend=dict(orientation="h", y=-0.18, x=0,
                               font=dict(size=11), tracegroupgap=2))
g1.plotly_chart(figb, use_container_width=True)

trend = (sel.groupby("flight_month")
            .agg(destinations=("dest", "nunique"),
                 seats=("seats", "sum"), passengers=("passengers", "sum"))
            .reset_index())
trend["load_factor"] = 100 * trend.passengers / trend.seats
figt = px.line(trend, x="flight_month", y="destinations",
               title=f"Destinations served from {apt} by month",
               labels={"destinations": "nonstop destinations", "flight_month": ""})
figt.update_yaxes(rangemode="tozero")
g2.plotly_chart(figt, use_container_width=True)

# --- best & worst performers -------------------------------------------------
st.subheader("Best & worst performers — carrier × route")
st.caption("Load factor by carrier-route over the window. The seats/month "
           "floor (plus a 3-months-served minimum) filters out one-off and "
           "token service so the rankings reflect sustained flying.")

p1, p2, p3 = st.columns([1, 1, 1])
scope = p1.radio("Routes", ["All", "Domestic", "International"],
                 horizontal=True, key="perf_scope")
min_mo_seats = p2.slider("Min seats/month", 100, 3000, 500, step=100)
n_each = p3.slider("Best / worst shown", 3, 10, 5)

perf = (sel[sel.dest != apt]        # T-100 has odd self-loop rows; drop them
        .groupby(["carrier", "carrier_name", "dest", "dest_city", "dest_country"],
                 dropna=False, as_index=False)
        .agg(seats=("seats", "sum"), passengers=("passengers", "sum"),
             months=("flight_month", "nunique")))
# Unknown country ⇒ small US airport missing from OpenFlights.
perf["domestic"] = perf.dest_country.isna() | (perf.dest_country == "United States")
if scope != "All":
    perf = perf[perf.domestic == (scope == "Domestic")]
perf["seats_mo"] = perf.seats / perf.months
perf = perf[(perf.months >= 3) & (perf.seats_mo >= min_mo_seats) & (perf.seats > 0)]

if len(perf) < 2:
    st.info("Not enough qualifying routes — lower the seats/month floor.")
else:
    perf["load_factor"] = 100 * perf.passengers / perf.seats
    perf["empty_seats"] = perf.seats - perf.passengers
    perf["label"] = perf.carrier + " → " + perf.dest
    scope_lf = 100 * perf.passengers.sum() / perf.seats.sum()

    b1, b2 = st.columns(2)

    ranked = perf.sort_values("load_factor")
    if len(ranked) <= 2 * n_each:
        half = max(1, len(ranked) // 2)
        show = pd.concat([ranked.head(half).assign(side="worst"),
                          ranked.tail(len(ranked) - half).assign(side="best")])
    else:
        show = pd.concat([ranked.head(n_each).assign(side="worst"),
                          ranked.tail(n_each).assign(side="best")])
    figr = px.bar(
        show, x="load_factor", y="label", orientation="h",
        color="side", color_discrete_map={"best": "seagreen", "worst": "indianred"},
        text=show.seats_mo.map(lambda s: f"{s:,.0f}/mo"),
        title=f"{scope} routes from {apt}: {n_each} best & worst load factors"
              f" (dashed line = avg {scope_lf:.1f}%)",
        hover_data={"carrier_name": True, "dest_city": True, "months": True,
                    "seats": ":,.0f", "empty_seats": ":,.0f", "side": False,
                    "label": False},
        labels={"load_factor": "load factor %", "label": "", "side": ""})
    figr.update_yaxes(categoryorder="array", categoryarray=show.label.tolist())
    figr.add_vline(x=scope_lf, line_dash="dash", line_color="gray")
    figr.update_layout(showlegend=False)
    b1.plotly_chart(figr, use_container_width=True)

    figs = px.scatter(
        perf, x="seats_mo", y="load_factor", log_x=True,
        color=perf.domestic.map({True: "Domestic", False: "International"}),
        hover_name="label",
        hover_data={"carrier_name": True, "dest_city": True, "months": True,
                    "seats_mo": ":,.0f", "empty_seats": ":,.0f"},
        title="Every qualifying route: volume vs load factor",
        labels={"seats_mo": "seats/month (log)", "load_factor": "load factor %",
                "color": ""})
    figs.add_hline(y=scope_lf, line_dash="dash", line_color="gray")
    figs.update_layout(legend=dict(orientation="h", y=-0.25))
    b2.plotly_chart(figs, use_container_width=True)
    b2.caption("Bottom-right is the pain quadrant: lots of seats flying "
               "emptier than the airport average.")

# --- momentum: YoY load factor + new/dropped routes --------------------------
cut12 = w1 - pd.DateOffset(months=12)
cut24 = w1 - pd.DateOffset(months=24)
st.subheader("Momentum — last 12 months vs the year before")
st.caption(f"Compares the 12 months ending {w1:%b %Y} with the 12 months "
           "before that (independent of the window start), using the scope, "
           "carrier and seats/month filters above. YoY deltas only count "
           "calendar months served in BOTH years, so seasonal routes compare "
           "like-for-like.")

yy = q("""
    SELECT flight_month, carrier, carrier_name, dest, dest_city, dest_country,
           seats::float AS seats, passengers::float AS passengers
    FROM gold.route_carrier_month
    WHERE origin = :a AND flight_month > :c24 AND flight_month <= :m1
    ORDER BY flight_month""", a=apt, c24=cut24, m1=w1)
yy = yy[yy.carrier.isin(picked) & (yy.dest != apt)].copy()
yy["domestic"] = yy.dest_country.isna() | (yy.dest_country == "United States")
if scope != "All":
    yy = yy[yy.domestic == (scope == "Domestic")]

if yy.empty:
    st.info("No traffic in the last 24 months for these filters.")
else:
    yy["period"] = yy.flight_month.gt(cut12).map({True: "latest", False: "prior"})
    yy["moy"] = yy.flight_month.dt.month
    key = ["carrier", "carrier_name", "dest", "dest_city"]

    mo1, mo2 = st.columns(2)

    # -- YoY LF delta on calendar months served in both years
    m = (yy.groupby(key + ["moy", "period"], as_index=False)
           [["seats", "passengers"]].sum()
           .pivot_table(index=key + ["moy"], columns="period",
                        values=["seats", "passengers"], fill_value=0))
    m.columns = [f"{a}_{b}" for a, b in m.columns]
    m = m.reset_index()
    for c in ("seats_latest", "seats_prior", "passengers_latest", "passengers_prior"):
        if c not in m:
            m[c] = 0.0
    matched = m[(m.seats_latest > 0) & (m.seats_prior > 0)]
    d = matched.groupby(key, as_index=False).agg(
        months=("moy", "nunique"),
        seats_latest=("seats_latest", "sum"), pax_latest=("passengers_latest", "sum"),
        seats_prior=("seats_prior", "sum"), pax_prior=("passengers_prior", "sum"))
    d["seats_mo"] = d.seats_latest / d.months
    d = d[(d.months >= 3) & (d.seats_mo >= min_mo_seats)]

    if len(d) < 2:
        mo1.info("Not enough routes served in both years — lower the floor.")
    else:
        d["lf_latest"] = 100 * d.pax_latest / d.seats_latest
        d["lf_prior"] = 100 * d.pax_prior / d.seats_prior
        d["delta_pp"] = d.lf_latest - d.lf_prior
        d["label"] = d.carrier + " → " + d.dest
        dr = d.sort_values("delta_pp")
        if len(dr) <= 2 * n_each:
            half = max(1, len(dr) // 2)
            showd = pd.concat([dr.head(half).assign(side="declining"),
                               dr.tail(len(dr) - half).assign(side="improving")])
        else:
            showd = pd.concat([dr.head(n_each).assign(side="declining"),
                               dr.tail(n_each).assign(side="improving")])
        figd = px.bar(
            showd, x="delta_pp", y="label", orientation="h",
            color="side", color_discrete_map={"improving": "seagreen",
                                              "declining": "indianred"},
            text=showd.apply(lambda r: f"{r.lf_prior:.0f}→{r.lf_latest:.0f}%",
                             axis=1),
            title="YoY load-factor change (pp); label = prior→current LF",
            hover_data={"carrier_name": True, "dest_city": True,
                        "months": True, "seats_mo": ":,.0f",
                        "lf_prior": ":.1f", "lf_latest": ":.1f",
                        "side": False, "label": False},
            labels={"delta_pp": "pp vs year before", "label": "", "side": ""})
        figd.update_yaxes(categoryorder="array", categoryarray=showd.label.tolist())
        figd.update_layout(showlegend=False)
        mo1.plotly_chart(figd, use_container_width=True)

    # -- new & dropped carrier-routes
    pres = (yy.groupby(key + ["period"], as_index=False)
              .agg(months=("flight_month", "nunique"), seats=("seats", "sum"),
                   passengers=("passengers", "sum"))
              .pivot_table(index=key, columns="period",
                           values=["months", "seats", "passengers"], fill_value=0))
    pres.columns = [f"{a}_{b}" for a, b in pres.columns]
    pres = pres.reset_index()
    for c in ("months_latest", "months_prior", "seats_latest", "seats_prior",
              "passengers_latest", "passengers_prior"):
        if c not in pres:
            pres[c] = 0.0

    def route_table(rows, period):
        rows = rows.copy()
        rows["Seats/mo"] = (rows[f"seats_{period}"] / rows[f"months_{period}"]).round()
        rows["LF %"] = (100 * rows[f"passengers_{period}"]
                        / rows[f"seats_{period}"]).round(1)
        rows["Route"] = rows.carrier + " → " + rows.dest
        rows["City"] = rows.dest_city
        rows["Months"] = rows[f"months_{period}"].astype(int)
        return (rows.sort_values("Seats/mo", ascending=False)
                    [["Route", "City", "Seats/mo", "Months", "LF %"]].head(12))

    new = pres[(pres.months_latest >= 2) & (pres.months_prior == 0)
               & (pres.seats_latest / pres.months_latest >= min_mo_seats)]
    dropped = pres[(pres.months_prior >= 2) & (pres.months_latest == 0)
                   & (pres.seats_prior / pres.months_prior >= min_mo_seats)]

    mo2.markdown(f"**New routes** — flying now, not a year ago ({len(new)})")
    if new.empty:
        mo2.caption("None above the seats/month floor.")
    else:
        mo2.dataframe(route_table(new, "latest"), hide_index=True,
                      width="stretch")
    mo2.markdown(f"**Dropped routes** — flew last year, gone now ({len(dropped)})")
    if dropped.empty:
        mo2.caption("None above the seats/month floor.")
    else:
        mo2.dataframe(route_table(dropped, "prior"), hide_index=True,
                      width="stretch")
