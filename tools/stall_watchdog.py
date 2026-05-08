#!/usr/bin/env python3
"""Detect (and optionally recover) NanoClaw stalls.

Two fault patterns observed 2026-05-01:

1. Unreplied user message — orchestrator stored a user message but no bot
   reply followed within the threshold window.
2. Stuck container — agent-runner emitted its OUTPUT_END sentinel but kept
   the process alive waiting for IPC reuse, so the GroupQueue never
   advances. The container looks "Up" in docker ps but is idle.

Designed to run host-side (systemd timer or user cron), not as a NanoClaw
scheduled task — the latter would queue behind the same stall it's meant to
detect.

Usage:
  stall_watchdog.py                    # detect + report via Telegram
  stall_watchdog.py --autokill         # also kill stuck containers
  stall_watchdog.py --dry-run          # print findings, no Telegram, no kill
  stall_watchdog.py --json             # JSON output (for scripting)

Exit codes: 0 = healthy, 1 = stalls found, 2 = error.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/aurellian/nanoclaw")
DB_PATH = ROOT / "store/messages.db"
STATE_PATH = ROOT / "store/stall-watchdog-state.json"
ENV_PATH = ROOT / ".env"
USAGE_PAUSE_PATH = ROOT / "data/usage-pause.json"

# Thresholds (seconds)
UNREPLIED_THRESHOLD = 4 * 60         # idle/absent container: alert at this age
UNREPLIED_THRESHOLD_BUSY = 60 * 60   # actively-working container (e.g. ingest, video pipeline): wait longer
CONTAINER_STUCK_THRESHOLD = 6 * 60   # container older than this AND idle = stuck
ALERT_DEDUPE_WINDOW = 30 * 60        # don't re-alert same condition within window

# Per-folder unreplied threshold overrides. Use for groups whose agent is
# deliberately slow-to-respond by design, where the default 4-min idle
# threshold would false-fire constantly. Override is applied unconditionally
# (overrides both idle and busy defaults) — set high enough to still catch a
# genuine stall.
UNREPLIED_THRESHOLD_OVERRIDES = {
    # Lydia is instructed not to reply immediately (deliberation gap is part
    # of her character). A 4-hour ceiling still surfaces real Bug A/B stalls.
    "lydia-clone": 4 * 3600,
}

# JID to send alerts to (main command bot, Leo)
ALERT_JID = "tg:8373094470"

# Reaction-style content prefixes that don't count as "user asked something"
NON_PROMPT_PREFIXES = ("[reacted ", "[removed reaction ")


# ---------- helpers ----------------------------------------------------------

def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # tolerate trailing 'Z'
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def get_usage_pause() -> dict | None:
    """Return active usage-pause state (mirrors src/usage-pause.ts), or None.

    While paused, the orchestrator deliberately defers all agent invocations,
    so every group's latest user message looks "unanswered" — alerting on
    that is noise. Stuck-container alerts are unaffected.
    """
    try:
        data = json.loads(USAGE_PAUSE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    paused_until = data.get("pausedUntil")
    if not isinstance(paused_until, (int, float)):
        return None
    if time.time() * 1000 >= paused_until:
        return None
    return data


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"alerts": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"alerts": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def _entry_ts(entry) -> float:
    """Pull alert timestamp from a state entry (legacy float or new dict form)."""
    if isinstance(entry, dict):
        return float(entry.get("ts", 0))
    return float(entry)


def should_alert(state: dict, key: str, current_last_bot_ts: str | None = None) -> bool:
    """True if we should fire a fresh alert for this key.

    Suppresses if we alerted within the dedupe window AND the prior stall
    hasn't ended. A bot reply appearing in the chat AFTER our last alert
    counts as the prior stall ending, so a re-stall fires fresh."""
    entry = state["alerts"].get(key)
    if not entry:
        return True
    last = _entry_ts(entry)
    if (time.time() - last) > ALERT_DEDUPE_WINDOW:
        return True
    if isinstance(entry, dict) and current_last_bot_ts:
        stored = entry.get("last_bot_msg_ts")
        if not stored or current_last_bot_ts > stored:
            # Bot replied since we alerted → prior stall cleared, this is new.
            return True
    return False


def mark_alerted(state: dict, key: str, last_bot_msg_ts: str | None = None) -> None:
    state["alerts"][key] = {
        "ts": time.time(),
        "last_bot_msg_ts": last_bot_msg_ts,
    }


def finding_key(f: dict) -> str:
    """Dedupe identity for a finding.

    Unreplied: keyed on jid only. One alert per stall episode per chat —
    follow-up user messages while the same stall persists do NOT re-alert.
    A new alert is allowed once a bot reply lands (stall ended) or the
    dedupe window expires.
    Stuck container: keyed on container name (which already embeds folder +
    start ts, so a new container after a kill gets a fresh alert)."""
    if f["kind"] == "unreplied":
        return f"unreplied:{f['jid']}"
    return f"{f['kind']}:{f.get('jid') or f.get('name')}"


def trim_state(state: dict) -> None:
    """Drop alert keys older than 2× dedupe window so the file doesn't grow."""
    cutoff = time.time() - 2 * ALERT_DEDUPE_WINDOW
    state["alerts"] = {
        k: v for k, v in state["alerts"].items() if _entry_ts(v) > cutoff
    }


# ---------- detectors --------------------------------------------------------

def find_unreplied(
    db: sqlite3.Connection,
    now: datetime,
    container_states: dict[str, dict],
) -> list[dict]:
    """Return groups where the latest message is an unanswered user prompt.

    Uses a longer threshold for groups whose container is actively working
    (not idle on OUTPUT_END) — ingests legitimately take >4 min."""
    db.row_factory = sqlite3.Row
    findings: list[dict] = []

    groups = db.execute(
        "SELECT jid, name, folder FROM registered_groups"
    ).fetchall()

    for g in groups:
        # Latest message overall in this chat (ordered by timestamp, falling back
        # to id-as-int — id is TEXT so naive ORDER BY id DESC misorders).
        row = db.execute(
            """
            SELECT id, content, is_bot_message, timestamp
            FROM messages
            WHERE chat_jid = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (g["jid"],),
        ).fetchone()
        if not row:
            continue

        # Skip reactions — they shouldn't trigger replies.
        content = (row["content"] or "")
        if content.startswith(NON_PROMPT_PREFIXES):
            continue
        if row["is_bot_message"]:
            continue

        ts = parse_iso(row["timestamp"])
        if not ts:
            continue
        age = (now - ts).total_seconds()

        # Pick threshold based on container activity for this folder.
        # Per-folder override (e.g. deliberately slow agents) wins over both
        # the idle and busy defaults.
        cs = container_states.get(g["folder"]) if g["folder"] else None
        is_busy = bool(cs and not cs["is_idle"])
        override = UNREPLIED_THRESHOLD_OVERRIDES.get(g["folder"]) if g["folder"] else None
        if override is not None:
            threshold = override
        else:
            threshold = UNREPLIED_THRESHOLD_BUSY if is_busy else UNREPLIED_THRESHOLD
        if age < threshold:
            continue

        # Latest bot reply in this chat — used by dedupe to detect that a
        # prior stall cleared (bot ts > stored alert's bot ts = new episode).
        bot_row = db.execute(
            "SELECT timestamp FROM messages WHERE chat_jid = ? AND is_bot_message = 1 "
            "ORDER BY timestamp DESC LIMIT 1",
            (g["jid"],),
        ).fetchone()
        last_bot_ts = bot_row["timestamp"] if bot_row else None

        findings.append({
            "kind": "unreplied",
            "jid": g["jid"],
            "name": g["name"],
            "folder": g["folder"],
            "msg_id": row["id"],
            "msg_preview": content[:120],
            "age_sec": int(age),
            "container_busy": is_busy,
            "last_bot_msg_ts": last_bot_ts,
        })

    return findings


_CONTAINER_AGE_RE = re.compile(r"Up\s+(\d+)\s*(second|minute|hour|day)s?")
_CONTAINER_NAME_RE = re.compile(r"^nanoclaw-(.+)-\d+$")


def _parse_container_age(status: str) -> int | None:
    """Convert docker ps Status string ('Up 4 minutes') to seconds."""
    m = _CONTAINER_AGE_RE.search(status)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]


def _parse_container_folder(name: str) -> str | None:
    """Extract group folder from container name 'nanoclaw-<folder>-<ts>'.
    Folder may itself contain hyphens (e.g. velikov-visions)."""
    m = _CONTAINER_NAME_RE.match(name)
    return m.group(1) if m else None


def get_container_states() -> dict[str, dict]:
    """Walk `docker ps` for nanoclaw-* containers and inspect each one's
    last log line. Returns folder → state dict with keys:
      name, age_sec, is_idle (last line is OUTPUT_END), last_line.
    Folders without a running container are absent from the map."""
    states: dict[str, dict] = {}
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "name=nanoclaw-",
             "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return states

    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        name, status = line.split("\t", 1)
        folder = _parse_container_folder(name)
        if not folder:
            continue
        age = _parse_container_age(status)
        try:
            tail = subprocess.run(
                ["docker", "logs", "--tail", "3", name],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except subprocess.SubprocessError:
            continue
        last = (tail.stdout + tail.stderr).strip().splitlines()
        last_line = last[-1].strip() if last else ""
        states[folder] = {
            "name": name,
            "age_sec": age,
            "is_idle": last_line.endswith("---NANOCLAW_OUTPUT_END---"),
            "last_line": last_line,
        }
    return states


def find_stuck_containers(container_states: dict[str, dict]) -> list[dict]:
    """Containers older than threshold whose last log line is the OUTPUT_END
    sentinel (= agent-runner finished, nothing new since)."""
    findings: list[dict] = []
    for folder, cs in container_states.items():
        if not cs["is_idle"]:
            continue
        age = cs["age_sec"]
        if age is None or age < CONTAINER_STUCK_THRESHOLD:
            continue
        findings.append({
            "kind": "stuck_container",
            "name": cs["name"],
            "folder": folder,
            "age_sec": age,
            "last_log_line": cs["last_line"],
        })
    return findings


# ---------- alerting ---------------------------------------------------------

def telegram_send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError):
        return False


def jid_to_chat_id(jid: str) -> str | None:
    # tg:<id> / tg2:<id> etc. — for the main bot we only send via tg:.
    m = re.match(r"^tg:(-?\d+)$", jid)
    return m.group(1) if m else None


def format_alert(findings: list[dict]) -> str:
    lines = ["⚠️ NanoClaw watchdog: stall detected"]
    for f in findings:
        if f["kind"] == "unreplied":
            mins = f["age_sec"] // 60
            lines.append(
                f"• {f['name']} ({f['jid']}): unanswered for {mins}m — "
                f"\"{f['msg_preview']}\""
            )
        elif f["kind"] == "stuck_container":
            mins = f["age_sec"] // 60
            lines.append(f"• stuck container {f['name']} ({mins}m idle)")
    return "\n".join(lines)


# ---------- recovery ---------------------------------------------------------

def kill_container(name: str) -> bool:
    try:
        subprocess.run(
            ["docker", "kill", name],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return True
    except subprocess.SubprocessError:
        return False


# ---------- main -------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--autokill", action="store_true",
                    help="kill stuck containers automatically")
    ap.add_argument("--dry-run", action="store_true",
                    help="print findings, no Telegram, no kill")
    ap.add_argument("--json", action="store_true",
                    help="emit findings as JSON instead of text")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"db missing: {DB_PATH}", file=sys.stderr)
        return 2

    state = load_state()
    trim_state(state)

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    now = now_utc()

    container_states = get_container_states()
    unreplied = find_unreplied(db, now, container_states)
    stuck = find_stuck_containers(container_states)

    # Suppress unreplied alerts during a global usage-pause: every group is
    # deliberately deferred, so unanswered messages are expected. Stuck
    # containers (idle-with-blocked-queue) are still real faults — keep them.
    pause = get_usage_pause()
    if pause and unreplied:
        suppressed_unreplied = unreplied
        unreplied = []
        if not args.json:
            expiry = datetime.fromtimestamp(pause["pausedUntil"] / 1000, tz=timezone.utc)
            print(
                f"[{now.isoformat()}] usage-pause active until {expiry.isoformat()} — "
                f"suppressing {len(suppressed_unreplied)} unreplied alert(s)"
            )

    # An idle agent-runner sitting on OUTPUT_END is the legitimate steady
    # state between IPC turns. Only treat it as actionable when its group
    # also has an unreplied user message — that's the actual fault signature
    # (idle container blocking a queued turn).
    blocked_folders = {f["folder"] for f in unreplied if f.get("folder")}
    actionable_stuck = []
    idle_only = []
    for s in stuck:
        (actionable_stuck if s.get("folder") in blocked_folders else idle_only).append(s)

    findings = unreplied + actionable_stuck

    # Filter by dedupe window (per finding key). Unreplied alerts dedupe on
    # the specific msg_id so a single stuck message produces a single alert
    # — not one every 30 min while the message sits there. A *new* unreplied
    # msg_id (or a stuck container in a different folder) still fires.
    fresh_findings = []
    for f in findings:
        key = finding_key(f)
        bot_ts = f.get("last_bot_msg_ts") if f["kind"] == "unreplied" else None
        if should_alert(state, key, bot_ts):
            fresh_findings.append(f)

    if args.json:
        print(json.dumps({"all": findings, "fresh": fresh_findings,
                          "idle_only": idle_only,
                          "now": now.isoformat()}, indent=2))
    else:
        if not findings:
            print(f"[{now.isoformat()}] healthy"
                  + (f" ({len(idle_only)} idle container(s) ignored)" if idle_only else ""))
        else:
            print(f"[{now.isoformat()}] {len(findings)} stall(s) "
                  f"({len(fresh_findings)} new):")
            for f in findings:
                print("  ", json.dumps(f))
            for s in idle_only:
                print("   idle (not blocking):", json.dumps(s))

    if args.dry_run or not fresh_findings:
        return 0 if not findings else 1

    # Optional auto-recovery.
    if args.autokill:
        for f in fresh_findings:
            if f["kind"] == "stuck_container":
                ok = kill_container(f["name"])
                f["killed"] = ok

    # Send Telegram alert via main bot.
    env = load_env(ENV_PATH)
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = jid_to_chat_id(ALERT_JID)
    if token and chat_id:
        text = format_alert(fresh_findings)
        if telegram_send(token, chat_id, text):
            for f in fresh_findings:
                bot_ts = f.get("last_bot_msg_ts") if f["kind"] == "unreplied" else None
                mark_alerted(state, finding_key(f), bot_ts)
            save_state(state)
        else:
            print("warning: telegram send failed", file=sys.stderr)
    else:
        print("warning: TELEGRAM_BOT_TOKEN or ALERT_JID missing — no alert sent",
              file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
