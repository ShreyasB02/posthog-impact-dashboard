"""
app.py - Single-page interactive dashboard for engineer impact.

Stage 3 of 3 (fetch -> score -> app).

Reads scored.json (committed, no secrets) and renders, on one laptop screen:
  - Header + methodology expander
  - Sidebar: live weight sliders (re-rank without recomputing pillars)
  - Top-5 leaderboard cards with composite score + pillar sparkbars
  - Stacked-bar "why" chart decomposing each engineer's weighted contribution
  - Concrete highlights for the selected engineer
  - Collaboration review-network graph (the hero visual for knowledge sharing)

Run locally:  streamlit run app.py
Deploy:       push to GitHub, connect on share.streamlit.io
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import plotly.graph_objects as go
import streamlit as st

SCORED_FILE = Path("scored.json")

PILLAR_LABELS = {
    "importance": "Problem Importance",
    "meaningful": "Meaningful Work",
    "influence": "Helping Others",
    "reliability": "Reliability",
    "knowledge": "Knowledge Sharing",
}
PILLAR_COLORS = {
    "importance": "#1f77b4",
    "meaningful": "#ff7f0e",
    "influence": "#2ca02c",
    "knowledge": "#9467bd",
}

st.set_page_config(
    page_title="PostHog Engineer Impact",
    page_icon=":bar_chart:",
    layout="wide",
)


@st.cache_data
def load_data() -> dict:
    if not SCORED_FILE.exists():
        st.error("scored.json not found. Run `python fetch.py && python score.py` first.")
        st.stop()
    return json.loads(SCORED_FILE.read_text())


def composite(rec: dict, weights: dict, floor: float, span: float) -> float:
    p = rec["pillars"]
    base = (
        weights["importance"] * p["importance"]
        + weights["meaningful"] * p["meaningful"]
        + weights["influence"] * p["influence"]
        + weights["knowledge"] * p["knowledge"]
    )
    guardrail = floor + span * (p["reliability"] / 100.0)
    return base * guardrail


def avatar_url(login: str) -> str:
    return f"https://github.com/{login}.png?size=80"


def main() -> None:
    data = load_data()
    meta = data["meta"]
    g = data["reliability_guardrail"]
    defaults = data["weights"]

    # ---- Sidebar: live weights -------------------------------------------
    st.sidebar.title("Impact model")
    st.sidebar.caption(
        "Four compensating pillars are weighted, then scaled by a Reliability "
        "guardrail so unreliable work can't buy its way to the top."
    )
    weights = {}
    weights["importance"] = st.sidebar.slider("Problem Importance", 0.0, 1.0, float(defaults["importance"]), 0.02)
    weights["meaningful"] = st.sidebar.slider("Meaningful Work", 0.0, 1.0, float(defaults["meaningful"]), 0.02)
    weights["influence"] = st.sidebar.slider("Helping Others", 0.0, 1.0, float(defaults["influence"]), 0.02)
    weights["knowledge"] = st.sidebar.slider("Knowledge Sharing", 0.0, 1.0, float(defaults["knowledge"]), 0.02)
    total = sum(weights.values()) or 1.0
    weights = {k: v / total for k, v in weights.items()}  # normalize so bars compare
    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"Reliability guardrail: score x ({g['floor']} + {g['span']} x reliability). "
        "Reliability is a multiplier, not an additive pillar."
    )

    # ---- recompute ranking -----------------------------------------------
    engineers = data["engineers"]
    for rec in engineers:
        rec["_impact"] = composite(rec, weights, g["floor"], g["span"])
    engineers = sorted(engineers, key=lambda r: r["_impact"], reverse=True)
    top5 = engineers[:5]

    # ---- Header ----------------------------------------------------------
    st.title("Who moves PostHog forward?")
    st.caption(
        f"Top engineers in `{meta['repo']}` over the last {meta['lookback_days']} days "
        f"({meta['counts']['pull_requests']} PRs, {meta['counts']['issues']} issues analyzed). "
        "Impact is modeled from what the team engages with - not lines of code or commit counts."
    )

    with st.expander("How impact is measured"):
        st.markdown(
            """
- **Problem Importance** - contributions weighted by the importance of the problem solved
  (reactions/comments on the issues they closed, how long the pain existed, bug/incident labels).
- **Meaningful Work** - work the team actually engages with, with a ratio penalty on vanity volume.
- **Helping Others** - substantive reviews given to *other people's* PRs, distinct teammates helped,
  others' issues resolved.
- **Reliability** *(guardrail multiplier)* - low revert rate, CI first-pass, tests present.
- **Knowledge Sharing** - reviewer diversity on their PRs and centrality in the review network.

Each pillar is a 0-100 percentile across eligible engineers. Adjust the weights in the sidebar to
see the ranking respond live.
            """
        )

    # ---- Top 5 cards -----------------------------------------------------
    cols = st.columns(5)
    for rank, (col, rec) in enumerate(zip(cols, top5), start=1):
        with col:
            st.markdown(f"**#{rank}**")
            st.image(avatar_url(rec["login"]), width=64)
            st.markdown(f"**[{rec['login']}](https://github.com/{rec['login']})**")
            st.metric("Impact", f"{rec['_impact']:.1f}")
            p = rec["pillars"]
            for key in ("importance", "meaningful", "influence", "knowledge"):
                st.progress(min(p[key] / 100.0, 1.0), text=f"{PILLAR_LABELS[key]}: {p[key]:.0f}")
            st.caption(f"Reliability {p['reliability']:.0f}/100")

    st.markdown("---")

    left, right = st.columns([3, 2])

    # ---- Stacked "why" bar ------------------------------------------------
    with left:
        st.subheader("Why they rank where they do")
        fig = go.Figure()
        logins = [r["login"] for r in reversed(top5)]
        for key in ("importance", "meaningful", "influence", "knowledge"):
            fig.add_trace(go.Bar(
                y=logins,
                x=[weights[key] * r["pillars"][key] for r in reversed(top5)],
                name=PILLAR_LABELS[key],
                orientation="h",
                marker_color=PILLAR_COLORS[key],
                hovertemplate="%{x:.1f} pts<extra>" + PILLAR_LABELS[key] + "</extra>",
            ))
        fig.update_layout(
            barmode="stack",
            height=320,
            margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            xaxis_title="Weighted contribution to impact score",
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Bar segments are each pillar's *weighted* contribution. "
            "Reliability is applied as a multiplier, so it shapes total length rather than a segment."
        )

    # ---- Highlights for selected engineer --------------------------------
    with right:
        st.subheader("Concrete highlights")
        sel = st.selectbox("Engineer", [r["login"] for r in top5])
        rec = next(r for r in top5 if r["login"] == sel)
        raw = rec.get("raw", {})
        c1, c2, c3 = st.columns(3)
        c1.metric("Merged PRs", int(raw.get("merged_prs", 0)))
        c2.metric("Reviews given", int(raw.get("reviews_others", 0)))
        c3.metric("Teammates helped", int(raw.get("distinct_authors_helped", 0)))
        if rec.get("highlights"):
            for h in rec["highlights"]:
                st.markdown(f"- {h['text']}")
        else:
            st.caption("No linked-issue highlights in window; impact driven by reviews/reliability.")

    # ---- Collaboration network (hero visual) -----------------------------
    st.markdown("---")
    st.subheader("The review network: who spreads knowledge")
    st.caption(
        "Each edge is a substantive review (reviewer to author). Larger, central nodes review across "
        "many teammates - the opposite of knowledge hoarding. Top 5 are highlighted."
    )
    render_network(data["graph"], {r["login"] for r in top5})


def render_network(graph_data: dict, highlight: set) -> None:
    edges = graph_data.get("edges", [])
    nodes = [n["id"] for n in graph_data.get("nodes", [])]
    if not edges:
        st.info("Not enough review interactions to render a network.")
        return

    G = nx.DiGraph()
    G.add_nodes_from(nodes)
    for e in edges:
        G.add_edge(e["source"], e["target"], weight=e.get("weight", 1))

    # keep it legible: drop isolates, cap to most-connected nodes
    G.remove_nodes_from(list(nx.isolates(G)))
    if G.number_of_nodes() == 0:
        st.info("Not enough review interactions to render a network.")
        return
    if G.number_of_nodes() > 60:
        top_nodes = sorted(G.degree(weight="weight"), key=lambda x: x[1], reverse=True)[:60]
        G = G.subgraph([n for n, _ in top_nodes]).copy()

    pos = nx.spring_layout(G, k=0.6, seed=42, weight="weight")
    deg = dict(G.degree(weight="weight"))

    edge_x, edge_y = [], []
    for u, v in G.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
    edge_trace = go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=0.5, color="rgba(150,150,150,0.4)"),
        hoverinfo="none",
    )

    node_x, node_y, sizes, colors, texts = [], [], [], [], []
    for n in G.nodes():
        x, y = pos[n]
        node_x.append(x)
        node_y.append(y)
        sizes.append(8 + 2.2 * deg.get(n, 1))
        colors.append("#d62728" if n in highlight else "#1f77b4")
        texts.append(f"{n} (degree {deg.get(n, 0)})")
    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers+text",
        text=[n if n in highlight else "" for n in G.nodes()],
        textposition="top center",
        hovertext=texts, hoverinfo="text",
        marker=dict(size=sizes, color=colors, line=dict(width=1, color="white")),
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        height=460, showlegend=False,
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
    )
    st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    main()
