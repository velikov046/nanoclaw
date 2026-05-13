"""
merge_aurora_learnings.py — Merge reviewed entries from aurora_learnings.md into AURORA.md.

Shows each unmerged entry and prompts for acceptance before appending to the
Learned techniques section of AURORA.md.

Usage:
  python3 merge_aurora_learnings.py
  python3 merge_aurora_learnings.py --all   # accept all without prompting
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent
LEARNINGS  = _TOOLS_DIR.parent / "groups/global/aurora_learnings.md"
AURORA_MD  = _TOOLS_DIR / "AURORA.md"
MERGED_TAG = "<!-- merged -->"
SECTION    = "## Learned techniques"


def load_entries(text: str) -> list[tuple[str, str]]:
    """Return list of (raw_block, header_line) for unmerged entries."""
    entries = []
    blocks = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", text, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block or MERGED_TAG in block:
            continue
        header = block.splitlines()[0]
        entries.append((block, header))
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="Accept all entries without prompting")
    args = ap.parse_args()

    if not LEARNINGS.exists():
        sys.exit(f"learnings file not found: {LEARNINGS}")
    if not AURORA_MD.exists():
        sys.exit(f"AURORA.md not found: {AURORA_MD}")

    learnings_text = LEARNINGS.read_text()
    entries = load_entries(learnings_text)

    if not entries:
        print("No unmerged entries.")
        return

    aurora_text = AURORA_MD.read_text()
    if SECTION not in aurora_text:
        aurora_text += f"\n\n{SECTION}\n\n"

    accepted = []
    for block, header in entries:
        if args.all:
            accepted.append(block)
            print(f"  + {header}")
        else:
            print(f"\n{'─'*60}\n{block}\n{'─'*60}")
            ans = input("Merge this entry? [y/N] ").strip().lower()
            if ans == "y":
                accepted.append(block)

    if not accepted:
        print("Nothing merged.")
        return

    # Append accepted entries after the section header
    insert = "\n\n" + "\n\n---\n\n".join(accepted)
    aurora_text = aurora_text.replace(SECTION, SECTION + insert, 1)
    AURORA_MD.write_text(aurora_text)
    print(f"\nMerged {len(accepted)} entr{'y' if len(accepted)==1 else 'ies'} into AURORA.md")

    # Mark merged entries in learnings file
    updated = learnings_text
    for block, _ in [(b, h) for b, h in entries if b in accepted]:
        updated = updated.replace(block, block + f"\n{MERGED_TAG}", 1)
    LEARNINGS.write_text(updated)


if __name__ == "__main__":
    main()
