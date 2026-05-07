#!/usr/bin/env python3
"""Daily fetch of mainstream trending topics.

Three complementary signals, configurable via `--sources`:

1. **Wikipedia top-viewed** (MediaWiki pageviews REST API): proper-noun event
   saturation. Quantitative, public, no auth. Pageviews lag 24h, so we pull
   yesterday's top.
2. **Reddit r/popular top-day** (JSON via curl, see feedback memo on httpx
   blocking): conversational/social — what English-speaking internet is
   converging on right now.
3. **4chan /pol/ catalog** (a.4cdn.org JSON): leading-edge / unfiltered
   discourse, stickies surfaced as a separate signal class. Smaller scope
   than the first two but catches things before they cross over.
4. **BBC top stories** (BBC News RSS): mainstream-press editorial
   directional signal — what the talking-class consensus is putting on the
   front page today. Different from Wikipedia (event saturation) or Reddit
   (social) in that it captures *editorial framing* of the news cycle.

Per-agent customization: agents whose specialism is a different surface
(financial: Yahoo Trending Tickers + Stocktwits; literary: Letterboxd +
Pitchfork + LRB) point `--sources` at their own list when the per-agent
matchers come online. Wikipedia stays universal as the "general public
attention" baseline; the rest is domain-tailored.

(Google Trends RSS was the original second source; the endpoint went 404
in late 2025 when Google retired it. Reddit r/popular replaced it.)

Output: per-agent JSON snapshot at the path passed via `--out`. Idempotent
(overwrites if re-run on the same day). Lives in `tools/` so any agent
container that wants to call it has it at `/workspace/tools/fetch_trending.py`.

Run from host:
  python3 tools/fetch_trending.py --out groups/velikov/trending/$(date -u +%F).json
Run from inside a container (e.g. Velikov):
  python3 /workspace/tools/fetch_trending.py --out /workspace/group/trending/$(date -u +%F).json
"""
import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

WIKI_TOP_N = 50
WIKI_EXCLUDE_PREFIXES = (
    "Special:", "Wikipedia:", "Help:", "Portal:", "Talk:", "File:",
    "Category:", "Template:", "User:", "Draft:",
)
WIKI_EXCLUDE_TITLES = {"Main_Page", "-"}

REDDIT_TOP_N = 50
# Subreddits whose top posts are pure noise from a "what's mainstream" angle.
# Filter at fetch time so the matcher doesn't waste tokens on them.
REDDIT_EXCLUDE_SUBS = {
    "r/aww", "r/MadeMeSmile", "r/funny", "r/pics", "r/mildlyinteresting",
    "r/interestingasfuck", "r/nextfuckinglevel", "r/oddlysatisfying",
    "r/BeAmazed", "r/Damnthatsinteresting", "r/HumansBeingBros",
    "r/wholesomememes", "r/memes", "r/dankmemes", "r/me_irl",
}

# 4chan /pol/ — sticky threads (always included) + top non-stickies by replies.
POL_TOP_N = 25
POL_MIN_REPLIES = 50  # baseline reply floor for non-stickies; stickies bypass

BBC_TOP_N = 20  # BBC top stories RSS gives ~30; trim to the editorial top

USER_AGENT = "NanoClaw-Trending/1.0 (contact: leo@local)"
# Reddit's bot detection treats generic / library-shaped UAs as suspicious;
# their docs mandate an app-style UA. From inside data-center IPs (containers)
# this matters more than from a residential host.
REDDIT_USER_AGENT = (
    "linux:NanoClawTrending:v1.0 (by /u/anonymous-research-bot)"
)


def fetch_wikipedia_top(date):
    """Fetch top-viewed Wikipedia articles for a given date.

    Pageviews data lags by ~24h; pass yesterday for fresh data.
    """
    y, m, d = date.strftime("%Y"), date.strftime("%m"), date.strftime("%d")
    url = (
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
        f"en.wikipedia/all-access/{y}/{m}/{d}"
    )
    r = httpx.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    items_block = data.get("items", [])
    if not items_block:
        return []
    articles = items_block[0].get("articles", [])
    out = []
    for art in articles:
        title = art.get("article", "")
        if title in WIKI_EXCLUDE_TITLES:
            continue
        if any(title.startswith(p) for p in WIKI_EXCLUDE_PREFIXES):
            continue
        out.append({
            "topic": title.replace("_", " "),
            "source": "wikipedia",
            "rank": art.get("rank"),
            "volume": art.get("views"),
            "url": f"https://en.wikipedia.org/wiki/{title}",
        })
        if len(out) >= WIKI_TOP_N:
            break
    return out


def fetch_reddit_popular():
    """Fetch r/popular top-day via Reddit's JSON listing API.

    Uses curl rather than httpx because Reddit fingerprints non-browser TLS
    clients aggressively — httpx returns 403 even with the spec-compliant
    UA from the same IP where curl returns 200. Curl is universally available
    in NanoClaw containers (confirmed via existing fetch_all_feeds.py
    yt-dlp pattern) and presents a profile Reddit doesn't block.
    """
    url = "https://www.reddit.com/r/popular/top.json?t=day&limit=100&raw_json=1"
    proc = subprocess.run(
        [
            "curl", "-sS", "--fail-with-body", "--max-time", "30",
            "-A", REDDIT_USER_AGENT,
            "-H", "Accept: application/json",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        snippet = (proc.stdout or proc.stderr).strip()[:200]
        raise RuntimeError(f"curl failed (rc={proc.returncode}): {snippet}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"json parse failed: {e}; first 200: {proc.stdout[:200]!r}")
    children = data.get("data", {}).get("children", [])
    out = []
    for child in children:
        post = child.get("data", {})
        sub = f"r/{post.get('subreddit', '')}"
        if sub in REDDIT_EXCLUDE_SUBS:
            continue
        title = (post.get("title") or "").strip()
        # Strip leading "[D]" / "[Discussion]" tag-prefixes some subs add.
        title = re.sub(r"^\[\w+\]\s+", "", title)
        permalink = post.get("permalink", "")
        out.append({
            "topic": html.unescape(title),
            "source": "reddit-popular",
            "subreddit": sub,
            "url": f"https://www.reddit.com{permalink}" if permalink else (post.get("url") or ""),
        })
        if len(out) >= REDDIT_TOP_N:
            break
    for i, item in enumerate(out):
        item["rank"] = i + 1
    return out


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip HTML tags + decode entities. 4chan comments come HTML-formatted."""
    if not text:
        return ""
    return html.unescape(_HTML_TAG_RE.sub(" ", text)).strip()


def fetch_4chan_pol():
    """Pull /pol/ catalog: stickies (always) + top-by-replies non-stickies.

    Each entry preserves `sticky` so the matcher knows whether it's
    moderator-curated or organic-volume. Closed/archived threads are dropped.
    """
    url = "https://a.4cdn.org/pol/catalog.json"
    r = httpx.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()

    stickies, regular = [], []
    for page in data:
        for t in page.get("threads", []):
            if t.get("closed") or t.get("archived"):
                continue
            no = t.get("no")
            sub = _strip_html(t.get("sub", ""))
            com = _strip_html(t.get("com", ""))
            replies = t.get("replies", 0)
            entry = {
                "topic": (sub or com[:120]).strip() or f"thread {no}",
                "source": "fourchan-pol",
                "subject": sub[:200],
                "comment": com[:400],
                "replies": replies,
                "sticky": bool(t.get("sticky")),
                "url": f"https://boards.4chan.org/pol/thread/{no}",
            }
            if entry["sticky"]:
                stickies.append(entry)
            elif replies >= POL_MIN_REPLIES:
                regular.append(entry)

    regular.sort(key=lambda x: x["replies"], reverse=True)
    out = stickies + regular[: max(POL_TOP_N - len(stickies), 0)]
    for i, item in enumerate(out):
        item["rank"] = i + 1
    return out


def fetch_bbc_top_stories():
    """BBC News top-stories RSS — the most recent BBC_TOP_N items.

    BBC's RSS is ordered most-recent-first, which doubles as their editorial
    surfacing of the news cycle. No auth, no rate-limit issues from this fetch
    cadence (once daily).
    """
    url = "https://feeds.bbci.co.uk/news/rss.xml"
    r = httpx.get(
        url, timeout=30,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"},
    )
    r.raise_for_status()
    # Lightweight RSS parsing — feedparser was removed for httpx-fingerprint
    # reasons; BBC works fine with httpx + xml parsing inline.
    import xml.etree.ElementTree as ET
    root = ET.fromstring(r.text)
    out = []
    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")[:300]
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        out.append({
            "topic": title,
            "source": "bbc-top-stories",
            "rank": len(out) + 1,
            "url": link,
            "description": desc,
            "published": pub,
        })
        if len(out) >= BBC_TOP_N:
            break
    return out


def build_snapshot(target_date):
    """Pull from all sources, return a snapshot dict.

    Per-source failures are recorded in `errors` rather than aborting — one
    upstream being down shouldn't lose the rest of the day's signal.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "fetched_at": fetched_at,
        "date": target_date.isoformat(),
        "sources": {},
        "errors": {},
    }

    # Wikipedia — yesterday relative to target_date (today's pageviews aren't
    # finalized until late next-day).
    wiki_date = target_date - timedelta(days=1)
    try:
        items = fetch_wikipedia_top(wiki_date)
        snapshot["sources"]["wikipedia"] = items
    except Exception as e:
        snapshot["errors"]["wikipedia"] = f"{type(e).__name__}: {e}"

    try:
        items = fetch_reddit_popular()
        snapshot["sources"]["reddit-popular"] = items
    except Exception as e:
        snapshot["errors"]["reddit-popular"] = f"{type(e).__name__}: {e}"

    try:
        items = fetch_4chan_pol()
        snapshot["sources"]["fourchan-pol"] = items
    except Exception as e:
        snapshot["errors"]["fourchan-pol"] = f"{type(e).__name__}: {e}"

    try:
        items = fetch_bbc_top_stories()
        snapshot["sources"]["bbc-top-stories"] = items
    except Exception as e:
        snapshot["errors"]["bbc-top-stories"] = f"{type(e).__name__}: {e}"

    return snapshot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Output JSON path.")
    ap.add_argument("--date", help="Override today (UTC). YYYY-MM-DD.")
    args = ap.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(timezone.utc).date()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(target_date)
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))

    total = sum(len(v) for v in snapshot["sources"].values())
    print(
        f"wrote {out_path}: {total} items across "
        f"{len(snapshot['sources'])} sources, {len(snapshot['errors'])} errors"
    )
    if snapshot["errors"]:
        for k, v in snapshot["errors"].items():
            print(f"  ERROR {k}: {v}")
        sys.exit(0 if total > 0 else 1)


if __name__ == "__main__":
    main()
