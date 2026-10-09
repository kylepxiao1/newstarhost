"""Live progress view for archive_supabase.py export.

    python scripts/archive_progress.py <export log file>   (Ctrl-C to exit)
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from archive_supabase import EVENT_TABLES, OPTIONAL_EVENT_TABLES, SNAPSHOT_TABLES

# Approximate rows older than the cutoff (inventory taken 2026-10-02); only used for % and ETA.
EXPECTED_ROWS = {
    "comment_events": 1_456_124,
    "competition_events": 272_860,
    "fan_events": 1_801,
    "goal_update_events": 116_936,
    "join_events": 2_662_818,
    "like_events": 3_208_028,
    "privilege_advance_events": 180_096,
    "room_update_events": 3_900_000,
    "social_events": 607_899,
    "tiktok_events_raw": 20,
    "gift_events": 2_150_146,
}
PROGRESS_RE = re.compile(r"^\s+(\w+): scanned ([\d,]+) rows, archived ([\d,]+) \(([\d,]+) rows/s\)")
DONE_RE = re.compile(r"^(\w+): (?:already archived \()?([\d,]+) rows(?: -> |\), skipping)")
START_RE = re.compile(r"^(\w+): exporting\.\.\.")
BAR = 30


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def render(log: Path) -> str:
    done: dict[str, int] = {}
    current: dict[str, int] = {}
    rate = 0
    running = None
    finished = False
    errors = []
    for line in log.read_text(errors="replace").splitlines():
        if m := START_RE.match(line):
            running = m.group(1)
        elif m := PROGRESS_RE.match(line):
            current[m.group(1)] = _int(m.group(3))
            rate = _int(m.group(4))
        elif m := DONE_RE.match(line):
            done[m.group(1)] = _int(m.group(2))
        elif line.startswith("Done. Manifest"):
            finished = True
        elif "Error" in line or "Traceback" in line:
            errors.append(line)

    out = [f"Supabase archive  ({log})", ""]
    remaining = 0
    for table in EVENT_TABLES + OPTIONAL_EVENT_TABLES + SNAPSHOT_TABLES:
        expected = EXPECTED_ROWS.get(table)
        if table in done:
            rows, frac, status = done[table], 1.0, "done"
        elif table == running:
            rows = current.get(table, 0)
            frac = min(0.99, rows / expected) if expected else 0.0
            status = "running"
        else:
            rows, frac, status = 0, 0.0, "pending"
        if expected and status != "done":
            remaining += max(0, expected - rows)
        bar = "#" * int(frac * BAR) + "-" * (BAR - int(frac * BAR))
        approx = "~" if table == "room_update_events" else ""
        target = f"/ {approx}{expected:,}" if expected else "(snapshot)"
        out.append(f"{table:26} [{bar}] {status:8} {rows:>11,} {target}")
    out.append("")
    if finished:
        out.append(f"Finished: {sum(done.values()):,} rows archived.")
    elif rate:
        out.append(f"{rate:,} rows/s, about {remaining / rate / 60:.0f} min left")
    out.extend(errors[-5:])
    return "\n".join(out)


def main() -> int:
    log = Path(sys.argv[1])
    try:
        while True:
            text = render(log)
            print("\033[2J\033[H" + text, flush=True)
            if text.splitlines()[-1].startswith("Finished"):
                return 0
            time.sleep(5)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
