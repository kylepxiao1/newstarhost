"""One-time move from media/*.json + local audio to per-group Supabase data + Storage CDN.

    python scripts/migrate_to_groups.py [--dry-run]            # data only
    python scripts/migrate_to_groups.py --with-history         # final run, right before deploying

Safe to re-run (idempotent): run it again right before deploying to pick up last edits.
- Creates the "wildcardz" group (all existing data) and an empty "testboys" group.
- Uploads every song's audio to the song-audio bucket (content-addressed) and rewrites URLs.
- Copies songs, dancers and settings documents into group_songs / group_kv.
- Tags existing play_events / stream_notes / battle_results with their group and remaps
  play_events.song_url to the new CDN URLs so play counts carry over.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth import hash_password, new_overlay_key  # noqa: E402
from backend.supabase_rest import SupabaseRest, audio_object_path, chunked  # noqa: E402
from scripts.supabase_sql import run_sql  # noqa: E402
from scripts.utils import _env  # noqa: E402

rest = SupabaseRest()
GROUPS = [
    {"username": "wildcardz", "display_name": "Wildcard Boys", "tiktok_handle": "wildcard_ns"},
    {"username": "testboys", "display_name": "Test Boys", "tiktok_handle": "valentinananaaaa"},
]
DOC_FILES = {"settings": "hotkeys.json", "role_presets": "role_presets.json",
             "rpd_queries": "rpd_queries.json", "gift_notes": "gift_notes.json"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def ensure_group(spec: dict, dry_run: bool) -> tuple[dict, str]:
    row = rest.select_one("groups", {"username": f"eq.{spec['username']}"})
    if row:
        return row, ""
    password = __import__("secrets").token_urlsafe(12)
    if dry_run:
        return {"id": f"<new {spec['username']}>", **spec}, password
    row = rest.insert("groups", {**spec, "password_hash": hash_password(password), "overlay_key": new_overlay_key()},
                      returning=True)[0]
    return row, password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="https://newstarhost.fly.dev", help="Old deployment serving /media/.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--with-history", action="store_true",
                        help="Also tag/remap play_events etc. Do this only in the final run right before "
                             "deploying: the old app's play counts are keyed by its /media/ URLs.")
    args = parser.parse_args()
    source = args.source.rstrip("/")
    http = httpx.Client(timeout=120.0)

    def get_json(name: str, default):
        r = http.get(f"{source}/media/{name}")
        return r.json() if r.status_code == 200 else default

    songs = get_json("songs.json", None)
    if not isinstance(songs, dict) or not songs:
        raise SystemExit(f"Couldn't read songs.json from {source} (already migrated and redeployed?)")
    dancers = get_json("dancers.json", [])
    docs = {key: get_json(fname, None) for key, fname in DOC_FILES.items()}
    print(f"Source: {len(songs)} songs, {len(dancers)} dancers")

    groups = {}
    for spec in GROUPS:
        row, password = ensure_group(spec, args.dry_run)
        groups[spec["username"]] = row
        print(f"Group {spec['username']}: {row['id']}" + (f"  NEW, temporary password: {password}" if password else " (exists)"))
    wc, tb = groups["wildcardz"], groups["testboys"]

    # Audio -> Storage CDN (content-addressed, so re-runs don't duplicate anything).
    old_urls = sorted({str(s.get("url") or "") for s in songs.values() if str(s.get("url") or "").startswith("/media/")})
    url_map: dict[str, str] = {}

    def move(old_url: str) -> None:
        name = urllib.parse.unquote(old_url[len("/media/"):])
        r = http.get(f"{source}/media/{urllib.parse.quote(name)}")
        if r.status_code != 200:
            print(f"  ! missing audio {name} ({r.status_code})")
            return
        path = audio_object_path(wc["id"], r.content, Path(name).suffix or "mp3")
        if not args.dry_run and not rest.object_exists(path):
            rest.upload(path, r.content, "audio/mpeg")
        url_map[old_url] = rest.public_url(path)

    print(f"Uploading {len(old_urls)} audio files to the song-audio bucket...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(move, old_urls))
    print(f"  {len(url_map)} ready")

    # Songs + documents for Wildcardz. "Test Boys" is its own login now, not a Wildcardz dancer.
    migrated = {}
    for sid, song in songs.items():
        song = dict(song)
        if song.get("url") in url_map:
            song["url"] = url_map[song["url"]]
        migrated[sid] = song
    wc_dancers = [d for d in dancers if str(d.get("name") or "").strip().lower() != "test boys"]
    settings = docs.get("settings") if isinstance(docs.get("settings"), dict) else {}
    if _env("SONG_SHEET_ID", "").strip():
        settings["song_sheet_id"] = _env("SONG_SHEET_ID", "").strip()
    docs["settings"] = settings
    if args.dry_run:
        print(f"[dry run] would write {len(migrated)} songs, {len(wc_dancers)} dancers, docs {sorted(k for k, v in docs.items() if v is not None)}")
        return 0
    rows = [{"group_id": wc["id"], "song_id": sid, "data": song, "updated_at": _now()} for sid, song in migrated.items()]
    for batch in chunked(rows, 200):
        rest.upsert("group_songs", batch, on_conflict="group_id,song_id")
    # Drop songs deleted on the old app since a previous run.
    existing = rest.select("group_songs", {"select": "song_id", "group_id": f"eq.{wc['id']}"})
    stale = [r["song_id"] for r in existing if r["song_id"] not in migrated]
    for batch in chunked(stale, 100):
        rest.delete("group_songs", {"group_id": f"eq.{wc['id']}", "song_id": "in.(" + ",".join(f'"{s}"' for s in batch) + ")"})
    kv = [{"group_id": wc["id"], "key": "dancers", "value": wc_dancers, "updated_at": _now()}]
    kv += [{"group_id": wc["id"], "key": k, "value": v, "updated_at": _now()} for k, v in docs.items() if v is not None]
    rest.upsert("group_kv", kv, on_conflict="group_id,key")
    print(f"Wrote {len(rows)} songs ({len(stale)} stale removed), {len(wc_dancers)} dancers, {len(kv) - 1} documents")
    if not args.with_history:
        print("History not touched (run again with --with-history right before deploying).")
        return 0

    # History: tag rows with their group, and point play history at the new audio URLs.
    test_match = "(lower(coalesce(group_name,'')) = 'test boys' or lower(coalesce(tiktok_username,'')) = 'valentinananaaaa')"
    run_sql(f"update public.play_events set group_id = {_sql_str(tb['id'])} where group_id is null and {test_match};")
    run_sql(f"update public.play_events set group_id = {_sql_str(wc['id'])} where group_id is null;")
    run_sql(f"update public.stream_notes set group_id = {_sql_str(tb['id'])} where group_id is null and lower(coalesce(group_name,'')) = 'test boys';")
    run_sql(f"update public.stream_notes set group_id = {_sql_str(wc['id'])} where group_id is null;")
    run_sql(f"update public.battle_results set group_id = {_sql_str(wc['id'])} where group_id is null;")
    pairs = list(url_map.items())
    for batch in chunked(pairs, 200):
        values = ",".join(f"({_sql_str(o)},{_sql_str(n)})" for o, n in batch)
        run_sql(f"update public.play_events p set song_url = m.new from (values {values}) as m(old, new) "
                f"where p.song_url = m.old and p.group_id = {_sql_str(wc['id'])};")
    counts = run_sql("select (select count(*) from public.play_events where group_id is null) as untagged_plays, "
                     f"(select count(*) from public.play_events where song_url like '/media/%' and group_id = {_sql_str(wc['id'])}) as old_url_plays;")
    print("History tagged:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
