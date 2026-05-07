#!/usr/bin/env python3
"""
Report on prompt-caching effectiveness 24h after the 2026-05-02 deploy
(excludeDynamicSections + dynamic budget moved out of cacheable append).

Sources:
  - /home/aurellian/nanoclaw/logs/nanoclaw.error.log (persistent, all exited
    containers' stderr blobs with embedded "Received input for group: X" and
    "Tokens used this session: N (cache read=R, write=W, hit=P%)" lines)
  - docker logs for currently-running nanoclaw-* containers (in-flight)

Posts a terse summary to the main bot (tg:8373094470).
"""
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/aurellian/nanoclaw")
LOG_PATH = ROOT / "logs/nanoclaw.error.log"
ENV_PATH = ROOT / ".env"
ALERT_JID_CHAT_ID = "8373094470"

# Sonnet 4.6 pricing (per 1M tokens)
PRICE_INPUT = 3.00
PRICE_CACHE_READ = 0.30
PRICE_CACHE_WRITE_5M = 3.75  # 5-min cache; SDK uses 5-min by default

TOKEN_LINE = re.compile(
    r"Tokens used this session: (\d+) \(cache read=(\d+), write=(\d+), hit=(\d+)%\)"
)
GROUP_LINE = re.compile(r"Received input for group: ([\w-]+)")


def load_token() -> str | None:
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):  # exact key, "=" excludes _2/_3/_4/_5 variants
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def collect_log_text() -> str:
    parts: list[str] = []
    if LOG_PATH.exists():
        parts.append(LOG_PATH.read_text(errors="replace"))
    try:
        names = subprocess.run(
            ["docker", "ps", "--filter", "name=nanoclaw-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
    except (subprocess.SubprocessError, FileNotFoundError):
        names = []
    for n in names:
        try:
            out = subprocess.run(
                ["docker", "logs", n], capture_output=True, text=True, timeout=15,
            )
            parts.append(out.stdout)
            parts.append(out.stderr)
        except subprocess.SubprocessError:
            continue
    return "\n".join(parts)


def parse(text: str) -> dict[str, dict[str, int]]:
    """Walk text; for each Tokens-with-cache match, attribute to most recent
    preceding 'Received input for group: X'."""
    agg: dict[str, dict[str, int]] = defaultdict(
        lambda: {"sessions": 0, "input": 0, "read": 0, "write": 0}
    )
    # Find all events (group markers + token lines) in order.
    events: list[tuple[int, str, tuple]] = []
    for m in GROUP_LINE.finditer(text):
        events.append((m.start(), "group", (m.group(1),)))
    for m in TOKEN_LINE.finditer(text):
        events.append((m.start(), "tokens", (
            int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)),
        )))
    events.sort(key=lambda e: e[0])
    current_group: str | None = None
    for _, kind, payload in events:
        if kind == "group":
            current_group = payload[0]
        elif kind == "tokens" and current_group:
            total, cache_read, cache_write, _hit = payload
            uncached_input = max(total - cache_read - cache_write, 0)
            a = agg[current_group]
            a["sessions"] += 1
            a["input"] += uncached_input
            a["read"] += cache_read
            a["write"] += cache_write
    return agg


def format_summary(agg: dict[str, dict[str, int]]) -> str:
    if not agg:
        return "Cache lever report: no annotated sessions logged yet (deploy was 2026-05-02 ~11:35 UTC+1)."
    total_read = sum(a["read"] for a in agg.values())
    total_write = sum(a["write"] for a in agg.values())
    total_input = sum(a["input"] for a in agg.values())
    denom = total_read + total_write + total_input
    eff_hit = (total_read / denom * 100) if denom else 0.0
    saved_usd = total_read * (PRICE_INPUT - PRICE_CACHE_READ) / 1_000_000
    write_cost_extra_usd = total_write * (PRICE_CACHE_WRITE_5M - PRICE_INPUT) / 1_000_000
    net_usd = saved_usd - write_cost_extra_usd
    net_label = f"${net_usd:.2f} saved" if net_usd >= 0 else f"${abs(net_usd):.2f} extra"
    flagged = [
        g for g, a in agg.items()
        if a["sessions"] >= 3 and a["read"] == 0
    ]
    lines = [
        f"Cache lever after ~24h: {eff_hit:.1f}% effective hit "
        f"(read={total_read:,}, write={total_write:,}, uncached_in={total_input:,})",
        f"Net est: {net_label} ({len(agg)} groups, {sum(a['sessions'] for a in agg.values())} sessions)",
    ]
    if flagged:
        lines.append(f"⚠ Stuck at 0% hit ≥3 sessions: {', '.join(flagged)}")
    # Per-group detail (compact)
    rows = sorted(agg.items(), key=lambda kv: kv[1]["read"], reverse=True)
    for g, a in rows:
        d = a["read"] + a["write"] + a["input"]
        h = (a["read"] / d * 100) if d else 0.0
        lines.append(f"  {g}: {a['sessions']}s, hit={h:.0f}%, read={a['read']:,}")
    return "\n".join(lines)


def send_telegram(token: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": ALERT_JID_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    text = collect_log_text()
    agg = parse(text)
    summary = format_summary(agg)
    print(summary)
    if "--no-send" in sys.argv:
        return 0
    token = load_token()
    if not token:
        print("TELEGRAM_BOT_TOKEN not found — printing only", file=sys.stderr)
        return 1
    ok = send_telegram(token, summary)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
