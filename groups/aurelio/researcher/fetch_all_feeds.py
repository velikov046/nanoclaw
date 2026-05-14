#!/usr/bin/env python3
"""Fetch every RSS feed in researcher/feeds.json once and write a structured cache.

Subsequent pipelines (pulse.py, find.py, rabbit.py) read the cache instead of
re-fetching, saving bandwidth and LLM tokens.

Cache schema:
{
  "fetched_at": "ISO-8601 UTC",
  "list":       "all" | "sources" | "master",
  "feeds": {
    "<source name>": {
      "url": "...",
      "type": "rss",
      "source_list": "sources" | "master",
      "category": "...",
      "items": [{title, url, summary, published}, ...]
    },
    ...
  },
  "errors": {"<source name>": "<error>"}   // optional, present when fetches fail
}

Run from host:    python3 groups/aurelio/researcher/fetch_all_feeds.py [--list master|sources|all]
Inside container: python3 /workspace/group/researcher/fetch_all_feeds.py [--list ...]
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx

HERE = Path(__file__).parent
GROUP = HERE.parent
FEEDS_FILE = HERE / 'feeds.json'

RSS_MAX_ITEMS = 15
HTTP_TIMEOUT = 20
FETCH_WORKERS = 10
RETRY_ATTEMPTS = 2
RETRY_BACKOFF = 3  # seconds between retries

USER_AGENT = "Mozilla/5.0 (compatible; AurelioFeedCache/1.0)"


def _strip_html(s):
    return re.sub(r'<[^>]+>', '', s or '').strip()


def fetch_rss(url):
    # Fetch via httpx (verify=False) so OneCLI proxy's gateway CA doesn't block urllib.
    # Same reason yt-dlp uses --no-check-certificates — proxy validates upstream.
    resp = httpx.get(url, headers={'User-Agent': USER_AGENT}, timeout=HTTP_TIMEOUT,
                     verify=False, follow_redirects=True)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"feedparser bozo: {parsed.bozo_exception}")
    items = []
    for entry in parsed.entries[:RSS_MAX_ITEMS]:
        items.append({
            'title': str(entry.get('title') or '').strip(),
            'url': str(entry.get('link') or ''),
            'summary': _strip_html(str(entry.get('summary') or ''))[:500],
            'published': str(entry.get('published') or entry.get('updated') or ''),
        })
    return items


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    msg = str(exc).lower()
    return 'timed out' in msg or 'timeout' in msg or 'connection' in msg


def _fetch_one(feed):
    rss_url = feed.get('rss_url') or feed['url']
    entry = {
        'url': feed['url'],
        'rss_url': feed.get('rss_url'),
        'type': feed.get('type', 'rss'),
        'source_list': feed.get('source_list', ''),
        'category': feed.get('category', ''),
        'items': [],
    }
    typ = entry['type']
    if typ != 'rss':
        # html / hash-based change detection not implemented yet
        return feed['name'], entry, f"type={typ} not yet handled"
    last_err = None
    for attempt in range(1 + RETRY_ATTEMPTS):
        try:
            entry['items'] = fetch_rss(rss_url)
            return feed['name'], entry, None
        except Exception as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS and _is_retryable(e):
                time.sleep(RETRY_BACKOFF * (attempt + 1))
            else:
                break
    return feed['name'], entry, str(last_err)[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', choices=['all', 'sources', 'master'], default='all',
                    help="Which source list to fetch. Default: all.")
    ap.add_argument('--out', help="Override output path. Default: feed_cache[_<list>].json at group root.")
    args = ap.parse_args()

    feeds = json.loads(FEEDS_FILE.read_text())
    if args.list != 'all':
        feeds = [f for f in feeds if f.get('source_list') == args.list]
        if not feeds:
            print(f"No feeds tagged source_list={args.list!r}", file=sys.stderr)
            sys.exit(1)

    suffix = '' if args.list == 'all' else f'_{args.list}'
    out_file = Path(args.out) if args.out else GROUP / f'feed_cache{suffix}.json'

    result = {
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'list': args.list,
        'feeds': {},
    }
    errors = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch_one, f): f for f in feeds}
        for fut in as_completed(futures):
            name, entry, err = fut.result()
            result['feeds'][name] = entry
            if err:
                errors[name] = err
                print(f"  ✗ {name:42} {entry['type']:5} ERROR: {err}", file=sys.stderr)
            else:
                print(f"  ✓ {name:42} {entry['type']:5} {len(entry['items']):3} items", file=sys.stderr)

    if errors:
        result['errors'] = errors

    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nWrote {out_file} ({out_file.stat().st_size} bytes)", file=sys.stderr)
    print(f"  feeds: {len(result['feeds'])}", file=sys.stderr)
    print(f"  errors: {len(errors)}", file=sys.stderr)


if __name__ == '__main__':
    main()
