# TikTok LIVE Host Stack

Windows-first control stack for automated TikTok LIVE battles. FastAPI backend orchestrates overlays/scoreboard, broadcasts realtime state via WebSockets, exposes a control panel, and listens to TikTok LIVE events through the unofficial `tiktoklive` library. Two output paths: (1) lightweight virtual camera compositor (no OBS), or (2) OBS browser overlay if you prefer OBS. Slots are neutral (`slot_one` / `slot_two`); no left/right or manual participant entry in the UI.

## Repository Layout
- `backend/` - FastAPI app, state manager, static overlay/control UI.
- `backend/static/overlay.html` - Browser overlay (connects to `/ws/state`, dotted black center line).
- `backend/static/battle_dances.html` - Web control panel at `/battle/dances` (start/end battle, score bumps, overlay toggles, read slot names).
- `backend/static/app.html` - Single-page shell that keeps audio alive while switching tabs. Open at `/app` (now default `/`).
- `scripts/tiktok_listener.py` - TikTok LIVE automation listener.
- `scripts/virtual_cam_compositor.py` - Lightweight virtual camera compositor (no OBS; overlays + camera into a virtual cam).
- `scripts/run_all.py` - One-shot launcher for backend + virtual cam compositor.
- `scripts/find_viral_trends.py` - Public-data TikTok topic trend/song radar with optional Supabase upload.
- `requirements.txt` - Python dependencies.

## Quick Start (Windows)
```powershell
cd C:\Users\kylep\OneDrive\Desktop\newstarhost
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass  # if activation is blocked
.\.venv\Scripts\activate
pip install -r requirements.txt
```

One command to start backend + virtual cam compositor (quiet logs):
```powershell
$env:UVICORN_LOG_LEVEL="warning"
$env:UVICORN_ACCESS_LOG="false"
$env:INPUT_CAM_INDEX="-1"     # auto-pick first working camera (or set a specific index)
& .\.venv\Scripts\python.exe scripts\run_all.py
```
Then select the created virtual camera in TikTok LIVE Studio and open `http://localhost:8000/battle/dances` to operate.

Run TikTok listener (automation):
```powershell
$env:TIKTOK_USERNAMES="wildcard_boys afterdark_ns"
python scripts/tiktok_listener.py
```

## Backend (FastAPI)
Endpoints:
- `POST /battle/start` - start battle (`{"mode":"rapid"}` optional).
- `POST /battle/end`
- `POST /battle/slot/{slot_one|slot_two}` - set a single slot (payload uses `slot_one`/`slot_two` keys).
- `POST /battle/slots/import` - set both slots (`{"slot_one":"A","slot_two":"B"}`), intended for TikTok Studio automation when "Start now" is pressed.
- `POST /overlay/{name}/show|hide` - toggle overlay sources (OBS-only).
- `POST /score/{slot_one|slot_two}/add` - increment score (`{"amount":1}`).
- `GET /state` - current state.
- `GET /battle/dances` - control UI.
- `GET /overlay` - overlay HTML (used by OBS path).
- `WS /ws/state` - realtime state feed (overlays & control UI subscribe).

Config via env (see `backend/config.py`): `OBS_HOST`, `OBS_PORT`, `OBS_PASSWORD`, `SCENE_BATTLE`, `TEXT_SOURCE_SLOT_ONE_NAME`, `OVERLAY_SOURCES`, `CAM_WIDTH`, `CAM_HEIGHT`, `CAM_FPS`, `INPUT_CAM_INDEX`, etc.

## Output Options

### A) Lightweight virtual camera (no OBS)
- Uses `scripts/virtual_cam_compositor.py` with `pyvirtualcam` + `opencv` to capture your real camera, draw names/scores/mode + dotted center line, and expose a virtual camera device.
- Configure env vars as needed: `INPUT_CAM_INDEX`, `CAM_WIDTH`, `CAM_HEIGHT`, `CAM_FPS`, `STATE_POLL_SECS`.
- Select the created virtual camera in TikTok LIVE Studio.
- Build a Windows executable (PyInstaller):
```powershell
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name virtual_cam_compositor --console --paths scripts --hidden-import overlay_renderer scripts\virtual_cam_compositor.py
```

### B) OBS-based overlay
- Scenes: `MainScene`, `BattleScene` (override via env) if you choose OBS.
- `BattleScene` sources:
  - Text: `SlotOneName`, `SlotTwoName`, `SlotOneScore`, `SlotTwoScore`.
  - Browser source: `http://localhost:8000/overlay` (includes dotted black vertical line).
  - Optional overlays matching `OVERLAY_SOURCES` (default: `BattleLowerThird`, `BurstOverlay`, `CenterDottedLine`).
- Start OBS Virtual Camera or RTMP if available; choose it in TikTok LIVE Studio.

## Control Panel
- Start/End battle.
- Increment scores per slot.
- Toggle overlays (OBS path).
- Read-only slot names (populated by automation; Refresh/Sync pulls current state).

## Overlay
- Browser overlay (`/overlay`) subscribes to `/ws/state`, shows slot_one/slot_two names, scores, battle mode, status, and a dotted black center line.
- Virtual cam compositor draws the same elements directly onto frames without OBS.

## TikTok LIVE Listener
`scripts/tiktok_listener.py` (async):
- Connects via `tiktoklive` to `TIKTOK_USERNAME`.
- Logs raw stdout to `stdout.log`.
- Commands: `!battle` starts, `!end` stops, `!slots A|B` sets slot_one/slot_two (fallback to env defaults).
- Uses native events: `LinkMicBattleEvent` to start battles, `LinkMicArmiesEvent` to track scores, heuristics as backup.
- Calls backend: `/battle/start`, `/battle/end`, `/battle/slots/import`, `/score/.../add`.

## Viral Trend Radar
`scripts/find_viral_trends.py` collects topic-relevant TikTok trend signals from publicly available sources and extracts song-linked trend candidates.

Example:
```powershell
.\.venv\Scripts\python.exe scripts\find_viral_trends.py --topic "dance challenges" --videos 120 --top 25
```

Notes:
- Browser scraping is enforced headless for all runs.
- Videos older than 365 days are excluded by default (`--max-video-age-days` to change, `0` to disable).
- Topic song rows can be upserted to Supabase `topic_trends` (disable with `--no-supabase-upload`).

### Fly.io Listener Machine
The `listener` process now runs `scripts/listener_multi.sh`, which launches 3 listener child processes with shard-specific usernames and heartbeat IDs:
- set 1 (`listener-set-1`): `wildcard_boys`
- set 2 (`listener-set-2`): `afterdark_ns,valentinananaaaa,cardin_v_`
- set 3 (`listener-set-3`): `snyki.live,sv_cloveris,superv_sv,visiondance.leo,millarboys233,primalkings_officialjwm,sunsetnova__,bdcuphedc3,play.zr4,play.hero8,vfm.aero,chaos001inc`

Shard env vars live in `app.env`:
- `TIKTOK_USERNAMES_SET_1`, `TIKTOK_USERNAMES_SET_2`, `TIKTOK_USERNAMES_SET_3`
- `LISTENER_HEARTBEAT_ID_SET_1`, `LISTENER_HEARTBEAT_ID_SET_2`, `LISTENER_HEARTBEAT_ID_SET_3`
- `LISTENER_HEARTBEAT_WATCH_IDS=listener-set-1,listener-set-2,listener-set-3`

Useful commands when running the listener on Fly.io:
```powershell
# Authenticate Fly CLI
flyctl auth login

# List machines
flyctl machines list -a newstarhost

# SSH into the machine
flyctl ssh console -a newstarhost -g app
flyctl ssh console -a newstarhost -g listener

# Start the listener machine
fly machines start <APP_MACHINE_ID>

# Check the listener logs
flyctl logs -a newstarhost --machine <APP_MACHINE_ID>

# Download a file from the machine (one-shot)
flyctl ssh sftp get -a newstarhost /path/on/machine/filename.ext C:\path\to\local\filename.ext

# Upload a file to the machine (one-shot)
flyctl ssh sftp put -a newstarhost C:\path\to\local\file.ext /path/on/machine/file.ext
```

### Fly.io Discord Worker
- `fly.toml` runs:
  - `app` process: backend + `scripts/s3_sync.sh`
  - `discord` process: `scripts/discord_worker.sh` (separate process group)
    This worker runs both:
    an internal trendbot loop and `scripts/discord_verify_bot.py`.
- The `/app/media` Fly volume is scoped to `app` machines only.
- Trendbot defaults (inside `scripts/discord_worker.sh`):
  - `TRENDBOT_INTERVAL=43200` (12 hours)
  - `TRENDBOT_CMD='python scripts/find_viral_trends.py --topic "dance challenges" --videos 120 --top 25 --api-max-attempts 5 --supabase-min-velocity 100'`
- Optional env file for trendbot: `TRENDBOT_ENV_FILE` (default `/app/app.env`).
- Scale process groups explicitly:
```powershell
fly scale count app=1 listener=1 discord=1 -a newstarhost
```

## Audio Routing (Windows)
- With virtual cam: route mic/system audio via VB-Cable or VoiceMeeter; select the same input in TikTok LIVE Studio.
- With OBS: configure monitoring device and feed Virtual Camera/RTMP as usual.

## Running Everything
- One-shot (no OBS): `python scripts/run_all.py` ? choose the virtual camera in TikTok LIVE Studio.
- OBS path: start OBS, add Browser Source `/overlay`, start OBS Virtual Camera or RTMP, run backend, run listener.
- Control UI: `http://localhost:8000/battle/dances` or use the SPA shell `http://localhost:8000/app` (default `/`).
- Automation: `python scripts/tiktok_listener.py`.

## Installing FFmpeg (local)
Run from repo root:
```powershell
# Optionally override URL:
$env:FFMPEG_URL="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
powershell -ExecutionPolicy Bypass -File .\scripts\install_ffmpeg.ps1
# Set ffmpeg for this session (path printed by the script):
$env:FFMPEG_BIN="C:\Users\kylep\OneDrive\Desktop\newstarhost\scripts\ffmpeg-bin\...\ffmpeg.exe"
```
If the default URLs fail, set `FFMPEG_URL` to a working archive (zip or 7z). 7z archives require 7-Zip on PATH.

## Analytics Notebooks
Jupyter notebooks are in `analytics/notebooks`

```powershell
.\.venv\Scripts\python.exe -m pip install -r analytics/requirements.txt
.\.venv\Scripts\python.exe -m jupyter lab analytics/notebooks
```
