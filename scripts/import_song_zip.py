"""Import a group's song folders (e.g. a Google Drive zip export) into its library.

    python scripts/import_song_zip.py <group_username> <export.zip> [--dry-run]

Expected layout (one top folder, then):
  <Stage>-<RealName>/Song- Artist- Stage.mp3     a dancer's song (dancer = stage name)
  <Stage>-<RealName>/MVP_ Song- Stage.mp3        that dancer's MVP song ("MVP" prefix)
  GROUP SONGS/Song- Group.mp3                    group dance
  GROUP SONGS/TTU/...                            group dance + the "ttu" role
  GROUP SONGS/Big Gifts/...                      group dance tagged "Big Gift"
  .../DO NOT USE/...                             skipped
The last "- X" in a filename says whose song it is when X is a dancer or "Group".
Identical audio files are imported once. Audio goes to the song-audio CDN bucket.

Only use this for a group that isn't loaded in the running app yet (e.g. a new group),
or restart the app afterwards: the app keeps each loaded group's library in memory.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.supabase_rest import SupabaseRest, audio_object_path, chunked  # noqa: E402

AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav", ".aac": "audio/aac", ".ogg": "audio/ogg"}
BIG_GIFT_TAG = {"text": "Big Gift", "color": "#f59e0b"}


def _duration(data: bytes, suffix: str):
    try:
        from mutagen import File as MutagenFile
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(data)
            tmp.flush()
            length = getattr(getattr(MutagenFile(tmp.name), "info", None), "length", None)
        return int(round(float(length))) if length else None
    except Exception:
        return None


def parse_title(stem: str):
    """'✨MVP_ Superpower- Kiss of Life- Pheebie (2)' -> (is_mvp, name, artist, owner_tag)."""
    text = re.sub(r"\s*\(\d+\)\s*$", "", stem).strip()
    is_mvp = False
    match = re.match(r"^\W*MVP[_\s]*", text)
    if match:
        is_mvp = True
        text = text[match.end():]
    text = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", "'", text)  # Drive turns ' into _
    parts = [p.strip() for p in re.split(r"-\s+", text) if p.strip()]
    owner = ""
    if parts and len(parts) > 1:
        owner = parts.pop()
    # "Animal Group" / "#MeNow- Fromis9 Group": trailing " Group" without a dash
    tail = parts[-1] if parts else ""
    if not owner and re.search(r"\s+Group$", tail):
        parts[-1] = re.sub(r"\s+Group$", "", tail)
        owner = "Group"
    if owner and re.search(r"\s+Group$", owner) and owner.lower() != "group":
        parts.append(re.sub(r"\s+Group$", "", owner))
        owner = "Group"
    name = parts[0] if parts else stem
    artist = " - ".join(parts[1:]) if len(parts) > 1 else ""
    return is_mvp, name.strip(), artist.strip(), owner.strip()


def build_songs(zip_path: Path):
    z = zipfile.ZipFile(zip_path)
    entries = [i for i in z.infolist() if not i.is_dir() and Path(i.filename).suffix.lower() in AUDIO_TYPES]
    # Folder (first level under the top folder) -> dancer stage name.
    dancers = []
    for info in entries:
        parts = info.filename.split("/")
        folder = parts[1] if len(parts) > 2 else ""
        if folder and folder.upper() != "GROUP SONGS":
            stage = folder.split("-")[0].strip()
            if stage and stage not in dancers:
                dancers.append(stage)
    by_hash: dict[str, dict] = {}
    skipped = []
    for info in sorted(entries, key=lambda i: i.filename):
        parts = info.filename.split("/")
        folders = parts[1:-1]
        if any(f.strip().upper() == "DO NOT USE" for f in folders):
            skipped.append(info.filename)
            continue
        data = z.read(info)
        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(info.filename).suffix.lower()
        is_mvp, name, artist, owner_tag = parse_title(Path(parts[-1]).stem)
        top = folders[0] if folders else ""
        in_group = top.upper() == "GROUP SONGS"
        folder_dancer = "" if in_group else top.split("-")[0].strip()
        owner = next((d for d in dancers if d.lower() == owner_tag.lower()), "") or ("" if owner_tag.lower() == "group" else folder_dancer)
        group_song = in_group or owner_tag.lower() == "group" or (not owner and not is_mvp)
        sub = [f.strip().lower() for f in folders[1:]]
        song = by_hash.get(digest) or {
            "data": data, "suffix": suffix, "sources": [],
            "meta": {"name": name, "artist": artist, "dancers": [], "front_dancers": [], "mvp_dancers": [],
                     "roles": [], "knows_song": [], "special_dance_for": [], "exclusive_mvp_for": "",
                     "exclusive_tournament_mvp_for": "", "volume": 100, "difficulty": "medium",
                     "camera_dance": False, "duo_dance": False, "tags": [], "battle_disabled": False,
                     "group_song": False},
        }
        song["sources"].append(info.filename)
        meta = song["meta"]
        if is_mvp and owner:
            meta["exclusive_mvp_for"] = owner
        elif owner and not group_song and owner not in meta["dancers"]:
            meta["dancers"].append(owner)
        if group_song:
            meta["group_song"] = True
        if "ttu" in sub and "ttu" not in meta["roles"]:
            meta["roles"].append("ttu")
            meta["group_song"] = True
        if any("big gift" in f for f in sub) or owner_tag.lower() == "big gift":
            meta["group_song"] = True
            if BIG_GIFT_TAG not in meta["tags"]:
                meta["tags"].append(dict(BIG_GIFT_TAG))
        by_hash[digest] = song
    return dancers, list(by_hash.values()), skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("group")
    parser.add_argument("zip", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rest = SupabaseRest()
    group = rest.select_one("groups", {"select": "id,username", "username": f"eq.{args.group.lower()}"})
    if not group:
        raise SystemExit(f"No group {args.group!r}; create it with scripts/manage_groups.py first.")
    dancers, songs, skipped = build_songs(args.zip)
    print(f"Dancers: {', '.join(dancers)}")
    print(f"Songs: {len(songs)} (skipped {len(skipped)} in DO NOT USE)")
    for s in sorted(songs, key=lambda s: (not s["meta"]["group_song"], s["meta"]["exclusive_mvp_for"] or "~", s["meta"]["name"].lower())):
        m = s["meta"]
        kind = ("MVP for " + m["exclusive_mvp_for"]) if m["exclusive_mvp_for"] else ("group" if m["group_song"] else ", ".join(m["dancers"]) or "?")
        extra = " ".join(x for x in [("roles=" + ",".join(m["roles"])) if m["roles"] else "", "tag=Big Gift" if m["tags"] else "",
                                       f"(x{len(s['sources'])} copies)" if len(s["sources"]) > 1 else ""] if x)
        print(f"  {m['name']:28} {m['artist']:18} {kind:18} {extra}")
    if args.dry_run:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for s in songs:
        ext = s["suffix"].lstrip(".")
        url = rest.upload(audio_object_path(group["id"], s["data"], ext), s["data"], AUDIO_TYPES[s["suffix"]])
        meta = {**s["meta"], "url": url, "duration_sec": _duration(s["data"], s["suffix"])}
        rows.append({"group_id": group["id"], "song_id": uuid.uuid4().hex, "data": meta, "updated_at": now})
    for batch in chunked(rows, 200):
        rest.upsert("group_songs", batch, on_conflict="group_id,song_id")
    existing = rest.select_one("group_kv", {"select": "value", "group_id": f"eq.{group['id']}", "key": "eq.dancers"})
    current = (existing or {}).get("value") or []
    names = {d.get("name", "").lower() for d in current}
    current += [{"name": d, "handle": ""} for d in dancers if d.lower() not in names]
    rest.upsert("group_kv", {"group_id": group["id"], "key": "dancers", "value": current, "updated_at": now},
                on_conflict="group_id,key")
    print(f"Imported {len(rows)} songs and {len(dancers)} dancers into {group['username']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
