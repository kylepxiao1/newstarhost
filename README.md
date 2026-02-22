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
- `scripts/sh/` - Supervisord runtime wrappers (`run_supervisor_program.sh`, `run_with_memory_cap.sh`, `run_program.sh`).
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
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name virtual_cam_compositor --console scripts\virtual_cam_compositor.py
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

### Fly.io Single Machine (supervisord)
Fly deploy now runs one machine with six `supervisord` programs:
- `app`: FastAPI web app (`backend.main:app`)
- `listener`: `scripts/tiktok_listener.py` (auto-retry on crash)
- `discord-verify-bot`: `scripts/discord_verify_bot.py` (auto-retry on crash)
- `watchdog`: `scripts/watchdog.py --mode heartbeat` (auto-retry on crash)
- `s3-sync`: scheduled by Supercronic at **4:00 AM America/Denver** (daily)
- `trendbot`: scheduled by Supercronic at **12:00 AM and 12:00 PM America/Denver** (daily)

Web app bind behavior:
- The `app` program starts Uvicorn with `--host $APP_HOST --port $PORT` (`APP_HOST=0.0.0.0`, `PORT=8080` in `fly.toml`).
- Slow media maintenance (rename/scan/cleanup) now runs in a background startup task so the web port can bind quickly for Fly proxy checks.
- Deploy defaults set `UVICORN_LOG_LEVEL=info` and `UVICORN_ACCESS_LOG=true` so `[app]` log filtering has regular request/startup lines.
- Listener verbosity is controllable with `LISTENER_LOG_LEVEL` (set to `WARNING` by default in `fly.toml` to reduce noise).

Supervisord also runs an event listener:
- `watchdog-process-events`: runs `scripts/watchdog.py --mode process-events` and posts webhook alerts when supervised programs crash unexpectedly (including OOM-style exits).
  It dedupes alerts per process incident (no repeated BACKOFF/FATAL/EXITED spam for the same outage) and rearms once that process returns to `RUNNING`.

Machine sizing in `fly.toml`:
- Shared VM memory: `2gb` (`memory_mb=2048`)
- CPU: `2`

Each supervised process has its own memory cap:
- `APP_MEMORY_LIMIT_MB`
- `LISTENER_MEMORY_LIMIT_MB`
- `DISCORD_VERIFY_BOT_MEMORY_LIMIT_MB`
- `WATCHDOG_MEMORY_LIMIT_MB`
- `WATCHDOG_PROCESS_EVENTS_MEMORY_LIMIT_MB`
- `S3_SYNC_MEMORY_LIMIT_MB`
- `TRENDBOT_MEMORY_LIMIT_MB`

Adding more processes is pattern-based:
- Continuous worker: add a `[program:...]` entry in `deploy/supervisord.conf` that runs `scripts/sh/run_supervisor_program.sh`.
- Scheduled worker: add a cron file under `deploy/cron/` and a `supercronic` program entry in `deploy/supervisord.conf`.

Crash alert webhook knobs (used by `watchdog` heartbeat mode and `watchdog-process-events`):
- `WATCHDOG_ALERT_WEBHOOK`
- `WATCHDOG_PROCESS_ALERTS_ENABLED` (`1` by default)
- `WATCHDOG_ALERT_COOLDOWN_SECONDS` (`300` by default)
- `WATCHDOG_PROCESS_STARTUP_GRACE_SECONDS` (`300` by default; suppresses deploy-time startup/backoff noise)
- `WATCHDOG_MONITOR_PROGRAMS` (default monitors `app,s3-sync,listener,discord-verify-bot,trendbot`)

#### Logging / Grep
`[program:*]` processes are wrapped by `scripts/sh/run_supervisor_program.sh` and are prefixed with process tags, for example:
- `[app]`
- `[listener]`
- `[discord-verify-bot]`
- `[watchdog]`
- `[s3-sync]`
- `[trendbot]`

`[eventlistener:watchdog-process-events]` is not wrapped with that same stdout/stderr wrapper because Supervisor event listeners must keep a strict stdout protocol (`READY` / `RESULT`).
Its human-readable log lines are tagged as:
- `[watchdog-events]`

Quick verification:
```powershell
flyctl logs -a newstarhost | Select-String '\[(app|listener|discord-verify-bot|watchdog|s3-sync|trendbot|watchdog-events)\]'
```

If Fly prints `app is not listening on 0.0.0.0:8080`, check app startup lines first:
```powershell
flyctl logs -a newstarhost | Select-String '\[app\]'
```

PowerShell examples:
```powershell
# listener-only lines
flyctl logs -a newstarhost | Select-String '\[listener\]'

# app-only lines
flyctl logs -a newstarhost | Select-String '\[app\]'

# trendbot-only lines
flyctl logs -a newstarhost | Select-String '\[trendbot\]'
```

Bash examples:
```bash
flyctl logs -a newstarhost | grep '\[listener\]'
flyctl logs -a newstarhost | grep '\[app\]'
flyctl logs -a newstarhost | grep '\[trendbot\]'
```

Useful Fly commands:
```powershell
# Authenticate Fly CLI
flyctl auth login

# List machines
flyctl machines list -a newstarhost

# Ensure exactly one machine is running
fly scale count 1 -a newstarhost

# SSH into the machine
flyctl ssh console -a newstarhost

# Start the machine
fly machines start <APP_MACHINE_ID>

# Check machine logs (all supervised processes)
flyctl logs -a newstarhost --machine <APP_MACHINE_ID>

# Download a file from the machine (one-shot)
flyctl ssh sftp get -a newstarhost /path/on/machine/filename.ext C:\path\to\local\filename.ext

# Upload a file to the machine (one-shot)
flyctl ssh sftp put -a newstarhost C:\path\to\local\file.ext /path/on/machine/file.ext
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
