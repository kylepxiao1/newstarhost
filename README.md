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

## Quick Start (Unix/macOS)
```bash
cd ~/newstarhost
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

One command to start backend + virtual cam compositor (quiet logs):
```powershell
$env:UVICORN_LOG_LEVEL="warning"
$env:UVICORN_ACCESS_LOG="false"
$env:INPUT_CAM_INDEX="-1"     # auto-pick first working camera (or set a specific index)
& .\.venv\Scripts\python.exe scripts\run_all.py
```
```bash
UVICORN_LOG_LEVEL=warning UVICORN_ACCESS_LOG=false INPUT_CAM_INDEX=-1 \
  .venv/bin/python scripts/run_all.py
```
Then select the created virtual camera in TikTok LIVE Studio and open `http://localhost:8000/battle/dances` to operate.

Run TikTok listener (automation):
```powershell
$env:TIKTOK_USERNAMES="wildcard_ns viiceversa_ns"
python scripts/tiktok_listener.py
```
```bash
TIKTOK_USERNAMES="wildcard_ns viiceversa_ns" python scripts/tiktok_listener.py
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
- Compatibility knobs (for broader GPU/driver support): `CAM_BACKENDS` (default `DSHOW,ANY,MSMF`), `CV2_USE_OPENCL` (default `auto`, accepts `auto|1|0`), `OVERLAY_GPU_MODE` (default `auto`, accepts `auto|1|0`), `OVERLAY_GPU_MIN_PIXELS` (default `180000`), `OVERLAY_RENDERER_BACKEND` (default `auto`, accepts `auto|cpu|skia`), `VCAM_BACKENDS` (default `unitycapture,obs,auto`), `VCAM_FORMATS` (default `BGR,RGB,I420`), `VCAM_FPS_CANDIDATES` (default `<CAM_FPS>,30,25,24`), `FFMPEG_PROBE_ON_START` (default `1`).
- Recommended for hybrid AMD Radeon + NVIDIA GeForce RTX systems:
```powershell
$env:CAM_BACKENDS="DSHOW,ANY,MSMF"
$env:CV2_USE_OPENCL="auto"
$env:OVERLAY_GPU_MODE="auto"
$env:OVERLAY_GPU_MIN_PIXELS="180000"
$env:OVERLAY_RENDERER_BACKEND="auto"
$env:VCAM_BACKENDS="unitycapture,obs,auto"
$env:VCAM_FORMATS="BGR,RGB,I420"
```
```bash
export CAM_BACKENDS="DSHOW,ANY,MSMF"
export CV2_USE_OPENCL="auto"
export OVERLAY_GPU_MODE="auto"
export OVERLAY_GPU_MIN_PIXELS="180000"
export OVERLAY_RENDERER_BACKEND="auto"
export VCAM_BACKENDS="unitycapture,obs,auto"
export VCAM_FORMATS="BGR,RGB,I420"
```
- Optional GPU-native text/primitives path (Skia/OpenGL):
```powershell
pip install skia-python glfw
$env:OVERLAY_RENDERER_BACKEND="skia"
```
```bash
pip install skia-python glfw
export OVERLAY_RENDERER_BACKEND="skia"
```
Notes: this Skia path currently falls back to CPU renderer when overlay rotation/flip is enabled. Do not use `--collect-all OpenGL` unless your own code directly uses PyOpenGL.
- Performance knobs: `AUTO_QUALITY_ENABLED` (default `1`), `AUTO_QUALITY_LEVELS` (default `1.0,0.9,0.8,0.7`), `AUTO_QUALITY_DOWNGRADE_FRAMES` (default `45`), `AUTO_QUALITY_UPGRADE_FRAMES` (default `240`), `PERF_LOG_INTERVAL_SECS` (default `5`), `CAM_READER_RESTART_STALE_SECS` (default `1.0`).
- GPU logging knobs: `GPU_LOG_INTERVAL_SECS` (default `0`, disabled; set e.g. `5` for periodic logs), `GPU_NVIDIA_SMI_BIN` (default `nvidia-smi`).
- Select the created virtual camera in TikTok LIVE Studio.
- Build a Windows executable (PyInstaller):
```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --name virtual_cam_compositor --console --paths scripts --hidden-import overlay_renderer --hidden-import gpu_overlay_renderer --hidden-import skia --hidden-import glfw --collect-all glfw scripts\virtual_cam_compositor.py
```
```bash
.venv/bin/python -m PyInstaller --noconfirm --onefile --name virtual_cam_compositor --console --paths scripts --hidden-import overlay_renderer --hidden-import gpu_overlay_renderer --hidden-import skia --hidden-import glfw --collect-all glfw scripts/virtual_cam_compositor.py
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
```bash
.venv/bin/python scripts/find_viral_trends.py --topic "dance challenges" --videos 120 --top 25
```

Notes:
- Browser scraping is enforced headless for all runs.
- Videos older than 365 days are excluded by default (`--max-video-age-days` to change, `0` to disable).
- Topic song rows can be upserted to Supabase `topic_trends` (disable with `--no-supabase-upload`).
- During Supabase upload, trendbot now generates `audio_fingerprint` per uploaded row and sets `shared=false` by default.
- At the end of each trendbot run, shared-audio clustering is executed for `topic_trends` rows with `generated_at` in the last 30 days. New cluster IDs start strictly above the max non-null cluster ID from rows older than 30 days.

### Find Likely Shared Audio
Use `scripts/cluster_audio_fingerprints.py` to compare backfilled `audio_fingerprint` values and surface rows likely using the same source audio.

Before first run, add cluster columns:
```sql
alter table public.topic_trends
add column if not exists cluster_id integer;

alter table public.topic_trends
alter column cluster_id type integer
using nullif(regexp_replace(coalesce(cluster_id::text, ''), '[^0-9-]', '', 'g'), '')::integer;

alter table public.topic_trends
add column if not exists shared boolean;

alter table public.topic_trends
alter column shared set default true;

update public.topic_trends
set shared = true
where shared is null;
```

Example:
```powershell
.\.venv\Scripts\python.exe scripts\cluster_audio_fingerprints.py --top 50
```
```bash
.venv/bin/python scripts/cluster_audio_fingerprints.py --top 50
```

Useful flags:
- `--same-topic-only` only compare rows inside the same `topic`.
- `--topic "dance challenges"` limit scan to one topic first.
- `--min-confidence 0.45` tighten match precision.
- Default behavior writes `cluster_id` and `shared=true` to matched rows in `topic_trends`.
- In write mode, rows loaded by the run that are not assigned to a cluster have `cluster_id` set to `null`.
- `--no-write-cluster-updates` keeps the run read-only.

### Fly.io Listener Machine
The `listener` process runs `scripts/listener_multi.sh`, which auto-discovers shard env vars in this format:
- `TIKTOK_USERNAMES_SET_<N>` (required per shard)
- `LISTENER_HEARTBEAT_ID_SET_<N>` (optional, defaults to `listener-set-<N>`)

Shards are launched in numeric order of `<N>`, so adding shard 4+ is an env-only change:
- `TIKTOK_USERNAMES_SET_4=...`
- Optional: `LISTENER_HEARTBEAT_ID_SET_4=listener-set-4`

Notes:
- If no `TIKTOK_USERNAMES_SET_*` vars exist, the launcher keeps the previous built-in defaults for sets 1-3.
- Heartbeat watchdog defaults to shard IDs `listener-set-<N>` based on discovered shard env vars.

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
flyctl ssh sftp get -a newstarhost -g app /path/on/machine/filename.ext C:\path\to\local\filename.ext

# Upload a file to the machine (one-shot)
flyctl ssh sftp put -a newstarhost -g app C:\path\to\local\file.ext /path/on/machine/file.ext
```
```bash
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
flyctl ssh sftp get -a newstarhost -g app /path/on/machine/filename.ext ~/local/filename.ext

# Upload a file to the machine (one-shot)
flyctl ssh sftp put -a newstarhost -g app ~/local/file.ext /path/on/machine/file.ext
```

### Fly.io Discord Worker
- `fly.toml` runs:
  - `app` process: backend + `scripts/s3_sync.sh`
  - `discord` process: `scripts/discord_worker.sh` (separate process group)
    This worker runs:
    an internal trendbot loop, `scripts/discord_verify_bot.py`,
    `scripts/reminder_bot.py` (daily Wildcardz calendar reminder),
    `scripts/top_gifters_bot.py` (top gifters report after livestreams),
    and a supervised summary-only TikTok listener via `scripts/gifter_summary.sh`.
- The `/app/media` Fly volume is scoped to `app` machines only.
- Trendbot defaults (inside `scripts/discord_worker.sh`):
  - `TRENDBOT_INTERVAL=21600` (6 hours)
  - `TRENDBOT_CMD='python scripts/find_viral_trends.py --topic "dance challenges" --videos 120 --top 25 --api-max-attempts 5 --supabase-min-velocity 100'`
- Optional env file for trendbot: `TRENDBOT_ENV_FILE` (default `/app/app.env`).
- Discord reconnect log noise control:
  - `DISCORD_SUPPRESS_RECONNECT_TRACEBACKS=1` (default) suppresses transient Discord gateway reconnect timeout tracebacks.
  - Set `DISCORD_SUPPRESS_RECONNECT_TRACEBACKS=0` for full raw traceback logging while debugging connectivity.
- Wildcardz reminder env:
  - Required: `WILDCARDZ_CALENDAR_ID`, `WILDCARDZ_CALENDAR_API_KEY`
  - Defaults: `WILDCARDZ_REMINDER_TIMEZONE=America/Denver`, `WILDCARDZ_REMINDER_HOUR=12`, `WILDCARDZ_REMINDER_MINUTE=0`
  - Event name match (case/whitespace-insensitive): `WILDCARDZ_REMINDER_EVENT_NAME=Wildcardz Live`
  - Practice event name match included in each live reminder when present: `WILDCARDZ_REMINDER_PRACTICE_EVENT_NAME=Wildcardz Practice`
  - Webhook (required, set in `app.env`): `WILDCARDZ_REMINDER_WEBHOOK`
  - Optional toggles: `WILDCARDZ_REMINDER_ENABLED=1`, `WILDCARDZ_REMINDER_SEND_IF_NONE=0`
- Outfit calendar writer:
  - Script: `scripts/update_calendar_outfits.py`
  - Uses the same `Outfit: ...` description line that `scripts/reminder_bot.py` reads.
  - Required write auth: `GOOGLE_SERVICE_ACCOUNT_FILE=/abs/path/to/service-account.json` or `GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'`
  - Uses `WILDCARDZ_CALENDAR_ID` and defaults to `WILDCARDZ_REMINDER_EVENT_NAME=Wildcardz Live`
  - Dry run: `.venv/bin/python scripts/update_calendar_outfits.py --dry-run`
  - Write June 2026 outfits: `.venv/bin/python scripts/update_calendar_outfits.py`
- Wildcardz top gifters env:
  - Uses same calendar keys: `WILDCARDZ_CALENDAR_ID`, `WILDCARDZ_CALENDAR_API_KEY`
  - Defaults: `WILDCARDZ_TOPGIFTER_TIMEZONE=America/Denver`, `WILDCARDZ_TOPGIFTER_POLL_SECONDS=60`
  - Event name match (case/whitespace-insensitive): `WILDCARDZ_TOPGIFTER_EVENT_NAME=Wildcardz Live`
  - TikTok account filter: `WILDCARDZ_TOPGIFTER_TIKTOK_USERNAME=wildcard_ns`
  - Webhook: `WILDCARDZ_TOPGIFTER_WEBHOOK`
  - Report behavior: starting at 7:15 PM local timezone each day, polls until a matching livestream has completed on that calendar day (so 6:30–9:30 PM streams report after 9:30 PM), excludes members whose nickname contains `the host`, and sends per-member ranked blocks (`#`, `gifter`, `diamonds`, `usd`).
  - Optional toggles: `WILDCARDZ_TOPGIFTER_ENABLED=1`, `WILDCARDZ_TOPGIFTER_SEND_EMPTY=1`
  - Debug trigger: `WILDCARDZ_TOPGIFTER_DEBUG=1` sends immediately on startup for the latest completed matching livestream in the last `WILDCARDZ_TOPGIFTER_LOOKBACK_DAYS` (default `2`)
- Scale process groups explicitly:
```powershell
fly scale count app=1 listener=1 discord=1 -a newstarhost
```
```bash
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
```bash
# On macOS:
brew install ffmpeg
# On Ubuntu/Debian:
sudo apt install ffmpeg
# Or manually set the path:
export FFMPEG_BIN="/usr/local/bin/ffmpeg"
```
If the default URLs fail, set `FFMPEG_URL` to a working archive (zip or 7z). 7z archives require 7-Zip on PATH.

## Analytics Notebooks
Jupyter notebooks are in `analytics/notebooks`

```powershell
.\.venv\Scripts\python.exe -m pip install -r analytics/requirements.txt
.\.venv\Scripts\python.exe -m jupyter lab analytics/notebooks
```
```bash
.venv/bin/python -m pip install -r analytics/requirements.txt
.venv/bin/python -m jupyter lab analytics/notebooks
```

## Summary-only gifter research listener

`bash scripts/gifter_summary.sh` is a separate instance from `listener_multi.sh`.
It listens only for gifts (plus connection lifecycle), aggregates in memory, and
upserts **one row per room observation** to
`public.tiktok_gifter_summaries`. Each row contains a top-20 JSON list and room
summary totals. Every flush replaces that list in place; falling out of the top
20 does not leave extra gifter rows behind. There are no raw-event or per-gift
writes, no chat/like/join collection, no stdout file, and no backend actions.
The existing full-event listeners keep their current configuration.

### Create the table in Supabase's web UI

1. Open the existing Supabase project in the dashboard.
2. Choose **SQL Editor → New query**.
3. Paste the full contents of
   [`supabase/migrations/20261015_gifter_summaries.sql`](supabase/migrations/20261015_gifter_summaries.sql)
   and click **Run**.
4. In **Table Editor**, refresh and check for `public.tiktok_gifter_summaries`.
   RLS deliberately permits no browser/anonymous access; the listener uses the
   existing `SUPABASE_SECRET_KEY` from `app.env`.
5. Verify access and then launch:

```bash
bash scripts/gifter_summary.sh --check
bash scripts/gifter_summary.sh
```

Default hours are **18:30–21:30 America/Denver**, including Utah daylight saving
changes. Outside those hours it waits without connecting. To research other
schedules, run `bash scripts/gifter_summary.sh --all-day`; each summary still
separately counts gifting within your Utah window. Change the dedicated roster
in [`config/gifter_summary.env`](config/gifter_summary.env), or override it:

```bash
GIFTER_SUMMARY_USERNAMES="cloudstarmedia168,kstar_kone,legend.co.ltd" \
  bash scripts/gifter_summary.sh --top 20 --flush-seconds 60
```

The pilot includes K-ONE and LEGEND.Co as outside leads plus the existing `play.zr4`,
`vfm.aero`, and `primalkings_officialjwm` accounts as benchmarks. It does not
silently inherit every existing listener account. Existing accounts can be added
to the same table by adding their handles to this dedicated roster. Running both
listeners on the same account creates a second TikTok connection and leaves the
original listener's full-event storage in place; this addition alone does not
reduce the original tables' growth.

The summary listener runs as a supervised background subroutine inside the
existing **Discord worker**, sharing its machine with the existing bots. There
is no new Fly process group. If the listener exits, its supervisor restarts it
after `GIFTER_SUMMARY_RESTART_SECONDS` (default 60), without restarting the other
Discord tasks. Worker shutdown signals the listener and allows its final flush.
Set `GIFTER_SUMMARY_ENABLED=0` in `app.env` to disable it. An optional
`GIFTER_SUMMARY_CONFIG` selects a different roster env file.

After checking table access, deploy only the Discord group:

```bash
flyctl deploy -a newstarhost --process-groups discord
```

This builds the updated image and targets only the existing Discord group;
its bots restart together as part of that deployment. The app and full-event
listener groups keep running. `fly.toml` is unchanged and remains the normal
configuration. Check `flyctl machines list -a newstarhost`, then view the Discord
machine's logs with `flyctl logs -a newstarhost --machine <DISCORD_MACHINE_ID>`.
Expect `[discord] Starting summary-only TikTok listener`, followed by connections
when the Utah collection window is open and the configured accounts are live.
The container uses the existing `app.env` credentials. These changes have not
been deployed yet.

### Outside leads found October 15, 2026

Prioritize **@cloudstarmedia168**. Its [official site](https://cloudstarlive.com/)
advertises an Asian boy-group dance/singing/king-battle stream daily at
17:30–20:30 PDT, currently 18:30–21:30 MDT: a full match to your window. It also
lists a later show. Recheck its schedule after daylight saving changes.
**Live lookup on October 15 returned `UserNotFoundError` for this handle.**
It is excluded from the active pilot until its current handle is confirmed;
the published schedule is a discovery lead, not a verified connection.

**@kstar_kone** is the next schedule lead. An
[August agency casting post](https://www.filmmakers.co.kr/pr/31575662?page=2)
lists K-ONE at 09:30. If this means Seoul local time, it starts at 18:30 MDT the
previous day (17:30 MST in winter). The post does not explicitly state timezone
or end time, so overlap is a hypothesis to validate. The same post lists
**@kstar_kiss** and **@kstar_kick** at 19:00; assuming KST, those are morning Utah
streams and lower priority for your evening window.

**@legend.co.ltd** is a credible dance LIVE lead: the
[Thai creative culture agency's TikTok event announcement](https://thacca.go.th/news/2025-07-14-08-02-24/)
identifies it as a hip-hop TikTok LIVE creator. Its current schedule, PK format,
and evening-Utah overlap are unverified.

[`research/dance_pk_outside_leads.csv`](research/dance_pk_outside_leads.csv)
contains eight outside leads, including @kstar.see and lower-confidence
@ubgenesix / @ubflash.x discovery leads. The latter two came from a secondary
video index; their TikTok pages could not be independently read. These are
ranked research prospects, not a verified ranking of high-gifting audiences.
No outside account's gifter totals have been measured yet.

For benchmarks, a read-only scan of existing gift rows from the preceding 14
days found the most observed Utah-window gift units on `play.zr4` (2,523,456),
`vfm.aero` (460,181), and `primalkings_officialjwm` (267,737). The saved
[`research/dance_pk_historical_summary.json`](research/dance_pk_historical_summary.json)
contains aggregate results and top gifters. These estimates deduplicate message
IDs and take maximum cumulative count per room/sender/combo group. They depend
on existing ingestion coverage and are not complete TikTok earnings or proof
that every observed gift occurred during a PK battle. Zero observed rows means
no evidence in this dataset, not no gifting.

### Reading results and storage limits

Run the queries in
[`research/gifter_summary_queries.sql`](research/gifter_summary_queries.sql)
in SQL Editor to rank streams and find repeat top gifters across dates/accounts.
`gift_units` means reported gift catalog `diamond_count × quantity`, not USD
spent, coin purchase cost, creator payout, or a viewer's wealth. Combo updates
use positive deltas and message deduplication. Missing-price gifts are counted
in `unknown_price_events` and excluded from amounts. No currency conversion is
invented.

A continuous room observation creates one compact row, regardless of the number
of gift events. A process restart creates a new observation; a new room or a new
local day on reconnection also starts a new observation. An uninterrupted
`--all-day` observation stays assigned to its starting date. Upserts are idempotent
per observation. Run one summary instance per account to avoid double counting.
Only retained top-N contributions can be analyzed later; cross-session gifter
amounts are lower bounds. Room totals include all observed priced gifts.

Failed writes retain compact snapshots in memory for retry, including on room
changes. A forced process crash can lose changes since the last successful
flush; an interrupted combo without a stable group ID can be undercounted. This
process has no durable per-event queue, intentionally limiting storage. Stored
summaries accumulate across sessions; the query file includes optional manual
90-day retention cleanup. No advertising messages are sent by this listener.

Validation: `.venv/bin/python -m unittest discover -s tests -v` covers the
installed TikTok event format, streak counting/deduplication, top-N replacement,
Utah daylight saving boundaries, missing prices, retrying writes exclusively
to the new summary table, reconnects, final-save handling after disconnect
errors, and waiting outside the collection window. Live writes require the web-UI schema setup above.
