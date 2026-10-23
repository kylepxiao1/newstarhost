"""Build a standalone, offline Windows zip of the base NewStarHost app.

    python scripts/build_offline_bundle.py --group wildcardz [--out build]

The server also calls build_bundle() directly (Settings > Offline Bundle) for the logged-in
group. Audio is fetched from the Supabase Storage CDN while zipping.

The zip contains an embedded Windows Python, the backend + web pages, and a copy of the
song library, dancers, settings and audio pulled from the live deployment. Unzip it on
Windows and double-click "Start NewStarHost.bat"; no install or internet needed.
Supabase, TikTok, OBS and Discord features are left disabled (no credentials are bundled).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import zipfile
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.group_store import LOCAL_KV_FILES  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PY_VERSION = "3.12.7"
PY_EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
SUPABASE_JS_URL = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"
# Pinned to versions known to work with the app. pip evaluates "Windows-only" dependency
# markers against the build machine, so Windows-only deps (colorama, tzdata) are listed.
REQUIREMENTS = [
    "fastapi==0.115.0",
    "starlette==0.38.6",
    "pydantic==2.12.5",
    "pydantic-core==2.41.5",
    "uvicorn==0.23.2",
    "click==8.1.8",
    "colorama==0.4.6",
    "websockets==15.0.1",
    "httpx==0.27.2",
    "httpcore==1.0.9",
    "anyio==4.12.1",
    "python-multipart==0.0.9",
    "mutagen==1.47.0",
    "tzdata==2025.2",
]
APP_FILES = [
    "backend/__init__.py",
    "backend/main.py",
    "backend/state.py",
    "backend/config.py",
    "backend/obs_controller.py",
    "backend/analytics_store.py",
    "backend/websocket_manager.py",
    "backend/access_log.py",
    "backend/auth.py",
    "backend/group_store.py",
    "backend/supabase_rest.py",
    "backend/tenancy.py",
    "scripts/utils.py",
    "scripts/sync_song_sheet.py",
]
LAUNCHER = r"""@echo off
title NewStarHost (offline)
"%~dp0python\python.exe" "%~dp0app\run_offline.py"
echo.
echo NewStarHost stopped. Press any key to close this window.
pause >nul
"""

RUNNER = """import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["NSH_OFFLINE"] = "1"  # single local group, data in media/, no logins


def free_port(start=8000):
    for port in range(start, start + 50):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("No free port found between 8000 and 8049.")


def open_when_ready(url, port):
    for _ in range(120):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.5)


def main():
    import uvicorn

    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    print("Starting NewStarHost... keep this window open while you use the app.")
    print(f"If the browser doesn't open by itself, go to {url}")
    threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()
    uvicorn.run("backend.main:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
"""

README = """NewStarHost (offline)
=====================

1. Unzip this folder anywhere (e.g. your Desktop). Don't run it from inside the zip.
2. Double-click "Start NewStarHost.bat".
   - If Windows SmartScreen warns about it, click "More info" then "Run anyway".
   - If Windows Firewall asks, you can click Cancel; the app only listens on this computer.
3. The app opens in your browser (usually at http://127.0.0.1:8000/).
   Keep the black console window open while you use it; close it to stop the app.

No internet connection is needed. Songs, dancers and settings are a copy of the live
app taken on {snapshot}. Changes you make here are saved in app\\media and do not sync
back to the live app.

Not included: TikTok live data, gifts/top gifters, Supabase analytics, OBS control,
Discord bots and overlays.
"""


Progress = Optional[Callable[[str, float], None]]
# Audio and wheels are already compressed; storing them keeps building and unzipping fast.
_STORED_SUFFIXES = {".mp3", ".zip", ".whl", ".pyd", ".dll"}


def log(msg: str) -> None:
    print(msg, flush=True)


def download(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 16):
                fh.write(chunk)
    tmp.replace(dest)


def prepare_runtime(cache: Path, client: httpx.Client) -> tuple[Path, Path, Path]:
    """Download (once) the Windows embedded Python, the Windows wheels and supabase-js."""
    cache.mkdir(parents=True, exist_ok=True)
    embed_zip = cache / Path(PY_EMBED_URL).name
    if not embed_zip.exists():
        log(f"Downloading embedded Python {PY_VERSION}...")
        download(client, PY_EMBED_URL, embed_zip)
    wheels = cache / "wheels"
    stamp = wheels / ".requirements"
    if not stamp.exists() or stamp.read_text() != "\n".join(REQUIREMENTS):
        log("Downloading Windows packages...")
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:",
             "--platform", "win_amd64", "--python-version", PY_VERSION.rsplit(".", 1)[0],
             "--implementation", "cp", "-d", str(wheels), *REQUIREMENTS],
            check=True,
        )
        stamp.write_text("\n".join(REQUIREMENTS))
    supabase_js = cache / "supabase.js"
    if not supabase_js.exists():
        download(client, SUPABASE_JS_URL, supabase_js)
    return embed_zip, wheels, supabase_js


def _audio_name(url: str) -> str:
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    if not name or re.search(r'[<>:"/\\|?*]', name) or name.endswith((" ", ".")):
        raise RuntimeError(f"Audio name not valid on Windows: {name!r}")
    return name


def build_bundle(zip_path: Path, export: dict, cache: Path, progress: Progress = None) -> dict:
    """Write the offline zip for one group.

    export = {"songs": {song_id: song}, "docs": {"dancers": [...], "settings": {...}, ...}}.
    Audio is downloaded from each song's (CDN) URL while zipping and the songs are rewritten
    to point at the bundled copies in /media/.
    """
    def report(step: str, frac: float) -> None:
        if progress:
            progress(step, frac)

    report("Preparing Windows runtime", 0.0)
    songs = json.loads(json.dumps(export.get("songs") or {}))
    docs = export.get("docs") or {}
    sources: dict[str, str] = {}  # bundled name -> source URL
    for song in songs.values():
        url = str(song.get("url") or "")
        if url.startswith("http://") or url.startswith("https://"):
            name = _audio_name(url)
            sources.setdefault(name, url)
            song["url"] = f"/media/{name}"

    missing = 0
    root = "NewStarHost"
    tmp = zip_path.with_suffix(".zip.part")
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        embed_zip, wheels, supabase_js = prepare_runtime(cache, client)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            def add_bytes(arc: str, data: bytes) -> None:
                kind = zipfile.ZIP_STORED if Path(arc).suffix.lower() in _STORED_SUFFIXES else zipfile.ZIP_DEFLATED
                zf.writestr(f"{root}/{arc}", data, compress_type=kind)

            def add_file(arc: str, src: Path) -> None:
                kind = zipfile.ZIP_STORED if src.suffix.lower() in _STORED_SUFFIXES else zipfile.ZIP_DEFLATED
                zf.write(src, f"{root}/{arc}", compress_type=kind)

            report("Packing Python", 0.05)
            with zipfile.ZipFile(embed_zip) as ez:
                for info in ez.infolist():
                    data = ez.read(info)
                    if info.filename.endswith("._pth"):
                        stem = Path(info.filename).stem
                        data = f"{stem}.zip\n.\nLib\\site-packages\n..\\app\nimport site\n".encode()
                    add_bytes(f"python/{info.filename}", data)
            for wheel in sorted(wheels.glob("*.whl")):
                with zipfile.ZipFile(wheel) as wz:
                    for info in wz.infolist():
                        if not info.is_dir():
                            add_bytes(f"python/Lib/site-packages/{info.filename}", wz.read(info))

            report("Packing app", 0.1)
            for rel in APP_FILES:
                add_file(f"app/{rel}", REPO / rel)
            add_bytes("app/scripts/__init__.py", b"")
            static = REPO / "backend" / "static"
            for path in sorted(static.rglob("*")):
                if not path.is_file():
                    continue
                arc = f"app/backend/static/{path.relative_to(static).as_posix()}"
                if path.suffix == ".html":
                    text = path.read_text(encoding="utf-8")
                    add_bytes(arc, text.replace(SUPABASE_JS_URL, "/static/vendor/supabase.js").encode("utf-8"))
                else:
                    add_file(arc, path)
            add_file("app/backend/static/vendor/supabase.js", supabase_js)
            add_bytes("app/run_offline.py", RUNNER.encode("utf-8"))
            add_bytes("Start NewStarHost.bat", LAUNCHER.replace("\n", "\r\n").encode("ascii"))
            add_bytes("README.txt", README.format(snapshot=date.today().isoformat()).replace("\n", "\r\n").encode("utf-8"))

            add_bytes("app/media/songs.json", json.dumps(songs, ensure_ascii=False, indent=2).encode("utf-8"))
            for key, value in docs.items():
                filename = LOCAL_KV_FILES.get(key, f"{key}.json")
                add_bytes(f"app/media/{filename}", json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

            names = sorted(sources)
            for i, name in enumerate(names):
                try:
                    resp = client.get(sources[name])
                    resp.raise_for_status()
                    add_bytes(f"app/media/{name}", resp.content)
                except Exception:
                    missing += 1
                if i % 10 == 0:
                    report(f"Packing songs ({i}/{len(names)})", 0.15 + 0.85 * i / max(1, len(names)))
    tmp.replace(zip_path)
    report("Done", 1.0)
    return {"songs": len(sources) - missing, "missing_audio": missing, "bytes": zip_path.stat().st_size}


def export_group(username: str) -> dict:
    """Read one group's library and documents straight from Supabase (secret key)."""
    from backend.supabase_rest import SupabaseRest

    rest = SupabaseRest()
    group = rest.select_one("groups", {"select": "id,display_name", "username": f"eq.{username.lower()}"})
    if not group:
        raise SystemExit(f"No group with username {username!r}")
    rows = rest.select("group_songs", {"select": "song_id,data", "group_id": f"eq.{group['id']}"})
    docs = rest.select("group_kv", {"select": "key,value", "group_id": f"eq.{group['id']}"})
    return {"songs": {r["song_id"]: r["data"] for r in rows}, "docs": {d["key"]: d["value"] for d in docs}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", required=True, help="Username of the group to export.")
    parser.add_argument("--out", default=str(REPO / "build"), help="Output directory.")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / "NewStarHost-offline.zip"
    export = export_group(args.group)
    log(f"Zipping {len(export['songs'])} songs...")
    info = build_bundle(zip_path, export, out / "cache")
    log(f"Done: {zip_path} ({info['bytes'] / 1e6:.0f} MB, {info['songs']} songs)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
