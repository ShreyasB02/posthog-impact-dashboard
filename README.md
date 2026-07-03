# PostHog Engineer Impact Dashboard

Identifies the **top 5 most impactful engineers** in [`PostHog/posthog`](https://github.com/PostHog/posthog)
over the last 90 days, and explains *why* on a single laptop screen.

Latest run analyzed **13,839 pull requests and 607 issues** created in the window, across
**149 eligible engineers** (after bots and low-activity accounts are filtered).

The core belief: **impact is not lines of code, commits, or files changed.** Those measure motion,
not value. This project models impact from what the *team* engages with and the problems that
actually got solved.

## The impact model

Five pillars, each scored as a 0-100 percentile across eligible engineers. Four are *compensating*
strengths combined as a weighted sum; **Reliability is a multiplicative guardrail** so brilliant-but-
breaks-everything work can't buy its way to the top.

```
Impact = ( 0.32*Importance + 0.26*Meaningful + 0.26*Influence + 0.16*Knowledge )
         * (0.7 + 0.3 * Reliability/100)
```

| Pillar | Question it answers | Key signals |
|---|---|---|
| **Problem Importance** | Did they solve problems that mattered? | reactions + comments on closed issues, how long the pain was open, bug/incident labels |
| **Meaningful Work** | Is the work substantive, not vanity volume? | PRs the team engages with, with a ratio penalty on padding |
| **Helping Others** | Do they lift the whole team, not just self? | substantive reviews given to others' PRs, distinct teammates helped, others' issues resolved |
| **Reliability** *(guardrail)* | Is their work correct and tested? | low revert rate, CI first-pass, tests present |
| **Knowledge Sharing** | Do they spread knowledge vs hoard it? | reviewer diversity on their PRs, betweenness centrality in the review network |

### Why these choices

- **Percentile normalization** with log-damping (`ln(1+x)`) keeps a few mega-accounts from flattening
  everyone else on this very high-traffic repo.
- **Eligibility gate** (`>=3 merged PRs OR >=5 reviews`) removes small-sample noise from the ranking.
- **Bots filtered** (`*[bot]`, dependabot, github-actions, posthog-bot, etc.).
- **Reliability as a multiplier, not an addend**: a chronically-reverting engineer gets scaled down no
  matter how flashy their features. This directly encodes "don't ruin other people's time."

See exact formulas in [score.py](score.py).

## Architecture

Three decoupled stages so weight-tuning never re-hits the API:

```
fetch.py  ->  raw_data.json  ->  score.py  ->  scored.json  ->  app.py (Streamlit)
```

The dashboard reads the committed `scored.json`, so the hosted app holds no secrets and loads instantly.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then add a GitHub token (public_repo / public read scope)

python fetch.py           # pulls 90+ days from PostHog/posthog -> raw_data.json
python score.py           # computes pillars + composite   -> scored.json
streamlit run app.py      # opens the dashboard
```

A GitHub personal access token is only needed for `fetch.py`. Create one at
<https://github.com/settings/tokens>.

## Scope

`fetch.py` pulls all PRs and issues **created** in the last 90 days (ordered by creation date so
pagination terminates deterministically at the window edge). PRs created before the window but merged
inside it are out of scope - a bounded, reproducible definition that avoids the `UPDATED_AT` explosion
where old PRs get bumped into recency by stray comments.

## Notes / limitations

- **Revert detection** is heuristic (title-based) and sparse (43 reverts across the window). It still
  works as a penalty: engineers with reverts are visibly pulled down by the Reliability guardrail.
- **CI first-pass** turned out to be a dense, reliable signal (available for ~99% of merged PRs), so
  Reliability rests mostly on it plus test presence.
- **Problem Importance** is concentrated (only engineers who close reacted-to/linked issues score
  highly) - this is by design; it is meant to be the differentiator for "solved a real problem."
- The model is transparent and tunable: the dashboard sidebar exposes the pillar weights so an
  engineering leader can re-rank live and see the methodology respond.


## Web Link:
https://posthog-dashboard-sbattula.streamlit.app/
