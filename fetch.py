"""
fetch.py - Pull raw GitHub data for the impact analysis.

Stage 1 of 3 (fetch -> score -> app).

Uses the GitHub GraphQL API to pull, for the last LOOKBACK_DAYS (>=90) days:
  - Pull requests: author, timestamps, merge state, labels, changed files,
    reviews (author/state/body), review threads, linked/closing issues,
    reactions, and CI status roll-up.
  - Issues: author, timestamps, labels, reactions, comments, and who closed them.

Design notes:
  - Pagination is cursor based, ordered by UPDATED_AT DESC, and we stop paging
    once records fall entirely outside the lookback window.
  - Every response is cached to .cache/ keyed by a hash of the query+variables,
    so re-runs are free and resumable.
  - We watch the GraphQL rateLimit budget and sleep until reset when low.

Output: raw_data.json  (consumed by score.py)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = os.getenv("REPO_OWNER", "PostHog")
REPO_NAME = os.getenv("REPO_NAME", "posthog")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "90"))

API_URL = "https://api.github.com/graphql"
CACHE_DIR = Path(".cache")
OUTPUT_FILE = Path("raw_data.json")

# We keep a small buffer beyond the strict window so score.py can detect
# follow-up fixes / reverts that happen shortly after the window edge.
WINDOW_START = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

BOT_SUFFIXES = ("[bot]",)
BOT_LOGINS = {
    "dependabot",
    "dependabot-preview",
    "github-actions",
    "posthog-bot",
    "sentry-io",
    "codecov",
    "renovate",
    "snyk-bot",
    "greenkeeper",
    "posthog-contributions-bot",
}


def is_bot(login: str | None) -> bool:
    if not login:
        return True
    low = login.lower()
    return low in BOT_LOGINS or any(low.endswith(s) for s in BOT_SUFFIXES)


def _headers() -> dict:
    if not GITHUB_TOKEN:
        sys.exit(
            "ERROR: GITHUB_TOKEN not set. Copy .env.example to .env and add a token."
        )
    return {
        "Authorization": f"bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }


def _cache_key(query: str, variables: dict) -> Path:
    raw = json.dumps({"q": query, "v": variables}, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.json"


def run_query(query: str, variables: dict, use_cache: bool = True) -> dict:
    """Execute one GraphQL query with caching, retries, and rate-limit back-off."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = _cache_key(query, variables)
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())

    for attempt in range(8):
        resp = requests.post(
            API_URL,
            headers=_headers(),
            json={"query": query, "variables": variables},
            timeout=60,
        )
        if resp.status_code in (502, 503, 504):
            wait = 2 ** attempt
            print(f"  server {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                # GitHub intermittently returns an empty/non-JSON 200 body under
                # load. Treat as transient and retry with back-off.
                wait = 2 ** attempt
                print(f"  empty/non-JSON 200 body, retrying in {wait}s...")
                time.sleep(wait)
                continue
            if "errors" in payload:
                # A partial data payload can still be useful; surface but continue.
                msg = json.dumps(payload["errors"])[:300]
                if payload.get("data") is None:
                    raise RuntimeError(f"GraphQL error (no data): {msg}")
                print(f"  GraphQL warning: {msg}")
            _respect_rate_limit(payload.get("data", {}))
            cache_path.write_text(json.dumps(payload))
            return payload
        if resp.status_code == 403:
            print("  403 (secondary rate limit). Sleeping 60s...")
            time.sleep(60)
            continue
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError("Exhausted retries for GraphQL query.")


def _respect_rate_limit(data: dict) -> None:
    rl = data.get("rateLimit")
    if not rl:
        return
    remaining, cost = rl.get("remaining", 9999), rl.get("cost", 1)
    if remaining < max(cost * 3, 50):
        reset = datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00"))
        sleep_s = max((reset - datetime.now(timezone.utc)).total_seconds(), 0) + 5
        print(f"  rate limit low ({remaining} left). Sleeping {sleep_s:.0f}s...")
        time.sleep(sleep_s)


RATE_LIMIT_FRAGMENT = "rateLimit { cost remaining resetAt }"

PR_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  %s
  repository(owner:$owner, name:$name) {
    pullRequests(first:40, after:$cursor, orderBy:{field:CREATED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        author { login }
        createdAt
        updatedAt
        mergedAt
        closedAt
        merged
        additions
        deletions
        changedFiles
        labels(first:20) { nodes { name } }
        reactions { totalCount }
        files(first:100) { nodes { path } }
        reviews(first:40) {
          nodes {
            author { login }
            state
            bodyText
            submittedAt
            comments { totalCount }
          }
        }
        closingIssuesReferences(first:10) {
          nodes {
            number
            createdAt
            closedAt
            author { login }
            reactions { totalCount }
            comments { totalCount }
            labels(first:20) { nodes { name } }
          }
        }
        commits(last:1) {
          nodes {
            commit {
              statusCheckRollup { state }
            }
          }
        }
      }
    }
  }
}
""" % RATE_LIMIT_FRAGMENT

ISSUE_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  %s
  repository(owner:$owner, name:$name) {
    issues(first:50, after:$cursor, orderBy:{field:CREATED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        author { login }
        createdAt
        closedAt
        state
        reactions { totalCount }
        comments { totalCount }
        labels(first:20) { nodes { name } }
        timelineItems(last:20, itemTypes:[CLOSED_EVENT]) {
          nodes {
            ... on ClosedEvent {
              actor { login }
              createdAt
            }
          }
        }
      }
    }
  }
}
""" % RATE_LIMIT_FRAGMENT


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_pull_requests() -> list[dict]:
    """Fetch PRs created within the window.

    Ordered by CREATED_AT DESC so createdAt strictly decreases; we stop as soon
    as a page's oldest createdAt falls before the window. PRs created before the
    window (even if merged inside it) are intentionally out of scope - a bounded,
    deterministic definition of "the last N days" that avoids the UPDATED_AT
    explosion where old PRs get bumped back into recency by stray comments.
    """
    print(f"Fetching pull requests created since {WINDOW_START.date()}...")
    cursor, out, page = None, [], 0
    while True:
        page += 1
        payload = run_query(
            PR_QUERY,
            {"owner": REPO_OWNER, "name": REPO_NAME, "cursor": cursor},
        )
        conn = payload["data"]["repository"]["pullRequests"]
        nodes = conn["nodes"]
        # keep only in-window records; a page may straddle the boundary
        in_window = [n for n in nodes if _parse_dt(n["createdAt"]) >= WINDOW_START]
        out.extend(in_window)
        oldest = min((_parse_dt(n["createdAt"]) for n in nodes), default=None)
        print(f"  page {page}: +{len(in_window)} PRs (total {len(out)}), oldest {oldest.date() if oldest else 'n/a'}")
        if oldest and oldest < WINDOW_START:
            break
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return out


def fetch_issues() -> list[dict]:
    print(f"Fetching issues created since {WINDOW_START.date()}...")
    cursor, out, page = None, [], 0
    while True:
        page += 1
        payload = run_query(
            ISSUE_QUERY,
            {"owner": REPO_OWNER, "name": REPO_NAME, "cursor": cursor},
        )
        conn = payload["data"]["repository"]["issues"]
        nodes = conn["nodes"]
        in_window = [n for n in nodes if _parse_dt(n["createdAt"]) >= WINDOW_START]
        out.extend(in_window)
        oldest = min((_parse_dt(n["createdAt"]) for n in nodes), default=None)
        print(f"  page {page}: +{len(in_window)} issues (total {len(out)}), oldest {oldest.date() if oldest else 'n/a'}")
        if oldest and oldest < WINDOW_START:
            break
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return out


def main() -> None:
    started = time.time()
    prs = fetch_pull_requests()
    issues = fetch_issues()

    meta = {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "lookback_days": LOOKBACK_DAYS,
        "window_start": WINDOW_START.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"pull_requests": len(prs), "issues": len(issues)},
    }
    OUTPUT_FILE.write_text(
        json.dumps({"meta": meta, "pull_requests": prs, "issues": issues}, indent=2)
    )
    print(
        f"\nWrote {OUTPUT_FILE} "
        f"({len(prs)} PRs, {len(issues)} issues) in {time.time()-started:.0f}s"
    )


if __name__ == "__main__":
    main()
