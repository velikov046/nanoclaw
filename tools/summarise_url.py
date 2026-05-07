#!/usr/bin/env python3
"""
URL Summariser — CLI
Fetches a URL and summarises the content in 2-3 sentences.

Usage:
  python3 summarise_url.py https://example.com/article
  python3 summarise_url.py https://example.com --brief
  python3 summarise_url.py https://example.com --out summary.txt
"""

import argparse
import os
import re
import sys

try:
    import anthropic
except ImportError:
    sys.exit("anthropic package not found. Run: pip install anthropic")

try:
    import httpx
except ImportError:
    sys.exit("httpx not found. Run: pip install httpx")

SYSTEM = "Summarise the key points of this web page content in 2–3 sentences. Be direct and factual. No preamble."
SYSTEM_BRIEF = "Summarise this web page in one sentence. Direct, no preamble."

STRIP_TAGS = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
STRIP_ALL = re.compile(r"<[^>]+>")
COLLAPSE = re.compile(r"\s+")


def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    for path in [
        r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env",
        "/home/aurellian/nanoclaw/.env",
    ]:
        try:
            with open(path, errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            continue
    return None


def fetch_text(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; summariser/1.0)"}
    try:
        r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        sys.exit(f"HTTP error {e.response.status_code} fetching {url}")
    except httpx.RequestError as e:
        sys.exit(f"Request error: {e}")

    html = r.text
    html = STRIP_TAGS.sub(" ", html)
    text = STRIP_ALL.sub(" ", html)
    text = COLLAPSE.sub(" ", text).strip()
    return text[:8000]


def summarise(url, brief=False):
    text = fetch_text(url)
    if not text:
        sys.exit("Error: no content retrieved from URL.")

    client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_BRIEF if brief else SYSTEM,
        messages=[{"role": "user", "content": f"URL: {url}\n\nContent:\n{text}"}],
    )
    return msg.content[0].text


def main():
    parser = argparse.ArgumentParser(description="Summarise a URL in 2-3 sentences")
    parser.add_argument("url", help="URL to fetch and summarise")
    parser.add_argument("--brief", action="store_true", help="One-sentence summary")
    parser.add_argument("--out", default="", help="Output file (default: print to stdout)")
    args = parser.parse_args()

    result = summarise(args.url, brief=args.brief)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
