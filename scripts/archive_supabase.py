"""Archive old Supabase rows to local disk, then (separately) delete exactly what was archived.

    export: python scripts/archive_supabase.py export --dest /Volumes/Elements/supabase_archive
    delete: python scripts/archive_supabase.py delete --manifest <dest>/<cutoff>/manifest.json [--yes]

Event tables are paged by primary key (no iso_ts index needed) and written as gzipped JSONL
together with a manifest of id-range chunks. `delete` re-verifies each archive file's checksum
and row count, and only deletes a chunk when Supabase still holds exactly the archived number of
rows for that id range, so anything not in the archive is left alone. Without --yes it is a dry run.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from utils import _env


# Raw TikTok event tables: archived and eligible for deletion.
EVENT_TABLES = [
    "comment_events",
    "competition_events",
    "fan_events",
    "goal_update_events",
    "join_events",
    "like_events",
    "privilege_advance_events",
    "room_update_events",
    "social_events",
    "tiktok_events_raw",
]
# gift_events is opt-in for deletion: discord_verify_bot sums all-time gifts per donor.
OPTIONAL_EVENT_TABLES = ["gift_events"]
# Small tables the app reads as history/state: snapshotted in full, never deleted.
SNAPSHOT_TABLES = [
    "battle_results",
    "fan_info",
    "listener_heartbeats",
    "play_events",
    "stream_notes",
    "topic_trends",
]

PAGE_SIZE = 1000
CHUNK_ROWS = 5000
# Rows are inserted roughly in iso_ts order; stop scanning once a page is entirely past cutoff + slack.
ORDER_SLACK = timedelta(days=1)


def _client() -> httpx.Client:
    project_id = _env("SUPABASE_PROJECT_ID", "").strip()
    base_url = (_env("SUPABASE_URL", "").strip() or f"https://{project_id}.supabase.co").rstrip("/")
    key = _env("SUPABASE_SECRET_KEY", "").strip()
    if not key or not (project_id or _env("SUPABASE_URL", "").strip()):
        raise SystemExit("Missing SUPABASE_PROJECT_ID/SUPABASE_URL or SUPABASE_SECRET_KEY in app.env.")
    return httpx.Client(
        base_url=f"{base_url}/rest/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=120.0,
    )


def _request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    for attempt in range(6):
        try:
            resp = client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            err: Any = exc
        else:
            if resp.is_success:
                return resp
            err = f"status={resp.status_code} body={resp.text[:300]}"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"{method} {path} failed: {err}")


def _exact_count(client: httpx.Client, table: str, params: dict[str, Any]) -> int:
    resp = _request(
        client, "HEAD", f"/{table}", params={"select": "id", **params}, headers={"Prefer": "count=exact"}
    )
    return int(resp.headers["content-range"].split("/")[-1])


_FRACTION_RE = re.compile(r"\.(\d+)")


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    # Postgres trims trailing zeros (".54507"); Python 3.9 only accepts 3 or 6 fraction digits.
    text = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Unparseable timestamp {raw!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pages(client: httpx.Client, table: str, order_col: str) -> Iterator[list[dict[str, Any]]]:
    last = None
    while True:
        params = {"select": "*", "order": f"{order_col}.asc", "limit": PAGE_SIZE}
        if last is not None:
            params[order_col] = f"gt.{last}"
        rows = _request(client, "GET", f"/{table}", params=params).json()
        if not rows:
            return
        yield rows
        last = rows[-1][order_col]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _count_lines(path: Path) -> int:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _export_events(client: httpx.Client, table: str, cutoff: datetime, out_dir: Path) -> dict[str, Any]:
    final = out_dir / f"{table}.jsonl.gz"
    partial = final.with_suffix(".gz.partial")
    chunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    total = 0
    scanned = 0
    started = time.monotonic()
    with gzip.open(partial, "wt", encoding="utf-8") as fh:
        for rows in _pages(client, table, "id"):
            scanned += len(rows)
            page_ts = []
            for row in rows:
                ts = _parse_ts(row.get("iso_ts"))
                page_ts.append(ts)
                if ts is None or ts >= cutoff:
                    continue
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                total += 1
                if current is None or current["count"] >= CHUNK_ROWS:
                    current = {"first_id": row["id"], "last_id": row["id"], "count": 0}
                    chunks.append(current)
                current["last_id"] = row["id"]
                current["count"] += 1
            if scanned % (PAGE_SIZE * 50) == 0:
                rate = scanned / max(1e-6, time.monotonic() - started)
                print(f"  {table}: scanned {scanned:,} rows, archived {total:,} ({rate:,.0f} rows/s)", flush=True)
            known = [ts for ts in page_ts if ts is not None]
            if known and min(known) >= cutoff + ORDER_SLACK:
                break
    lines = _count_lines(partial)
    if lines != total:
        raise RuntimeError(f"{table}: wrote {total} rows but file has {lines} lines")
    os.replace(partial, final)
    return {"kind": "events", "file": final.name, "rows": total, "sha256": _sha256(final), "chunks": chunks}


def _export_snapshot(client: httpx.Client, table: str, out_dir: Path) -> dict[str, Any]:
    spec = _request(client, "GET", "/").json()["definitions"][table]["properties"]
    order_col = next((c for c, p in spec.items() if "Primary Key" in p.get("description", "")), None)
    final = out_dir / f"{table}.snapshot.jsonl.gz"
    partial = final.with_suffix(".gz.partial")
    total = 0
    with gzip.open(partial, "wt", encoding="utf-8") as fh:
        if order_col:
            pages = _pages(client, table, order_col)
        else:
            pages = iter([_request(client, "GET", f"/{table}", params={"select": "*"}).json()])
        for rows in pages:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                total += 1
    if _count_lines(partial) != total:
        raise RuntimeError(f"{table}: snapshot line count mismatch")
    os.replace(partial, final)
    return {"kind": "snapshot", "file": final.name, "rows": total, "sha256": _sha256(final)}


def cmd_export(args: argparse.Namespace) -> int:
    dest = Path(args.dest)
    if not dest.parent.exists():
        raise SystemExit(f"{dest.parent} does not exist (is the drive mounted?)")
    cutoff = (
        _parse_ts(args.cutoff)
        if args.cutoff
        else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)
    )
    out_dir = dest / f"before_{cutoff.strftime('%Y-%m-%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if manifest and manifest.get("cutoff") != cutoff.isoformat():
        raise SystemExit(f"{manifest_path} was made with cutoff {manifest.get('cutoff')}; use a new --dest.")
    manifest.setdefault("cutoff", cutoff.isoformat())
    manifest.setdefault("tables", {})

    event_tables = EVENT_TABLES + OPTIONAL_EVENT_TABLES
    snapshot_tables = [] if args.skip_snapshots else SNAPSHOT_TABLES
    print(f"Archiving rows with iso_ts < {cutoff.isoformat()} to {out_dir}")
    with _client() as client:
        for table in event_tables + snapshot_tables:
            entry = manifest["tables"].get(table)
            if entry and (out_dir / entry["file"]).exists() and table in event_tables:
                print(f"{table}: already archived ({entry['rows']:,} rows), skipping")
                continue
            print(f"{table}: exporting...", flush=True)
            if table in event_tables:
                entry = _export_events(client, table, cutoff, out_dir)
            else:
                entry = _export_snapshot(client, table, out_dir)
            entry["exported_at"] = datetime.now(timezone.utc).isoformat()
            manifest["tables"][table] = entry
            manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"{table}: {entry['rows']:,} rows -> {entry['file']}")
    print(f"Done. Manifest: {manifest_path}")
    return 0


class _HashingReader:
    def __init__(self, raw, digest):
        self._raw, self._digest = raw, digest

    def read(self, size=-1):
        data = self._raw.read(size)
        self._digest.update(data)
        return data


def cmd_rebuild_manifest(args: argparse.Namespace) -> int:
    """Recreate a manifest from the archive files (chunks are CHUNK_ROWS rows in id order)."""
    archive_dir = Path(args.archive_dir)
    cutoff = _parse_ts(args.cutoff)
    manifest = {"cutoff": cutoff.isoformat(), "tables": {}}
    for path in sorted(p for p in archive_dir.glob("*.jsonl.gz") if not p.name.startswith("._")):
        table = path.name.split(".")[0]
        digest = hashlib.sha256()
        chunks: list[dict[str, Any]] = []
        rows = 0
        with path.open("rb") as raw, gzip.GzipFile(fileobj=_HashingReader(raw, digest)) as gz:
            for line in gz:
                rows += 1
                if path.name.endswith(".snapshot.jsonl.gz"):
                    continue
                row_id = json.loads(line)["id"]
                if not chunks or chunks[-1]["count"] >= CHUNK_ROWS:
                    chunks.append({"first_id": row_id, "last_id": row_id, "count": 0})
                chunks[-1]["last_id"] = row_id
                chunks[-1]["count"] += 1
            raw_rest = raw.read()
            digest.update(raw_rest)
        entry: dict[str, Any] = {"file": path.name, "rows": rows, "sha256": digest.hexdigest()}
        if path.name.endswith(".snapshot.jsonl.gz"):
            entry["kind"] = "snapshot"
        else:
            entry.update(kind="events", chunks=chunks)
        manifest["tables"][table] = entry
        print(f"{table}: {rows:,} rows, {len(chunks)} chunks", flush=True)
    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {args.out}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    archive_dir = Path(args.archive_dir) if args.archive_dir else manifest_path.parent
    cutoff = manifest["cutoff"]
    deletable = set(EVENT_TABLES) | (set(OPTIONAL_EVENT_TABLES) if args.include_gifts else set())
    tables = [t for t, e in manifest["tables"].items() if e["kind"] == "events" and t in deletable]
    if args.tables:
        tables = [t for t in tables if t in args.tables]

    # Verify every archive file before touching Supabase.
    for table in tables:
        entry = manifest["tables"][table]
        path = archive_dir / entry["file"]
        if _sha256(path) != entry["sha256"] or _count_lines(path) != entry["rows"]:
            raise SystemExit(f"{path} does not match the manifest; refusing to delete anything.")
        if sum(c["count"] for c in entry["chunks"]) != entry["rows"]:
            raise SystemExit(f"{table}: chunk counts do not add up; refusing to delete anything.")
    print(f"Archive verified for: {', '.join(tables)}")
    if not args.yes:
        print("Dry run: re-run with --yes to delete. Nothing was deleted.")

    with _client() as client:
        for table in tables:
            entry = manifest["tables"][table]
            done = set(entry.get("deleted_chunks", []))
            deleted = skipped = 0
            for idx, chunk in enumerate(entry["chunks"]):
                if idx in done:
                    continue
                params = {"and": f"(id.gte.{chunk['first_id']},id.lte.{chunk['last_id']},iso_ts.lt.{cutoff})"}
                live = _exact_count(client, table, params)
                if live == 0:
                    continue
                if live != chunk["count"]:
                    skipped += 1
                    print(f"  {table} ids {chunk['first_id']}-{chunk['last_id']}: "
                          f"{live} live rows vs {chunk['count']} archived, skipping")
                    continue
                if args.yes:
                    _request(client, "DELETE", f"/{table}", params=params, headers={"Prefer": "return=minimal"})
                    done.add(idx)
                    entry["deleted_chunks"] = sorted(done)
                    manifest_path.write_text(json.dumps(manifest, indent=2))
                deleted += live
            verb = "deleted" if args.yes else "would delete"
            print(f"{table}: {verb} {deleted:,} rows, skipped {skipped} chunk(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="Archive rows older than the cutoff to local disk.")
    exp.add_argument("--dest", required=True, help="Archive root, e.g. /Volumes/Elements/supabase_archive")
    exp.add_argument("--cutoff", help="ISO timestamp; default is 30 days ago at 00:00 UTC.")
    exp.add_argument("--skip-snapshots", action="store_true", help="Do not snapshot the small state tables.")
    exp.set_defaults(func=cmd_export)
    dele = sub.add_parser("delete", help="Delete archived rows from Supabase (dry run unless --yes).")
    dele.add_argument("--manifest", required=True)
    dele.add_argument("--archive-dir", help="Directory holding the archive files (default: the manifest's).")
    dele.add_argument("--tables", nargs="*", help="Limit to these tables.")
    dele.add_argument("--include-gifts", action="store_true",
                      help="Also delete gift_events (lowers all-time totals used by discord_verify_bot).")
    dele.add_argument("--yes", action="store_true", help="Actually delete.")
    dele.set_defaults(func=cmd_delete)
    reb = sub.add_parser("rebuild-manifest", help="Recreate a manifest from the archive files.")
    reb.add_argument("--archive-dir", required=True)
    reb.add_argument("--cutoff", required=True, help="The cutoff the archive was exported with.")
    reb.add_argument("--out", required=True, help="Where to write the manifest (local disk recommended).")
    reb.set_defaults(func=cmd_rebuild_manifest)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
