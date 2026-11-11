"""
Trade Explorer - live UN Comtrade dashboard
Pick any reporter, partner, year and flow -> the app asks Comtrade live
and shows HS2 / HS4 / HS6 drill-down, value, net weight, $/kg and transport mode.

Run locally / in Codespaces:   streamlit run app.py
Needs the API key in the environment variable COMTRADE_KEY
(or in Streamlit secrets when deployed online).
"""
import os
import time
from datetime import date

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Trade Explorer · UN Comtrade", layout="wide")

API_URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"        # Goods / Annual / HS
REF_URL = "https://comtradeapi.un.org/files/v1/app/reference/"   # country & product lists
FLOWS = {"X": "Export", "M": "Import", "RX": "Re-export", "RM": "Re-import"}
LATEST_YEAR = date.today().year - 1


# ---------------------------------------------------------------- helpers
def get_key():
    """API key: from Codespaces/terminal env var, or from Streamlit Cloud secrets."""
    key = os.environ.get("COMTRADE_KEY")
    if not key:
        try:
            key = st.secrets["COMTRADE_KEY"]
        except Exception:
            key = None
    return key


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def load_reference(filename):
    """Comtrade's public reference lists (no key needed)."""
    r = requests.get(REF_URL + filename, timeout=60)
    r.raise_for_status()
    return pd.DataFrame(r.json()["results"])


def country_table(filename):
    df = load_reference(filename)
    id_col = next(c for c in ["id", "reporterCode", "PartnerCode"] if c in df.columns)
    name_col = next(c for c in ["text", "reporterDesc", "PartnerDesc"] if c in df.columns)
    out = df[[id_col, name_col]].rename(columns={id_col: "code", name_col: "name"})
    out["code"] = pd.to_numeric(out["code"], errors="coerce")
    out = out.dropna().drop_duplicates("name")
    out["code"] = out["code"].astype(int)
    return out.sort_values("name")


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def hs_names():
    """Names for HS2 / HS4 codes, e.g. '08' -> 'Edible fruit and nuts...'"""
    try:
        df = load_reference("HS.json")
        return {str(i): str(t).split(" - ", 1)[-1] for i, t in zip(df["id"], df["text"])}
    except Exception:
        return {}  # app still works, just shows codes without names


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_trade(reporter, partner, year, flows):
    """ONE request: all HS6 products, split by mode of transport.
    Rows with motCode 0 = totals, other motCodes = Road / Sea / Air / Rail ...
    Cached for 24h, so repeating a selection costs no API calls."""
    params = {
        "reporterCode": reporter,
        "partnerCode": partner,
        "period": year,
        "flowCode": ",".join(flows),
        "cmdCode": "AG6",
        "breakdownMode": "plus",
        "customsCode": "C00",      # all customs procedures together
        "partner2Code": 0,         # all second partners together
        "includeDesc": "true",
    }
    headers = {"Ocp-Apim-Subscription-Key": get_key()}
    problem = ""
    for attempt in range(5):
        try:
            r = requests.get(API_URL, params=params, headers=headers, timeout=120)
        except requests.exceptions.RequestException as e:
            problem = type(e).__name__
            time.sleep(5)
            continue
        if r.status_code == 429:               # too many requests -> wait, retry
            problem = "429 Too Many Requests"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Comtrade answered with status {r.status_code}: {r.text[:300]}")
        return pd.DataFrame(r.json().get("data") or [])
    raise RuntimeError(f"Comtrade did not answer after several tries ({problem}). Try again in a minute.")


def prepare(df):
    df = df.copy()
    df["cmdCode"] = df["cmdCode"].astype(str)
    df = df[df["cmdCode"].str.len() == 6]          # keep HS6 rows
    for c in ["primaryValue", "netWgt", "qty", "motCode"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["HS2"], df["HS4"], df["HS6"] = df.cmdCode.str[:2], df.cmdCode.str[:4], df.cmdCode
    if "motDesc" not in df.columns:
        df["motDesc"] = df["motCode"].astype(str)
    return df


def summarize(rows, level, names):
    """Add HS6 rows up to the chosen level and compute weight, $/kg, share."""
    g = rows.groupby(level).agg(value=("primaryValue", "sum"),
                                kg=("netWgt", "sum")).reset_index()
    g = g.rename(columns={level: "code"})
    if level == "HS6":
        desc = rows.drop_duplicates("HS6").set_index("HS6")["cmdDesc"]
        g["description"] = g["code"].map(desc)
    else:
        g["description"] = g["code"].map(names)
    g["description"] = g["description"].fillna("")
    g["tonnes"] = g["kg"] / 1000
    g["usd_per_kg"] = (g["value"] / g["kg"]).where(g["kg"] > 0)
    g["share"] = 100 * g["value"] / g["value"].sum()
    g = g.sort_values("value", ascending=False)
    return g[["code", "description", "value", "tonnes", "usd_per_kg", "share"]]


def label(code, names):
    n = names.get(code, "")
    return f"{code} – {n[:60]}" if n else code


def money(v):
    if v >= 1e9:
        return f"${v / 1e9:,.2f} bn"
    if v >= 1e6:
        return f"${v / 1e6:,.1f} mln"
    return f"${v / 1e3:,.0f} k"


# ---------------------------------------------------------------- page
st.title("🌍 Trade Explorer")
st.caption("Live data from UN Comtrade · goods · annual · HS as reported")

if not get_key():
    st.error("No API key found. Add COMTRADE_KEY as a Codespaces secret (or Streamlit secret).")
    st.stop()

try:
    reporters = country_table("Reporters.json")
    partners = country_table("partnerAreas.json")
except Exception as e:
    st.error(f"Could not load the country lists from Comtrade: {e}")
    st.stop()

names = hs_names()
rep_names, par_names = reporters["name"].tolist(), partners["name"].tolist()
rep_code = dict(zip(reporters["name"], reporters["code"]))
par_code = dict(zip(partners["name"], partners["code"]))

with st.sidebar.form("query"):
    st.header("What do you want to see?")
    reporter = st.selectbox("Reporter (country reporting the data)", rep_names,
                            index=rep_names.index("Georgia") if "Georgia" in rep_names else 0)
    partner = st.selectbox("Partner", par_names,
                           index=par_names.index("World") if "World" in par_names else 0)
    year = st.selectbox("Year", list(range(LATEST_YEAR, 1987, -1)), index=1)
    flows = st.multiselect("Trade flows", list(FLOWS), default=["X", "M"],
                           format_func=FLOWS.get)
    go = st.form_submit_button("Get data", type="primary", width="stretch")

st.sidebar.caption("Each new selection = 1 API call (500/day free). "
                   "Repeating a selection is instant and free.")

if go:
    if not flows:
        st.sidebar.error("Pick at least one flow.")
    else:
        st.session_state["selection"] = (reporter, partner, year, tuple(flows))

if "selection" not in st.session_state:
    st.info("👈 Choose a reporter, partner, year and flows, then click **Get data**.")
    st.stop()

reporter, partner, year, flows = st.session_state["selection"]

with st.spinner(f"Asking UN Comtrade for {reporter} ↔ {partner}, {year}..."):
    try:
        raw = fetch_trade(rep_code[reporter], par_code[partner], year, flows)
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

if raw.empty:
    st.warning(f"No data: {reporter} may not have reported {year} yet, "
               f"or there was no trade with {partner}. Try another year.")
    st.stop()
if len(raw) >= 100_000:
    st.warning("Comtrade returned 100,000 rows (free-tier maximum), so some rows may be missing. "
               "Pick fewer flows or a specific partner instead of World.")

df = prepare(raw)
totals = df[df["motCode"] == 0]          # total over all transport modes
by_mode = df[df["motCode"] != 0]         # split by Road / Sea / Air / Rail...
present = [f for f in flows if f in set(totals["flowCode"])]

st.subheader(f"{reporter} ↔ {partner} · {year}")
for col, f in zip(st.columns(max(len(present), 1)), present):
    t = totals[totals["flowCode"] == f]
    col.metric(FLOWS[f], money(t["primaryValue"].sum()))
    col.caption(f"Net weight: {t['netWgt'].sum() / 1000:,.0f} tonnes")

# ---- drill-down controls
c1, c2, c3 = st.columns([1, 2, 2])
level = c1.radio("Product level", ["HS2", "HS4", "HS6"], horizontal=True)
chapters = ["All"] + sorted(totals["HS2"].unique())
chapter = c2.selectbox("HS2 chapter", chapters,
                       format_func=lambda c: "All chapters" if c == "All" else label(c, names))
sel, sel_mode = totals, by_mode
if chapter != "All":
    sel, sel_mode = sel[sel["HS2"] == chapter], sel_mode[sel_mode["HS2"] == chapter]
headings = ["All"] + sorted(sel["HS4"].unique())
heading = c3.selectbox("HS4 heading", headings,
                       format_func=lambda c: "All headings" if c == "All" else label(c, names))
if heading != "All":
    sel, sel_mode = sel[sel["HS4"] == heading], sel_mode[sel_mode["HS4"] == heading]

# ---- one tab per flow
for tab, f in zip(st.tabs([FLOWS[f] for f in present]), present):
    with tab:
        rows = sel[sel["flowCode"] == f]
        if rows.empty:
            st.info("No trade for this selection.")
            continue
        table = summarize(rows, level, names)

        left, right = st.columns([3, 2])
        top = table.head(15).copy()
        top["product"] = top["code"] + " " + top["description"].str[:45]
        fig = px.bar(top.iloc[::-1], x="value", y="product", orientation="h",
                     hover_data={"tonnes": ":,.0f", "usd_per_kg": ":,.2f", "product": False},
                     labels={"value": "Value (USD)", "product": ""},
                     title=f"Top {len(top)} products by value ({level})")
        fig.update_layout(height=520, margin=dict(l=10, r=10, t=50, b=10))
        left.plotly_chart(fig, width="stretch")

        m = sel_mode[sel_mode["flowCode"] == f]
        with right:
            st.markdown("**By mode of transport**")
            if m.empty:
                st.info(f"{reporter} does not report transport mode for this selection.")
            else:
                mt = (m.groupby("motDesc")
                       .agg(value=("primaryValue", "sum"), kg=("netWgt", "sum"))
                       .reset_index().sort_values("value", ascending=False))
                mt["tonnes"] = mt["kg"] / 1000
                mt["share"] = 100 * mt["value"] / mt["value"].sum()
                pie = px.pie(mt, names="motDesc", values="value", hole=0.5)
                pie.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(pie, width="stretch")
                st.dataframe(mt[["motDesc", "value", "tonnes", "share"]], hide_index=True,
                             column_config={
                                 "motDesc": "Mode",
                                 "value": st.column_config.NumberColumn("Value (USD)", format="localized"),
                                 "tonnes": st.column_config.NumberColumn("Tonnes", format="localized"),
                                 "share": st.column_config.NumberColumn("Share %", format="%.1f"),
                             })

        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={
                         "code": level,
                         "description": "Product",
                         "value": st.column_config.NumberColumn("Value (USD)", format="localized"),
                         "tonnes": st.column_config.NumberColumn("Net weight (t)", format="localized"),
                         "usd_per_kg": st.column_config.NumberColumn("USD per kg", format="%.2f"),
                         "share": st.column_config.NumberColumn("Share %", format="%.2f"),
                     })
        st.download_button("⬇️ Download this table (CSV)", table.to_csv(index=False),
                           file_name=f"{reporter}_{partner}_{year}_{FLOWS[f]}_{level}.csv",
                           mime="text/csv", key=f"dl_{f}")
