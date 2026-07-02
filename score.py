"""
score.py - Turn raw GitHub data into per-engineer impact scores.

Stage 2 of 3 (fetch -> score -> app).

Implements the five-pillar impact model agreed in planning:

  P1 Importance   - weight contributions by the importance of the problem solved
  P2 Meaningful   - reward signal (work others engage with), punish vanity volume
  P3 Influence    - resolving OTHERS' problems: reviews given, authors helped
  P4 Reliability  - correct/tested work; applied as a MULTIPLICATIVE guardrail
  P5 Knowledge    - not hoarding: reviewer diversity + review-graph centrality

Composite (hybrid):
  Impact = ( wI*Importance + wM*Meaningful + wF*Influence + wK*Knowledge )
           * (0.7 + 0.3 * Reliability/100)

All per-pillar scores are 0-100 (percentile-normalized across ELIGIBLE engineers),
so the composite is explainable and decomposes into a stacked bar in the dashboard.

Output: scored.json  (consumed by app.py)
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import numpy as np

RAW_FILE = Path("raw_data.json")
OUTPUT_FILE = Path("scored.json")

# --- model configuration (defaults; dashboard sliders can override live) -------
DEFAULT_WEIGHTS = {
    "importance": 0.32,
    "meaningful": 0.26,
    "influence": 0.26,
    "knowledge": 0.16,
}
RELIABILITY_FLOOR = 0.7  # guardrail: (0.7 + 0.3 * reliability)
RELIABILITY_SPAN = 0.3

# eligibility gate: below this, samples are too noisy to rank
MIN_MERGED_PRS = 3
MIN_REVIEWS = 5

BUG_LABELS = {
    "bug", "incident", "regression", "p0", "p1", "sev1", "sev2",
    "severity/critical", "severity/high", "kind/bug", "type/bug",
}

BOT_SUFFIXES = ("[bot]",)
BOT_LOGINS = {
    "dependabot", "dependabot-preview", "github-actions", "posthog-bot",
    "sentry-io", "codecov", "renovate", "snyk-bot", "greenkeeper",
    "posthog-contributions-bot",
}

TEST_PATH_HINTS = ("test", "spec", "__tests__", ".test.", "_test.", "/tests/", ".spec.")


def is_bot(login: str | None) -> bool:
    if not login:
        return True
    low = login.lower()
    return low in BOT_LOGINS or any(low.endswith(s) for s in BOT_SUFFIXES)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def L(x: float) -> float:
    """Log damping for heavy-tailed counts."""
    return math.log1p(max(x, 0.0))


def percentile_scores(values: dict[str, float]) -> dict[str, float]:
    """Map raw values -> 0-100 percentile rank across the given engineers.

    Ties share the average rank. Empty / all-equal inputs map to 0.
    """
    if not values:
        return {}
    keys = list(values.keys())
    arr = np.array([values[k] for k in keys], dtype=float)
    if np.allclose(arr, arr[0]):
        # No spread: everyone identical -> neutral 0 so it doesn't dominate.
        return {k: 0.0 for k in keys}
    order = arr.argsort()
    ranks = np.empty_like(order, dtype=float)
    # average-rank for ties
    sorted_vals = arr[order]
    i = 0
    n = len(arr)
    tmp = np.empty(n, dtype=float)
    while i < n:
        j = i
        while j + 1 < n and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            tmp[k] = avg_rank
        i = j + 1
    ranks[order] = tmp
    scaled = 100.0 * ranks / (n - 1)
    return {keys[idx]: float(scaled[idx]) for idx in range(n)}


class Engineer:
    __slots__ = ("login", "raw", "pillars", "highlights")

    def __init__(self, login: str):
        self.login = login
        self.raw = defaultdict(float)
        self.pillars = {}
        self.highlights = []


def within_window(dt: datetime | None, start: datetime) -> bool:
    return dt is not None and dt >= start


def build() -> dict:
    data = json.loads(RAW_FILE.read_text())
    meta = data["meta"]
    window_start = parse_dt(meta["window_start"])
    prs = data["pull_requests"]
    issues = data["issues"]

    eng: dict[str, Engineer] = {}

    def get(login: str) -> Engineer:
        if login not in eng:
            eng[login] = Engineer(login)
        return eng[login]

    # review graph: edge reviewer -> author, weighted by review count
    graph = nx.DiGraph()

    # median engagement is needed for the P2 "meaningful" threshold; compute in a
    # first pass over in-window merged PRs.
    engagements = []
    for pr in prs:
        if not within_window(parse_dt(pr.get("mergedAt")), window_start):
            continue
        reviews = pr.get("reviews", {}).get("nodes", [])
        review_comments = sum(r.get("comments", {}).get("totalCount", 0) for r in reviews)
        engagements.append(
            review_comments
            + 0.5 * len(reviews)
            + pr.get("reactions", {}).get("totalCount", 0)
        )
    median_engagement = float(np.median(engagements)) if engagements else 0.0

    # --- main PR pass ---------------------------------------------------------
    for pr in prs:
        author = (pr.get("author") or {}).get("login")
        created = parse_dt(pr.get("createdAt"))
        merged_at = parse_dt(pr.get("mergedAt"))
        is_merged = pr.get("merged", False)

        pr_labels = {l["name"].lower() for l in pr.get("labels", {}).get("nodes", [])}
        bugflag = 1 if (pr_labels & BUG_LABELS) else 0
        reviews = pr.get("reviews", {}).get("nodes", [])
        files = [f["path"] for f in pr.get("files", {}).get("nodes", [])]

        # -- P3/P5: reviews GIVEN (attributed to reviewers, for any PR in window)
        if author and not is_bot(author):
            reviewers_here = set()
            for r in reviews:
                rlogin = (r.get("author") or {}).get("login")
                submitted = parse_dt(r.get("submittedAt"))
                if is_bot(rlogin) or rlogin == author:
                    continue
                if not within_window(submitted, window_start):
                    continue
                substantive = (
                    (r.get("bodyText") or "").strip() != ""
                    or r.get("comments", {}).get("totalCount", 0) > 0
                    or r.get("state") == "CHANGES_REQUESTED"
                )
                reviewer = get(rlogin)
                if substantive:
                    reviewer.raw["reviews_others"] += 1
                    reviewer.raw["_authors_helped_set"] = reviewer.raw.get(
                        "_authors_helped_set", 0
                    )
                    reviewers_here.add(rlogin)
                    graph.add_edge(rlogin, author,
                                   weight=graph.get_edge_data(rlogin, author, {}).get("weight", 0) + 1)
                if r.get("state") == "APPROVED":
                    # reviewer diversity accrues to the PR AUTHOR
                    get(author).raw.setdefault("_approver_set", set())
                    get(author).raw["_approver_set"].add(rlogin)
            # track distinct authors helped per reviewer
            for rlogin in reviewers_here:
                get(rlogin).raw.setdefault("_helped_set", set())
                get(rlogin).raw["_helped_set"].add(author)

        # Everything below is about the AUTHOR's own merged work in-window
        if not author or is_bot(author):
            continue

        e = get(author)

        if is_merged and within_window(merged_at, window_start):
            e.raw["merged_prs"] += 1
        if within_window(created, window_start):
            e.raw["opened_prs"] += 1
            # activity weeks for consistency (not a pillar but useful context)
            e.raw.setdefault("_weeks", set())
            e.raw["_weeks"].add(created.isocalendar()[1])

        # -- P1 Importance: weight by importance of closed issues
        closing = pr.get("closingIssuesReferences", {}).get("nodes", [])
        pr_importance = 0.0
        top_issue = None
        for iss in closing:
            reactions = iss.get("reactions", {}).get("totalCount", 0)
            comments = iss.get("comments", {}).get("totalCount", 0)
            i_created = parse_dt(iss.get("createdAt"))
            i_closed = parse_dt(iss.get("closedAt"))
            age_days = 0.0
            if i_created and i_closed:
                age_days = max((i_closed - i_created).total_seconds() / 86400.0, 0.0)
            score = reactions + 0.5 * comments + 0.3 * age_days
            pr_importance += score
            if top_issue is None or score > top_issue[1]:
                top_issue = (iss["number"], score, reactions, age_days)
        pr_importance *= (1 + bugflag)
        if pr_importance == 0 and bugflag:
            pr_importance = 1.0  # unlinked but labeled bug fix gets a small base
        if is_merged and within_window(merged_at, window_start):
            e.raw["importance"] += pr_importance
            if top_issue and top_issue[1] > 0:
                e.highlights.append({
                    "type": "importance",
                    "text": f"Closed #{top_issue[0]} ({top_issue[2]} reactions, open {top_issue[3]:.0f}d)",
                    "value": pr_importance,
                    "pr": pr["number"],
                })

        # -- P2 Meaningful
        if within_window(created, window_start):
            review_comments = sum(r.get("comments", {}).get("totalCount", 0) for r in reviews)
            engagement = (
                review_comments + 0.5 * len(reviews)
                + pr.get("reactions", {}).get("totalCount", 0)
            )
            linked = 1 if closing else 0
            if is_merged and (linked or engagement >= median_engagement):
                e.raw["meaningful_prs"] += 1

        # -- P4 Reliability signals
        if is_merged and within_window(merged_at, window_start):
            # test presence
            if any(any(h in f.lower() for h in TEST_PATH_HINTS) for f in files):
                e.raw["prs_with_tests"] += 1
            # CI first-pass proxy: last-commit rollup SUCCESS
            rollup = None
            cnodes = pr.get("commits", {}).get("nodes", [])
            if cnodes:
                rollup = (cnodes[0].get("commit", {}) or {}).get("statusCheckRollup")
            if rollup is not None:
                e.raw["ci_evaluated"] += 1
                if rollup.get("state") == "SUCCESS":
                    e.raw["ci_success"] += 1

        # -- P4 revert / hotfix detection (heuristic, title-based)
        title = (pr.get("title") or "").lower()
        if title.startswith("revert"):
            # the reverted author loses a point if we can find them later; here we
            # just flag reverts in the window. Attribution handled in second pass.
            pass

    # -- second pass: revert/hotfix attribution -------------------------------
    # Build quick lookup of merged PRs by number and by touched files+time.
    reverted_numbers = set()
    for pr in prs:
        title = (pr.get("title") or "").lower()
        if title.startswith('revert "') or title.startswith("revert:"):
            # try to extract the reverted PR/title; count as a defect signal for
            # whoever authored a merged PR with a matching title.
            reverted_numbers.add(pr["number"])
    # Attribute reverts: a merged PR whose title appears inside a later revert PR.
    title_to_author = {}
    for pr in prs:
        a = (pr.get("author") or {}).get("login")
        if a and not is_bot(a) and pr.get("merged"):
            title_to_author[(pr.get("title") or "").strip().lower()] = a
    for pr in prs:
        t = (pr.get("title") or "").lower()
        if t.startswith("revert"):
            for orig_title, orig_author in title_to_author.items():
                if orig_title and orig_title in t and orig_author in eng:
                    eng[orig_author].raw["reverted"] += 1
                    break

    # -- finalize set-based signals -------------------------------------------
    for e in eng.values():
        helped = e.raw.pop("_helped_set", set())
        e.raw["distinct_authors_helped"] = len(helped) if isinstance(helped, set) else 0
        approvers = e.raw.pop("_approver_set", set())
        e.raw["reviewer_diversity"] = len(approvers) if isinstance(approvers, set) else 0
        weeks = e.raw.pop("_weeks", set())
        e.raw["active_weeks"] = len(weeks) if isinstance(weeks, set) else 0
        e.raw.pop("_authors_helped_set", None)

    # -- issues closed for others (P3) ----------------------------------------
    for iss in issues:
        opener = (iss.get("author") or {}).get("login")
        closed_events = iss.get("timelineItems", {}).get("nodes", [])
        for ev in closed_events:
            actor = (ev.get("actor") or {}).get("login")
            closed_at = parse_dt(ev.get("createdAt"))
            if is_bot(actor) or not within_window(closed_at, window_start):
                continue
            e = get(actor)
            e.raw["issues_closed"] += 1
            if opener and opener != actor and not is_bot(opener):
                e.raw["others_issues_closed"] += 1

    # -- graph centralities (P5) ----------------------------------------------
    if graph.number_of_nodes() >= 3:
        betweenness = nx.betweenness_centrality(graph, weight="weight")
        out_deg = {n: graph.out_degree(n) for n in graph.nodes()}
    else:
        betweenness = {}
        out_deg = {}
    for login, b in betweenness.items():
        get(login).raw["betweenness"] = b
    for login, d in out_deg.items():
        get(login).raw["out_degree_distinct"] = d

    # -- eligibility gate ------------------------------------------------------
    eligible = {
        login: e for login, e in eng.items()
        if e.raw.get("merged_prs", 0) >= MIN_MERGED_PRS
        or e.raw.get("reviews_others", 0) >= MIN_REVIEWS
    }

    if not eligible:
        raise SystemExit("No eligible engineers found. Check raw_data.json.")

    # -- compute pillar percentiles -------------------------------------------
    def raw_map(key: str, transform=lambda x: x) -> dict[str, float]:
        return {lg: transform(e.raw.get(key, 0.0)) for lg, e in eligible.items()}

    # P1 Importance
    imp_pct = percentile_scores(raw_map("importance", L))

    # P2 Meaningful: blend volume^0.6 * ratio^0.4
    meaningful_vol_pct = percentile_scores(raw_map("meaningful_prs", L))
    quality_ratio = {}
    for lg, e in eligible.items():
        opened = e.raw.get("opened_prs", 0)
        quality_ratio[lg] = (e.raw.get("meaningful_prs", 0) / opened) if opened else 0.0
    ratio_pct = percentile_scores(quality_ratio)
    meaningful_pct = {
        lg: (meaningful_vol_pct.get(lg, 0) ** 0.6) * (ratio_pct.get(lg, 0) ** 0.4)
        for lg in eligible
    }

    # P3 Influence
    infl_raw = {}
    for lg, e in eligible.items():
        infl_raw[lg] = (
            0.40 * L(e.raw.get("reviews_others", 0))
            + 0.35 * L(e.raw.get("distinct_authors_helped", 0))
            + 0.25 * L(e.raw.get("others_issues_closed", 0))
        )
    infl_pct_base = percentile_scores(infl_raw)
    influence_pct = {}
    for lg, e in eligible.items():
        closed = e.raw.get("issues_closed", 0)
        own_ratio = 0.0
        if closed:
            own = closed - e.raw.get("others_issues_closed", 0)
            own_ratio = max(min(own / closed, 1.0), 0.0)
        influence_pct[lg] = infl_pct_base.get(lg, 0) * (1 - 0.2 * own_ratio)

    # P4 Reliability (0-100), lower defect_rate is better
    defect_rate = {}
    for lg, e in eligible.items():
        merged = max(e.raw.get("merged_prs", 0), 1)
        defect_rate[lg] = e.raw.get("reverted", 0) / merged
    defect_pct = percentile_scores(defect_rate)  # high = worse
    ci_rate = {}
    test_rate = {}
    for lg, e in eligible.items():
        ci_eval = e.raw.get("ci_evaluated", 0)
        ci_rate[lg] = (e.raw.get("ci_success", 0) / ci_eval) if ci_eval else 0.5
        merged = max(e.raw.get("merged_prs", 0), 1)
        test_rate[lg] = e.raw.get("prs_with_tests", 0) / merged
    ci_pct = percentile_scores(ci_rate)
    test_pct = percentile_scores(test_rate)
    reliability_pct = {
        lg: 0.45 * (100 - defect_pct.get(lg, 0))
        + 0.35 * ci_pct.get(lg, 0)
        + 0.20 * test_pct.get(lg, 0)
        for lg in eligible
    }

    # P5 Knowledge
    div_pct = percentile_scores(raw_map("reviewer_diversity"))
    betw_pct = percentile_scores(raw_map("betweenness"))
    outdeg_pct = percentile_scores(raw_map("out_degree_distinct"))
    knowledge_pct = {
        lg: 0.45 * div_pct.get(lg, 0)
        + 0.35 * betw_pct.get(lg, 0)
        + 0.20 * outdeg_pct.get(lg, 0)
        for lg in eligible
    }

    # -- assemble per-engineer records ----------------------------------------
    records = []
    for lg, e in eligible.items():
        pillars = {
            "importance": round(imp_pct.get(lg, 0), 1),
            "meaningful": round(meaningful_pct.get(lg, 0), 1),
            "influence": round(influence_pct.get(lg, 0), 1),
            "reliability": round(reliability_pct.get(lg, 0), 1),
            "knowledge": round(knowledge_pct.get(lg, 0), 1),
        }
        e.highlights.sort(key=lambda h: h.get("value", 0), reverse=True)
        records.append({
            "login": lg,
            "pillars": pillars,
            "raw": {k: (round(v, 3) if isinstance(v, float) else v)
                     for k, v in e.raw.items() if not k.startswith("_")},
            "highlights": e.highlights[:3],
        })

    graph_export = {
        "nodes": [{"id": n} for n in graph.nodes() if n in eligible],
        "edges": [
            {"source": u, "target": v, "weight": d.get("weight", 1)}
            for u, v, d in graph.edges(data=True)
            if u in eligible and v in eligible
        ],
    }

    result = {
        "meta": meta,
        "weights": DEFAULT_WEIGHTS,
        "reliability_guardrail": {"floor": RELIABILITY_FLOOR, "span": RELIABILITY_SPAN},
        "engineers": records,
        "graph": graph_export,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


def compute_composite(record: dict, weights: dict, floor: float, span: float) -> float:
    p = record["pillars"]
    base = (
        weights["importance"] * p["importance"]
        + weights["meaningful"] * p["meaningful"]
        + weights["influence"] * p["influence"]
        + weights["knowledge"] * p["knowledge"]
    )
    guardrail = floor + span * (p["reliability"] / 100.0)
    return base * guardrail


def main() -> None:
    result = build()
    w = result["weights"]
    g = result["reliability_guardrail"]
    for rec in result["engineers"]:
        rec["impact"] = round(compute_composite(rec, w, g["floor"], g["span"]), 2)
    result["engineers"].sort(key=lambda r: r["impact"], reverse=True)
    OUTPUT_FILE.write_text(json.dumps(result, indent=2))
    print(f"Wrote {OUTPUT_FILE} with {len(result['engineers'])} eligible engineers.")
    print("\nTop 5 by impact:")
    for rec in result["engineers"][:5]:
        p = rec["pillars"]
        print(
            f"  {rec['impact']:6.2f}  {rec['login']:<20} "
            f"imp={p['importance']:.0f} mean={p['meaningful']:.0f} "
            f"infl={p['influence']:.0f} rel={p['reliability']:.0f} know={p['knowledge']:.0f}"
        )


if __name__ == "__main__":
    main()
