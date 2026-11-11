"""
Trade Explorer - live UN Comtrade dashboard
Goods (HS2/HS4/HS6, annual or monthly, weight, $/kg, transport mode, all 12 trade flows)
and Services (EBOPS categories, annual).

Run in Codespaces:   streamlit run app.py
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

API_BASE = "https://comtradeapi.un.org/data/v1/get"
REF_URL = "https://comtradeapi.un.org/files/v1/app/reference/"
TODAY = date.today()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# All Comtrade trade flows.  M = FM + RM + MIP + MOP   and   X = DX + RX + XIP + XOP
FLOWS = {
    "X": "Export (total)",
    "M": "Import (total)",
    "DX": "Domestic export",
    "RX": "Re-export",
    "FM": "Foreign import",
    "RM": "Re-import",
    "XIP": "Export after inward processing",
    "MIP": "Import for inward processing",
    "XOP": "Export for outward processing",
    "MOP": "Import after outward processing",
    "XIF": "Export, intra-firm",
    "MIF": "Import, intra-firm",
}


# ================================================================ API access
def get_key():
    """API key: from Codespaces env var, or from Streamlit Cloud secrets."""
    key = os.environ.get("COMTRADE_KEY")
    if not key:
        try:
            key = st.secrets["COMTRADE_KEY"]
        except Exception:
            key = None
    return key


_last_call = {"t": 0.0}


def call_api(path, params):
    """One request to Comtrade, with polite spacing and retries on 429 (too many requests)."""
    headers = {"Ocp-Apim-Subscription-Key": get_key()}
    problem = ""
    for attempt in range(6):
        gap = time.time() - _last_call["t"]
        if gap < 2:                                   # max ~1 request every 2 seconds
            time.sleep(2 - gap)
        try:
            r = requests.get(f"{API_BASE}/{path}", params=params, headers=headers, timeout=120)
        except requests.exceptions.RequestException as e:
            _last_call["t"] = time.time()
            problem = type(e).__name__
            time.sleep(5)
            continue
        _last_call["t"] = time.time()
        if r.status_code == 429:
            problem = "429 Too Many Requests"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Comtrade answered with status {r.status_code}: {r.text[:300]}")
        return pd.DataFrame(r.json().get("data") or [])
    raise RuntimeError(f"Comtrade did not answer after several tries ({problem}). Try again in a minute.")


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def load_reference(filename):
    """Comtrade's public reference lists (no key needed)."""
    r = requests.get(REF_URL + filename, timeout=60)
    r.raise_for_status()
    return pd.DataFrame(r.json()["results"])


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_goods(reporter, partner, freq, period, flows):
    """ONE request = one year (annual) or one month (monthly): all HS6 products,
    split by mode of transport. motCode 0 rows = totals."""
    df = call_api(f"C/{freq}/HS", {
        "reporterCode": reporter, "partnerCode": partner, "period": period,
        "flowCode": ",".join(flows), "cmdCode": "AG6",
        "breakdownMode": "plus", "customsCode": "C00", "partner2Code": 0,
        "includeDesc": "true",
    })
    df["period_req"] = period
    return df


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_services(reporter, partner, year, flows, codes):
    """Services (EBOPS), annual. Codes are sent in chunks."""
    return call_api("S/A/EB", {
        "reporterCode": reporter, "partnerCode": partner, "period": year,
        "flowCode": ",".join(flows), "cmdCode": ",".join(codes),
        "breakdownMode": "classic", "includeDesc": "true",
    })


# ================================================================ reference helpers
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
    try:
        df = load_reference("HS.json")
        return {str(i): str(t).split(" - ", 1)[-1] for i, t in zip(df["id"], df["text"])}
    except Exception:
        return {}


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def ebops_tree():
    """Service codes with their root ('200' or 'S') and depth (1 = main categories)."""
    df = load_reference("EB.json")
    df["id"], df["parent"] = df["id"].astype(str), df["parent"].astype(str)
    parent = dict(zip(df["id"], df["parent"]))
    roots, depths = [], []
    for code in df["id"]:
        c, d = code, 0
        while parent.get(c) in parent and d < 10:     # climb until the top
            c, d = parent[c], d + 1
        roots.append(c)
        depths.append(d)
    df["root"], df["depth"] = roots, depths
    return df


# ================================================================ formatting
def money(v):
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:,.2f} bn"
    if v >= 1e6:
        return f"{sign}${v / 1e6:,.1f} mln"
    return f"{sign}${v / 1e3:,.0f} k"


def label(code, names):
    n = names.get(code, "")
    return f"{code} – {n[:60]}" if n else code


def period_label(p):
    p = str(p)
    return f"{MONTHS[int(p[4:6]) - 1]} {p[:4]}" if len(p) == 6 else p


NUM = st.column_config.NumberColumn
VALUE_COL = NUM("Value (USD)", format="localized")


# ================================================================ page + sidebar
st.title("🌍 Trade Explorer")
st.caption("Live data from UN Comtrade · goods (HS) and services (EBOPS)")

if not get_key():
    st.error("No API key found. Add COMTRADE_KEY as a Codespaces secret (or Streamlit secret).")
    st.stop()

try:
    reporters = country_table("Reporters.json")
    partners = country_table("partnerAreas.json")
except Exception as e:
    st.error(f"Could not load the country lists from Comtrade: {e}")
    st.stop()

rep_names, par_names = reporters["name"].tolist(), partners["name"].tolist()
rep_code = dict(zip(reporters["name"], reporters["code"]))
par_code = dict(zip(partners["name"], partners["code"]))
years = list(range(TODAY.year, 1987, -1))

with st.sidebar.form("query"):
    st.header("What do you want to see?")
    kind = st.radio("Type", ["Goods", "Services"], horizontal=True)
    freq = st.radio("Frequency", ["Annual", "Monthly"], horizontal=True,
                    help="Monthly is available for goods only.")
    reporter = st.selectbox("Reporter (country reporting the data)", rep_names,
                            index=rep_names.index("Georgia") if "Georgia" in rep_names else 0)
    partner = st.selectbox("Partner", par_names,
                           index=par_names.index("World") if "World" in par_names else 0)
    year = st.selectbox("Year", years, index=years.index(TODAY.year - 2))
    months = st.multiselect("Months (monthly only)", MONTHS, default=MONTHS)
    flows = st.multiselect("Trade flows", list(FLOWS), default=["X", "M"],
                           format_func=lambda f: f"{f} – {FLOWS[f]}")
    go = st.form_submit_button("Get data", type="primary", width="stretch")

st.sidebar.caption("Annual = 1 API call. Monthly = 1 call per month. "
                   "Services = ~3 calls. Free limit: 500 calls/day. "
                   "Repeating a selection is instant and free.")
with st.sidebar.expander("About trade flows"):
    st.markdown("**M** (total imports) = FM + RM + MIP + MOP  \n"
                "**X** (total exports) = DX + RX + XIP + XOP  \n"
                "Every country reports M and X; the sub-flows only when the country "
                "reports them. Each flow is shown in its own tab and never added together.")

if go:
    problems = []
    if not flows:
        problems.append("Pick at least one trade flow.")
    if kind == "Goods" and freq == "Monthly":
        # drop months that are in the future
        m_nums = [MONTHS.index(m) + 1 for m in months
                  if (year, MONTHS.index(m) + 1) < (TODAY.year, TODAY.month)]
        if not m_nums:
            problems.append("Pick at least one month that is already over.")
        periods = tuple(f"{year}{m:02d}" for m in m_nums)
    else:
        if year >= TODAY.year:
            problems.append(f"Annual data for {year} doesn't exist yet. "
                            + ("Switch to **Monthly**." if kind == "Goods" else "Pick an earlier year."))
        periods = (str(year),)
    if kind == "Services":
        flows = [f for f in flows if f in ("X", "M")] or ["X", "M"]
    for p in problems:
        st.sidebar.error(p)
    if not problems:
        st.session_state["selection"] = dict(
            kind=kind, freq="M" if (kind == "Goods" and freq == "Monthly") else "A",
            reporter=reporter, partner=partner, year=year, periods=periods, flows=tuple(flows))

if "selection" not in st.session_state:
    st.info("👈 Choose type, frequency, reporter, partner, year and flows, then click **Get data**.")
    st.stop()

S = st.session_state["selection"]
rc, pc = rep_code[S["reporter"]], par_code[S["partner"]]


# ================================================================ SERVICES
def show_services():
    if S["kind"] == "Services" and freq == "Monthly":
        st.caption("Services data in Comtrade is annual only.")
    try:
        tree = ebops_tree()
    except Exception as e:
        st.error(f"Could not load the services code list: {e}")
        st.stop()
    codes = tree["id"].tolist()
    chunks = [tuple(codes[i:i + 150]) for i in range(0, len(codes), 150)]
    frames = []
    with st.spinner(f"Asking UN Comtrade for services {S['reporter']} ↔ {S['partner']}, {S['year']}..."):
        try:
            for ch in chunks:
                frames.append(fetch_services(rc, pc, S["year"], S["flows"], ch))
        except RuntimeError as e:
            st.error(str(e))
            st.stop()
    raw = pd.concat(frames, ignore_index=True)
    if raw.empty:
        st.warning(f"No services data: {S['reporter']} may not report services with "
                   f"{S['partner']} for {S['year']}. Try Partner = World or another year.")
        st.stop()

    df = raw.copy()
    df["cmdCode"] = df["cmdCode"].astype(str)
    df["primaryValue"] = pd.to_numeric(df["primaryValue"], errors="coerce")
    if "mosCode" in df.columns:                       # prefer 'all modes of supply' rows
        df["_tot"] = df["mosCode"].astype(str).isin(["0", "-1", "TOTAL"])
        df = df.sort_values("_tot", ascending=False)
    df = df.drop_duplicates(["flowCode", "cmdCode"])
    df = df.merge(tree[["id", "text", "root", "depth"]], left_on="cmdCode", right_on="id", how="left")

    # countries report in one of two code trees -> use the one with more data
    root = df[df["depth"] > 0]["root"].value_counts().idxmax() if (df["depth"] > 0).any() else df["root"].mode()[0]
    df = df[df["root"] == root]
    st.subheader(f"Services · {S['reporter']} ↔ {S['partner']} · {S['year']}")
    st.caption("Classification: " + ("EBOPS 2010 (SDMX codes)" if root == "S" else "EBOPS 2002/2010")
               + " · services have no weight or transport mode")

    present = [f for f in S["flows"] if f in set(df["flowCode"])]
    totals = {}
    for col, f in zip(st.columns(max(len(present), 1)), present):
        d = df[df["flowCode"] == f]
        top = d[d["depth"] == 0]["primaryValue"]
        totals[f] = top.iloc[0] if len(top) else d[d["depth"] == 1]["primaryValue"].sum()
        col.metric(FLOWS[f], money(totals[f]))

    max_depth = int(df["depth"].max())
    level = st.radio("Detail level", list(range(1, max_depth + 1)), horizontal=True,
                     format_func=lambda d: "Main categories" if d == 1 else f"Level {d}")
    for tab, f in zip(st.tabs([FLOWS[f] for f in present]), present):
        with tab:
            t = df[(df["flowCode"] == f) & (df["depth"] == level)].copy()
            if t.empty:
                st.info("No data at this level.")
                continue
            t["description"] = t["text"].fillna(t.get("cmdDesc", ""))
            t["share"] = 100 * t["primaryValue"] / totals[f] if totals[f] else None
            t = t.sort_values("primaryValue", ascending=False)
            top15 = t.head(15).iloc[::-1]
            fig = px.bar(top15, x="primaryValue", y="description", orientation="h",
                         labels={"primaryValue": "Value (USD)", "description": ""},
                         title=f"Top {len(top15)} service categories")
            fig.update_layout(height=max(300, 32 * len(top15) + 80), margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig, width="stretch", key=f"svc_bar_{f}")
            out = t[["cmdCode", "description", "primaryValue", "share"]]
            st.dataframe(out, hide_index=True, width="stretch", column_config={
                "cmdCode": "Code", "description": "Service",
                "primaryValue": VALUE_COL, "share": NUM("Share of total %", format="%.1f")})
            st.download_button("⬇️ Download this table (CSV)", out.to_csv(index=False),
                               file_name=f"services_{S['reporter']}_{S['partner']}_{S['year']}_{f}.csv",
                               mime="text/csv", key=f"dl_s_{f}")


# ================================================================ GOODS
def prepare(df):
    df = df.copy()
    df["cmdCode"] = df["cmdCode"].astype(str)
    df = df[df["cmdCode"].str.len() == 6]
    for c in ["primaryValue", "netWgt", "motCode"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["HS2"], df["HS4"], df["HS6"] = df.cmdCode.str[:2], df.cmdCode.str[:4], df.cmdCode
    if "motDesc" not in df.columns:
        df["motDesc"] = df["motCode"].astype(str)
    return df


def summarize(rows, level, names):
    g = rows.groupby(level).agg(value=("primaryValue", "sum"), kg=("netWgt", "sum")).reset_index()
    g = g.rename(columns={level: "code"})
    if level == "HS6":
        g["description"] = g["code"].map(rows.drop_duplicates("HS6").set_index("HS6")["cmdDesc"])
    else:
        g["description"] = g["code"].map(names)
    g["description"] = g["description"].fillna("")
    g["tonnes"] = g["kg"] / 1000
    g["usd_per_kg"] = (g["value"] / g["kg"]).where(g["kg"] > 0)
    g["share"] = 100 * g["value"] / g["value"].sum()
    return g.sort_values("value", ascending=False)[["code", "description", "value", "tonnes", "usd_per_kg", "share"]]


def show_goods():
    names = hs_names()
    periods = S["periods"]
    frames, truncated = [], False
    bar = st.progress(0.0, text="Asking UN Comtrade...")
    try:
        for i, p in enumerate(periods):
            bar.progress(i / len(periods), text=f"Downloading {period_label(p)} ({i + 1}/{len(periods)})...")
            d = fetch_goods(rc, pc, S["freq"], p, S["flows"])
            truncated |= len(d) >= 100_000
            frames.append(d)
    except RuntimeError as e:
        bar.empty()
        st.error(str(e) + " Months already downloaded are saved; click Get data again to continue.")
        st.stop()
    bar.empty()

    raw = pd.concat(frames, ignore_index=True)
    with_data = [p for p, d in zip(periods, frames) if not d.empty]
    if raw.empty or not with_data:
        st.warning(f"No data: {S['reporter']} may not have reported this period yet, "
                   f"or there was no trade with {S['partner']}. Try another period.")
        st.stop()
    if truncated:
        st.warning("A request hit the 100,000-row free-tier maximum, so some rows may be missing. "
                   "Pick fewer flows or a specific partner instead of World.")

    df = prepare(raw)
    totals = df[df["motCode"] == 0]
    by_mode = df[df["motCode"] != 0]
    present = [f for f in S["flows"] if f in set(totals["flowCode"])]
    missing = [f for f in S["flows"] if f not in present]

    span = period_label(with_data[0]) if len(with_data) == 1 else \
        f"{period_label(with_data[0])} – {period_label(with_data[-1])}"
    st.subheader(f"Goods · {S['reporter']} ↔ {S['partner']} · {span}")
    if S["freq"] == "M":
        no_data = [period_label(p) for p in periods if p not in with_data]
        st.caption(f"Months with data: {len(with_data)}"
                   + (f" · not (yet) available: {', '.join(no_data)}" if no_data else "")
                   + " · tables below add up the selected months")
    if missing:
        st.caption("Not reported by this country for this selection: "
                   + ", ".join(FLOWS[f] for f in missing))

    for col, f in zip(st.columns(max(len(present), 1)), present):
        t = totals[totals["flowCode"] == f]
        col.metric(FLOWS[f], money(t["primaryValue"].sum()))
        col.caption(f"Net weight: {t['netWgt'].sum() / 1000:,.0f} t")

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

    # ---- monthly trend
    if S["freq"] == "M" and len(with_data) > 1:
        tr = sel.groupby(["period_req", "flowCode"])["primaryValue"].sum().reset_index()
        tr["month"] = tr["period_req"].map(period_label)
        tr["flow"] = tr["flowCode"].map(FLOWS)
        fig = px.line(tr.sort_values("period_req"), x="month", y="primaryValue", color="flow", markers=True,
                      labels={"primaryValue": "Value (USD)", "month": "", "flow": ""},
                      title="Monthly trend (current filter)")
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, width="stretch", key="trend")

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
            left.plotly_chart(fig, width="stretch", key=f"bar_{f}")

            m = sel_mode[sel_mode["flowCode"] == f]
            with right:
                st.markdown("**By mode of transport**")
                if m.empty:
                    st.info(f"{S['reporter']} does not report transport mode for this selection.")
                else:
                    mt = (m.groupby("motDesc").agg(value=("primaryValue", "sum"), kg=("netWgt", "sum"))
                          .reset_index().sort_values("value", ascending=False))
                    mt["tonnes"] = mt["kg"] / 1000
                    mt["share"] = 100 * mt["value"] / mt["value"].sum()
                    pie = px.pie(mt, names="motDesc", values="value", hole=0.5)
                    pie.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
                    st.plotly_chart(pie, width="stretch", key=f"pie_{f}")
                    st.dataframe(mt[["motDesc", "value", "tonnes", "share"]], hide_index=True,
                                 column_config={"motDesc": "Mode", "value": VALUE_COL,
                                                "tonnes": NUM("Tonnes", format="localized"),
                                                "share": NUM("Share %", format="%.1f")})

            st.dataframe(table, hide_index=True, width="stretch", column_config={
                "code": level, "description": "Product", "value": VALUE_COL,
                "tonnes": NUM("Net weight (t)", format="localized"),
                "usd_per_kg": NUM("USD per kg", format="%.2f"),
                "share": NUM("Share %", format="%.2f")})
            st.download_button("⬇️ Download this table (CSV)", table.to_csv(index=False),
                               file_name=f"{S['reporter']}_{S['partner']}_{span}_{f}_{level}.csv",
                               mime="text/csv", key=f"dl_{f}")


if S["kind"] == "Services":
    show_services()
else:
    show_goods()