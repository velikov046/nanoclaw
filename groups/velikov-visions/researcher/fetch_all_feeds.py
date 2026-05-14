#!/usr/bin/env python3
"""Fetch every feed in feeds.json once and write a structured cache.

Subsequent pipelines (Russel Review, Respectable Reading, Visions Digest) read from
the cache instead of independently fetching, saving bandwidth and LLM tokens.

Cache schema:
{
  "fetched_at": "ISO-8601 UTC",
  "feeds": {
    "<source name>": {
      "tag": "...",
      "geo": "...",
      "type": "rss|youtube|4chan",
      "url": "...",
      "items": [...]   // shape varies by type
    },
    ...
  }
}

RSS items: {title, url, summary, published}
YouTube items: {title, url, video_id, channel, published, transcript}
  — transcript is the full extracted text from auto-subs (English), stripped of timestamps
4chan items: {no, subject, comment, replies, url}

Run from host: python3 groups/velikov/researcher/fetch_all_feeds.py
Run inside container: python3 /workspace/group/researcher/fetch_all_feeds.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx

HERE = Path(__file__).parent
# Source list — researcher/sources.json has {feeds, websites}, news/feeds.json has flat list.
# Prefer the flat list since we just want feeds.
SOURCES_FILE = HERE / 'sources.json'
NEWS_FEEDS_FILE = HERE.parent / 'news' / 'feeds.json'
CACHE_FILE = HERE.parent / 'feed_cache.json'  # /workspace/group/feed_cache.json from velikov container


def _load_feeds():
    if NEWS_FEEDS_FILE.exists():
        d = json.load(open(NEWS_FEEDS_FILE))
        if isinstance(d, list):
            return d
    if SOURCES_FILE.exists():
        d = json.load(open(SOURCES_FILE))
        if isinstance(d, dict) and 'feeds' in d:
            return d['feeds']
    raise FileNotFoundError("Could not find feeds list (looked in news/feeds.json and researcher/sources.json)")

RSS_MAX_ITEMS = 15
YT_MAX_VIDEOS = 3
CHAN_MIN_REPLIES = 30
CHAN_MAX_THREADS = 20
HTTP_TIMEOUT = 20

USER_AGENT = "Mozilla/5.0 (compatible; VelikovFeedCache/1.0)"


def _find_yt_cookies():
    """Locate a YouTube cookies file (Netscape format) or return None.

    Without cookies, yt-dlp gets the "Sign in to confirm you're not a bot"
    block on channel feeds. Lookup order: $YT_COOKIES, per-group override,
    shared global file (works for any agent that runs YouTube ingest).
    """
    env = os.environ.get('YT_COOKIES')
    if env and os.path.exists(env):
        return env
    group_root = HERE.parent  # /workspace/group or groups/<agent>
    candidates = [
        group_root / 'cookies.txt',
        group_root / 'cookies(3).txt',
        Path('/workspace/global/youtube_cookies.txt'),         # in-container shared
        group_root.parent / 'global' / 'youtube_cookies.txt',  # host-side
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _strip_html(s):
    return re.sub(r'<[^>]+>', '', s or '').strip()


def _parse_vtt(vtt_text):
    """Strip WebVTT to plain transcript text (dedupe + remove timestamps)."""
    lines = []
    seen = set()
    for line in vtt_text.split('\n'):
        line = line.strip()
        if not line or '-->' in line or line.startswith('WEBVTT') or \
           line.startswith('Kind:') or line.startswith('Language:') or \
           re.match(r'^\d+$', line) or 'align:' in line:
            continue
        line = re.sub(r'<[^>]+>', '', line)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return ' '.join(lines)


def fetch_rss(url):
    # Fetch via httpx (verify=False) so OneCLI proxy's gateway CA doesn't block urllib.
    # Same reason yt-dlp uses --no-check-certificates — proxy validates upstream.
    headers = {'User-Agent': USER_AGENT}
    resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT, verify=False, follow_redirects=True)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"feedparser bozo: {parsed.bozo_exception}")
    items = []
    for entry in parsed.entries[:RSS_MAX_ITEMS]:
        items.append({
            'title': entry.get('title', '').strip(),
            'url': entry.get('link', ''),
            'summary': _strip_html(entry.get('summary', ''))[:500],
            'published': entry.get('published', '') or entry.get('updated', ''),
        })
    return items


def fetch_youtube(channel_url):
    """Use yt-dlp to flat-list latest videos and pull auto-subs."""
    # Find yt-dlp on host or container
    yt_dlp = None
    for candidate in ['yt-dlp', '/home/aurellian/.local/bin/yt-dlp', '/usr/local/bin/yt-dlp']:
        try:
            r = subprocess.run([candidate, '--version'], capture_output=True, timeout=5)
            if r.returncode == 0:
                yt_dlp = candidate
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    if not yt_dlp:
        raise RuntimeError("yt-dlp not found")

    # yt-dlp ships with certifi and prefers certifi's Mozilla bundle over SSL_CERT_FILE,
    # so it can't see OneCLI's gateway CA. Skip cert checking — OneCLI proxy validates upstream.
    items = []
    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, '%(id)s.%(ext)s')
        cmd = [
            yt_dlp,
            '--no-check-certificates',
            '--write-auto-subs', '--sub-langs', 'en', '--skip-download',
            '--sub-format', 'vtt',
            '--ignore-no-formats-error',
            '--playlist-items', f'1:{YT_MAX_VIDEOS}',
            '--print-to-file', '%(id)s\t%(title)s\t%(channel)s\t%(upload_date)s', os.path.join(tmpdir, '_meta.tsv'),
            '-o', out_template,
        ]
        cookies = _find_yt_cookies()
        if cookies:
            # yt-dlp rewrites the cookies file at end of run to persist refreshed
            # session cookies. groups/global is mounted RO in non-main containers,
            # so write back to the source path fails. Copy into tmpdir first.
            cookies_rw = os.path.join(tmpdir, 'cookies.txt')
            shutil.copy2(cookies, cookies_rw)
            cmd += ['--cookies', cookies_rw]
        cmd.append(channel_url)
        r = subprocess.run(cmd, capture_output=True, timeout=180, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"yt-dlp exit {r.returncode}: {r.stderr[-500:]}")

        meta_path = os.path.join(tmpdir, '_meta.tsv')
        if not os.path.exists(meta_path):
            return items
        for line in open(meta_path):
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 4:
                continue
            video_id, title, channel, upload_date = parts[:4]
            vtt_path = os.path.join(tmpdir, f'{video_id}.en.vtt')
            transcript = ''
            if os.path.exists(vtt_path):
                with open(vtt_path) as f:
                    transcript = _parse_vtt(f.read())
            items.append({
                'title': title,
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'video_id': video_id,
                'channel': channel,
                'published': upload_date,
                'transcript': transcript[:8000],   # cap to keep cache reasonable
            })
    return items


def fetch_4chan(catalog_url):
    """4chan catalog.json → list of threads above reply threshold."""
    headers = {'User-Agent': USER_AGENT}
    r = httpx.get(catalog_url, headers=headers, timeout=HTTP_TIMEOUT, verify=False)
    r.raise_for_status()
    data = r.json()
    board = urlparse(catalog_url).path.strip('/').split('/')[0]
    threads = []
    for page in data:
        for t in page.get('threads', []):
            replies = t.get('replies', 0)
            if replies < CHAN_MIN_REPLIES:
                continue
            threads.append({
                'no': t.get('no'),
                'subject': _strip_html(t.get('sub', ''))[:200],
                'comment': _strip_html(t.get('com', ''))[:400],
                'replies': replies,
                'url': f'https://boards.4chan.org/{board}/thread/{t.get("no")}',
            })
    threads.sort(key=lambda x: x['replies'], reverse=True)
    return threads[:CHAN_MAX_THREADS]


FETCH_WORKERS = 10


def _fetch_one(feed):
    typ = feed.get('type', 'rss')
    entry = {
        'tag': feed.get('tag'),
        'geo': feed.get('geo'),
        'type': typ,
        'url': feed['url'],
        'items': [],
    }
    try:
        if typ == 'youtube':
            entry['items'] = fetch_youtube(feed['url'])
        elif typ == '4chan':
            entry['items'] = fetch_4chan(feed['url'])
        else:
            entry['items'] = fetch_rss(feed['url'])
        return feed['name'], entry, None
    except Exception as e:
        return feed['name'], entry, str(e)[:200]


def main():
    rss_only = '--rss-only' in sys.argv
    out_file = HERE.parent / ('feed_cache_rss.json' if rss_only else 'feed_cache.json')

    feeds = _load_feeds()
    if rss_only:
        feeds = [f for f in feeds
                 if f.get('type', 'rss') != 'youtube'
                 and f.get('tag') != 'academic']

    result = {
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'rss_only': rss_only,
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
                print(f"  ✗ {name:30} ({entry['type']:7}) ERROR: {err}", file=sys.stderr)
            else:
                print(f"  ✓ {name:30} ({entry['type']:7}) {len(entry['items']):3} items", file=sys.stderr)

    if errors:
        result['errors'] = errors

    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nWrote {out_file} ({out_file.stat().st_size} bytes)", file=sys.stderr)
    print(f"  feeds: {len(result['feeds'])}", file=sys.stderr)
    print(f"  errors: {len(errors)}", file=sys.stderr)


if __name__ == '__main__':
    main()
