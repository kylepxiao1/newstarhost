import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

from utils import _env


def _env_int(key: str, default: int) -> int:
    value = _env(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    value = _env(key, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _parse_iso(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_supabase_url() -> str:
    explicit_url = _env("SUPABASE_URL", "").strip()
    if explicit_url:
        return explicit_url
    project_id = _env("SUPABASE_PROJECT_ID", "").strip()
    if project_id:
        return f"https://{project_id}.supabase.co"
    return ""


def _parse_watch_ids() -> list[str]:
    raw = _env("LISTENER_HEARTBEAT_WATCH_IDS", "").strip()
    if not raw:
        raw = _env("LISTENER_HEARTBEAT_ID", "").strip()
    if not raw:
        raw = "listener"
    parts = [part.strip() for part in raw.replace(",", " ").split()]
    seen = set()
    out = []
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out or ["listener"]


def _parse_kv_tokens(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for token in str(text or "").strip().split():
        if ":" not in token:
            continue
        key, value = token.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def _parse_list(value: str) -> list[str]:
    items = [part.strip() for part in str(value or "").replace(",", " ").split()]
    deduped: list[str] = []
    seen = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _load_env_file(path: str) -> None:
    file_path = str(path or "").strip()
    if not file_path or not os.path.isfile(file_path):
        return
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip()
    except Exception as exc:
        logging.warning("Failed to load env file %s: %s", file_path, exc)


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _send_result(text: str = "OK") -> None:
    payload = str(text)
    sys.stdout.write(f"RESULT {len(payload)}\n{payload}")
    sys.stdout.flush()


def _send_ready() -> None:
    sys.stdout.write("READY\n")
    sys.stdout.flush()


async def _send_webhook_async(client: httpx.AsyncClient, webhook_url: str, content: str) -> None:
    body = {"content": (content or "")[:1900]}
    try:
        resp = await client.post(webhook_url, json=body)
    except Exception as exc:
        logging.warning("Failed to POST webhook alert: %s", exc)
        return
    if not resp.is_success:
        logging.warning("Webhook returned status=%s body=%s", resp.status_code, (resp.text or "").strip())


def _send_webhook_sync(client: httpx.Client, webhook_url: str, content: str) -> None:
    body = {"content": (content or "")[:1900]}
    try:
        resp = client.post(webhook_url, json=body)
    except Exception as exc:
        logging.warning("Webhook POST error: %s", exc)
        return
    if not resp.is_success:
        logging.warning("Webhook POST failed status=%s body=%s", resp.status_code, (resp.text or "").strip())


async def _fetch_heartbeat_row(
    client: httpx.AsyncClient,
    table: str,
    listener_id: str,
) -> Optional[dict]:
    resp = await client.get(
        f"/{table}",
        params={
            "select": (
                "listener_id,updated_at,updated_unix,status,hostname,machine_id,region,"
                "active_listener_count,connected_listener_count,last_log_line,recent_logs"
            ),
            "listener_id": f"eq.{listener_id}",
        },
    )
    if not resp.is_success:
        raise RuntimeError(f"heartbeat query failed status={resp.status_code} body={(resp.text or '').strip()}")
    payload = resp.json()
    if isinstance(payload, list) and payload:
        row = payload[0]
        if isinstance(row, dict):
            return row
    return None


def _heartbeat_age_seconds(row: dict, now_dt: datetime) -> Optional[float]:
    updated_at = _parse_iso(str(row.get("updated_at") or ""))
    if updated_at is not None:
        return max(0.0, (now_dt - updated_at).total_seconds())
    updated_unix = row.get("updated_unix")
    try:
        updated_unix = int(updated_unix)
    except Exception:
        return None
    return max(0.0, now_dt.timestamp() - float(updated_unix))


async def run_heartbeat_mode() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[watchdog] [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("watchdog")

    supabase_url = _resolve_supabase_url().rstrip("/")
    supabase_key = _env("SUPABASE_SECRET_KEY", "").strip()
    webhook_url = _env("WATCHDOG_ALERT_WEBHOOK", "").strip()
    table = _env("LISTENER_HEARTBEAT_TABLE", "listener_heartbeats").strip() or "listener_heartbeats"
    check_interval = max(15, _env_int("LISTENER_HEARTBEAT_CHECK_INTERVAL_SECONDS", 60))
    stale_seconds = max(30, _env_int("LISTENER_HEARTBEAT_STALE_SECONDS", 300))
    include_log_lines = max(1, _env_int("LISTENER_HEARTBEAT_ALERT_LOG_LINES", 5))
    stale_confirm_checks = max(1, _env_int("LISTENER_HEARTBEAT_STALE_CONFIRM_CHECKS", 2))
    recovery_confirm_checks = max(1, _env_int("LISTENER_HEARTBEAT_RECOVERY_CONFIRM_CHECKS", 2))
    send_recovery_alert = str(_env("LISTENER_HEARTBEAT_SEND_RECOVERY_ALERT", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    watch_ids = _parse_watch_ids()
    listener_heartbeat_interval = max(1.0, _env_float("LISTENER_HEARTBEAT_INTERVAL_SECONDS", 60.0))
    startup_delay_seconds = max(1.0, 2.0 * listener_heartbeat_interval)

    if not supabase_url or not supabase_key:
        logger.error("Missing Supabase credentials for heartbeat watchdog.")
        return
    if not webhook_url:
        logger.error("Missing WATCHDOG_ALERT_WEBHOOK; watchdog cannot alert.")
        return

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
    states = {
        watch_id: {
            "stale": False,
            "stale_hits": 0,
            "healthy_hits": 0,
        }
        for watch_id in watch_ids
    }
    logger.info(
        "Watchdog started: table=%s ids=%s stale_after=%ss interval=%ss stale_confirm=%s recovery_confirm=%s",
        table,
        ",".join(watch_ids),
        stale_seconds,
        check_interval,
        stale_confirm_checks,
        recovery_confirm_checks,
    )
    logger.info(
        "Watchdog startup delay active: sleeping %.0fs (2 * LISTENER_HEARTBEAT_INTERVAL_SECONDS=%.2fs)",
        startup_delay_seconds,
        listener_heartbeat_interval,
    )
    await asyncio.sleep(startup_delay_seconds)

    async with httpx.AsyncClient(
        base_url=f"{supabase_url}/rest/v1",
        headers=headers,
        timeout=timeout,
    ) as client:
        while True:
            now_dt = datetime.now(timezone.utc)
            for watch_id in watch_ids:
                state = states[watch_id]
                row = None
                query_error = None
                stale_reason = ""
                try:
                    row = await _fetch_heartbeat_row(client, table=table, listener_id=watch_id)
                except Exception as exc:
                    query_error = str(exc)

                stale = False
                age_seconds = None
                if query_error:
                    stale = True
                    stale_reason = "query_error"
                elif row is None:
                    stale = True
                    stale_reason = "row_unavailable"
                else:
                    age_seconds = _heartbeat_age_seconds(row, now_dt)
                    stale = age_seconds is None or age_seconds > stale_seconds
                    if age_seconds is None:
                        stale_reason = "age_unreadable"
                    elif age_seconds > stale_seconds:
                        stale_reason = "age_exceeded"

                if stale:
                    state["stale_hits"] = int(state.get("stale_hits", 0)) + 1
                    state["healthy_hits"] = 0
                else:
                    state["healthy_hits"] = int(state.get("healthy_hits", 0)) + 1
                    state["stale_hits"] = 0

                should_alert = (
                    (not state["stale"])
                    and stale
                    and int(state.get("stale_hits", 0)) >= stale_confirm_checks
                )
                if should_alert:
                    if query_error:
                        message = (
                            f"Listener heartbeat stale for `{watch_id}`: failed to query Supabase heartbeat row. "
                            f"Error: `{query_error[:500]}`"
                        )
                    elif row is None:
                        message = (
                            f"Listener heartbeat stale for `{watch_id}`: heartbeat row is currently unavailable in "
                            f"`{table}` (reason=`{stale_reason or 'row_unavailable'}`). "
                            "Most likely cause: heartbeat service is down."
                        )
                    else:
                        last_log = str(row.get("last_log_line") or "").strip()
                        recent_logs = row.get("recent_logs")
                        if isinstance(recent_logs, list):
                            recent_lines = [str(line) for line in recent_logs if str(line).strip()][-include_log_lines:]
                        else:
                            recent_lines = []
                        details = [
                            f"age={int(age_seconds or 0)}s",
                            f"host={row.get('hostname') or 'n/a'}",
                            f"machine={row.get('machine_id') or 'n/a'}",
                            f"connected={row.get('connected_listener_count')}/{row.get('active_listener_count')}",
                        ]
                        message = f"Listener heartbeat stale for `{watch_id}` ({', '.join(details)})."
                        if last_log:
                            message += f"\nLast log: `{last_log[:600]}`"
                        if recent_lines:
                            clipped = "\n".join(recent_lines)[-1200:]
                            message += f"\nRecent logs:\n```text\n{clipped}\n```"
                    await _send_webhook_async(client, webhook_url=webhook_url, content=message)
                    state["stale"] = True

                should_recover = (
                    state["stale"]
                    and (not stale)
                    and int(state.get("healthy_hits", 0)) >= recovery_confirm_checks
                )
                if should_recover and send_recovery_alert:
                    recovery_age = int(age_seconds or 0)
                    await _send_webhook_async(
                        client,
                        webhook_url=webhook_url,
                        content=(
                            f"Listener heartbeat recovered for `{watch_id}`. "
                            f"Latest heartbeat age is {recovery_age}s."
                        ),
                    )
                if should_recover:
                    state["stale"] = False
            await asyncio.sleep(check_interval)


def _process_should_alert(event_name: str, payload: Dict[str, str]) -> bool:
    if event_name == "PROCESS_STATE_EXITED":
        return payload.get("expected", "") == "0"
    if event_name in {"PROCESS_STATE_FATAL", "PROCESS_STATE_BACKOFF"}:
        return True
    return False


def _format_process_message(event_name: str, payload: Dict[str, str]) -> str:
    process_name = payload.get("processname", "unknown")
    group_name = payload.get("groupname", process_name)
    from_state = payload.get("from_state", "unknown")
    expected = payload.get("expected", "n/a")
    pid = payload.get("pid", "n/a")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    oom_hint = ""
    if from_state.upper() == "RUNNING":
        oom_hint = "\nIf this was OOM-related, check Fly logs and machine memory usage around this timestamp."

    return (
        f"[watchdog] Supervisord process alert\n"
        f"time={now}\n"
        f"event={event_name}\n"
        f"process={process_name}\n"
        f"group={group_name}\n"
        f"from_state={from_state}\n"
        f"expected={expected}\n"
        f"pid={pid}"
        f"{oom_hint}"
    )


def _is_process_running_event(event_name: str) -> bool:
    return event_name == "PROCESS_STATE_RUNNING"


def run_process_events_mode() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [watchdog-events] [%(levelname)s] %(message)s",
    )

    env_file = os.environ.get("WATCHDOG_ENV_FILE", "/app/app.env")
    _load_env_file(env_file)

    enabled = os.environ.get("WATCHDOG_PROCESS_ALERTS_ENABLED", "1")
    if not _is_truthy(enabled):
        logging.warning("Process alerts disabled via WATCHDOG_PROCESS_ALERTS_ENABLED=%s", enabled)

    webhook = os.environ.get("WATCHDOG_ALERT_WEBHOOK", "").strip()

    monitor_programs = _parse_list(
        os.environ.get(
            "WATCHDOG_MONITOR_PROGRAMS",
            "app s3-sync listener discord-verify-bot trendbot",
        )
    )
    monitor_programs_set = set(monitor_programs)

    exclude_programs = _parse_list(
        os.environ.get(
            "WATCHDOG_EXCLUDE_PROGRAMS",
            "watchdog watchdog-process-events",
        )
    )
    exclude_programs_set = set(exclude_programs)

    cooldown_seconds = 300
    try:
        cooldown_seconds = max(0, int(os.environ.get("WATCHDOG_ALERT_COOLDOWN_SECONDS", "300")))
    except Exception:
        cooldown_seconds = 300

    startup_grace_seconds = 300
    try:
        startup_grace_seconds = max(0, int(os.environ.get("WATCHDOG_PROCESS_STARTUP_GRACE_SECONDS", "300")))
    except Exception:
        startup_grace_seconds = 300
    started_at = time.time()

    logging.info(
        "Starting process event watchdog monitor_programs=%s exclude_programs=%s cooldown=%ss startup_grace=%ss webhook_set=%s",
        ",".join(monitor_programs) if monitor_programs else "(all)",
        ",".join(exclude_programs) if exclude_programs else "(none)",
        cooldown_seconds,
        startup_grace_seconds,
        bool(webhook),
    )

    client = httpx.Client(timeout=httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0))
    last_alert_by_process: dict[str, float] = {}
    incident_open_by_process: dict[str, bool] = {}

    try:
        while True:
            _send_ready()
            header_line = sys.stdin.readline()
            if not header_line:
                logging.warning("Supervisord event stream ended.")
                break

            headers = _parse_kv_tokens(header_line)
            event_name = headers.get("eventname", "")
            length_text = headers.get("len", "0")
            try:
                payload_len = int(length_text)
            except Exception:
                payload_len = 0
            payload_text = sys.stdin.read(payload_len) if payload_len > 0 else ""
            payload = _parse_kv_tokens(payload_text)

            _send_result("OK")

            process_name = payload.get("processname", "")
            if not process_name:
                continue
            if process_name in exclude_programs_set:
                continue
            if monitor_programs_set and process_name not in monitor_programs_set:
                continue
            if _is_process_running_event(event_name):
                # Re-arm alerting after a supervised process becomes healthy again.
                incident_open_by_process[process_name] = False
                continue
            if not _process_should_alert(event_name, payload):
                continue

            now_ts = time.time()
            if startup_grace_seconds > 0 and (now_ts - started_at) < startup_grace_seconds:
                continue

            # Avoid multi-alert spam for the same outage across BACKOFF/FATAL/EXITED events.
            if incident_open_by_process.get(process_name, False):
                continue

            last_ts = float(last_alert_by_process.get(process_name, 0.0))
            if cooldown_seconds > 0 and (now_ts - last_ts) < cooldown_seconds:
                continue
            last_alert_by_process[process_name] = now_ts
            incident_open_by_process[process_name] = True

            logging.warning("Process event: %s payload=%s", event_name, payload_text.strip())

            if _is_truthy(enabled) and webhook:
                _send_webhook_sync(client, webhook, _format_process_message(event_name, payload))
    finally:
        try:
            client.close()
        except Exception:
            pass

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog runtime and process-event monitor.")
    parser.add_argument(
        "--mode",
        choices=["heartbeat", "process-events"],
        default="heartbeat",
        help="Watchdog mode to run.",
    )
    args = parser.parse_args()

    if args.mode == "process-events":
        return run_process_events_mode()

    try:
        asyncio.run(run_heartbeat_mode())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
