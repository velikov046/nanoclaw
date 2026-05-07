"""Push analysis results to nanoclaw agents via IPC."""
import json
import os
import re
import time
from pathlib import Path

IPC_BASE = Path("/home/aurellian/nanoclaw/data/ipc")
DEFAULT_SOURCE = "telegram_main"
DEFAULT_JID = "tg4:-5117247882"  # Stella Support


def _extract_ranking(analysis: str) -> str:
    """Pull the ranked list section out of the analysis markdown."""
    # Find the ranking table (first table in the doc)
    lines = analysis.splitlines()
    in_table = False
    table_lines = []
    header_line = ""

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_table and re.search(r"rank|suspect|fascist", stripped, re.IGNORECASE) and stripped.startswith("#"):
            header_line = stripped.lstrip("#").strip()
        if "|" in stripped and not in_table:
            in_table = True
        if in_table:
            if not stripped or (stripped and "|" not in stripped and not stripped.startswith("|")):
                break
            table_lines.append(stripped)

    if not table_lines:
        return ""

    # Convert markdown table to plain text ranking
    rows = [r for r in table_lines if not re.match(r"^\|[-:| ]+\|$", r)]
    result_lines = []
    for row in rows[1:]:  # skip header row
        cells = [c.strip().strip("*") for c in row.strip("|").split("|")]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            rank = cells[0]
            player = cells[1]
            reason = cells[2] if len(cells) > 2 else ""
            result_lines.append(f"{rank}. {player} — {reason}" if reason else f"{rank}. {player}")

    return "\n".join(result_lines)


def _extract_summary(analysis: str) -> str:
    """Pull the Summary section."""
    match = re.search(r"#{1,3}\s*Summary.*?\n(.*?)(?=\n#{1,3}|\Z)", analysis, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
        # Trim to ~500 chars for Telegram
        if len(text) > 500:
            text = text[:497].rsplit(" ", 1)[0] + "…"
        return text
    return ""


def format_telegram_message(analysis: str, analysis_num: int = 0, total_secs: int = 0) -> str:
    ranking = _extract_ranking(analysis)
    summary = _extract_summary(analysis)

    header = "🎭 *SilentPartner Analysis*"
    if total_secs:
        mins = total_secs // 60
        header += f" — {mins}m in"
    if analysis_num:
        header += f" (#{analysis_num})"

    parts = [header]
    if ranking:
        parts.append("\n*Fascist ranking:*\n" + ranking)
    if summary:
        parts.append("\n*Read:*\n" + summary)

    return "\n".join(parts)


def push(analysis: str, source: str = DEFAULT_SOURCE, jid: str = DEFAULT_JID,
         analysis_num: int = 0, total_secs: int = 0) -> None:
    messages_dir = IPC_BASE / source / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)

    text = format_telegram_message(analysis, analysis_num=analysis_num, total_secs=total_secs)
    payload = {"type": "message", "chatJid": jid, "text": text}

    filename = f"silentpartner_{int(time.time() * 1000)}.json"
    filepath = messages_dir / filename
    filepath.write_text(json.dumps(payload), encoding="utf-8")
    print(f"Pushed to Telegram ({jid}) via {filepath.name}")
