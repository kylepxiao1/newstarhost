import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

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
    if raw:
        parts = [part.strip() for part in raw.replace(",", " ").split()]
        seen = set()
        out = []
        for part in parts:
            if not part or part in seen:
                continue
            seen.add(part)
            out.append(part)
        if out:
            return out

    # Default shard-aware behavior:
    # - detect shard indices from TIKTOK_USERNAMES_SET_<N> and LISTENER_HEARTBEAT_ID_SET_<N>
    # - if no shard vars are present, keep launcher-compatible fallback sets 1..3
    indices = set()
    for key in os.environ.keys():
        m = re.match(r"^TIKTOK_USERNAMES_SET_(\d+)$", key)
        if m:
            indices.add(int(m.group(1)))
            continue
        m = re.match(r"^LISTENER_HEARTBEAT_ID_SET_(\d+)$", key)
        if m:
            indices.add(int(m.group(1)))
    if not indices:
        single_id = _env("LISTENER_HEARTBEAT_ID", "").strip()
        if single_id:
            return [single_id]
        indices = {1, 2, 3}

    out = []
    seen = set()
    for idx in sorted(indices):
        shard_id = _env(f"LISTENER_HEARTBEAT_ID_SET_{idx}", "").strip() or f"listener-set-{idx}"
        if shard_id in seen:
            continue
        seen.add(shard_id)
        out.append(shard_id)
    return out


async def _send_webhook(client: httpx.AsyncClient, webhook_url: str, content: str) -> None:
    body = {"content": (content or "")[:1900]}
    try:
        resp = await client.post(webhook_url, json=body)
    except Exception as exc:
        logging.warning("Failed to POST webhook alert: %s", exc)
        return
    if not resp.is_success:
        logging.warning("Webhook returned status=%s body=%s", resp.status_code, (resp.text or "").strip())


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


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("listener-heartbeat-watchdog")

    supabase_url = _resolve_supabase_url().rstrip("/")
    supabase_key = _env("SUPABASE_SECRET_KEY", "").strip()
    webhook_url = _env("LISTENER_HEARTBEAT_ALERT_WEBHOOK", "").strip()
    table = _env("LISTENER_HEARTBEAT_TABLE", "listener_heartbeats").strip() or "listener_heartbeats"
    check_interval = max(15, _env_int("LISTENER_HEARTBEAT_CHECK_INTERVAL_SECONDS", 60))
    stale_seconds = max(30, _env_int("LISTENER_HEARTBEAT_STALE_SECONDS", 300))
    include_log_lines = max(1, _env_int("LISTENER_HEARTBEAT_ALERT_LOG_LINES", 5))
    stale_confirm_checks = max(1, _env_int("LISTENER_HEARTBEAT_STALE_CONFIRM_CHECKS", 2))
    recovery_confirm_checks = max(1, _env_int("LISTENER_HEARTBEAT_RECOVERY_CONFIRM_CHECKS", 2))
    # Default off to avoid noisy "recovered" messages during transient flaps.
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
        logger.error("Missing LISTENER_HEARTBEAT_ALERT_WEBHOOK; watchdog cannot alert.")
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
        "Listener heartbeat watchdog started: table=%s ids=%s stale_after=%ss interval=%ss stale_confirm=%s recovery_confirm=%s",
        table,
        ",".join(watch_ids),
        stale_seconds,
        check_interval,
        stale_confirm_checks,
        recovery_confirm_checks,
    )
    logger.info(
        "Heartbeat watchdog startup delay active: sleeping %.0fs (2 * LISTENER_HEARTBEAT_INTERVAL_SECONDS=%.2fs)",
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

                # Alert once when transitioning from healthy -> stale, after
                # N consecutive stale checks to avoid flapping spam.
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
                        message = (
                            f"Listener heartbeat stale for `{watch_id}` ({', '.join(details)})."
                        )
                        if last_log:
                            message += f"\nLast log: `{last_log[:600]}`"
                        if recent_lines:
                            clipped = "\n".join(recent_lines)[-1200:]
                            message += f"\nRecent logs:\n```text\n{clipped}\n```"
                    await _send_webhook(client, webhook_url=webhook_url, content=message)
                    state["stale"] = True

                should_recover = (
                    state["stale"]
                    and (not stale)
                    and int(state.get("healthy_hits", 0)) >= recovery_confirm_checks
                )
                if should_recover and send_recovery_alert:
                    recovery_age = int(age_seconds or 0)
                    await _send_webhook(
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


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
