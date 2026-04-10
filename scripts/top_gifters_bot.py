from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

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


def _env_bool(key: str, default: bool = False) -> bool:
    value = _env(key, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_token(text: str) -> str:
    return _normalize_space(text).lower()


def _to_int(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        try:
            return int(float(text))
        except Exception:
            return None


def _gift_row_diamond_amount(row: dict) -> int:
    amount_value = _to_int(row.get("amount_value"))
    if amount_value is not None:
        return max(0, amount_value)
    diamond_count = _to_int(row.get("diamond_count"))
    if diamond_count is None:
        return 0
    repeat_count = _to_int(row.get("repeat_count"))
    combo_count = _to_int(row.get("combo_count"))
    multiplier = repeat_count if repeat_count is not None else combo_count
    if multiplier is None:
        multiplier = 1
    return max(0, diamond_count * max(1, multiplier))


def _format_google_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_event_datetime(value: dict[str, Any]) -> datetime | None:
    if not isinstance(value, dict):
        return None
    raw_dt = _normalize_space(str(value.get("dateTime") or ""))
    tz_name = _normalize_space(str(value.get("timeZone") or ""))
    if raw_dt:
        dt_text = raw_dt[:-1] + "+00:00" if raw_dt.endswith("Z") else raw_dt
        try:
            parsed = datetime.fromisoformat(dt_text)
        except Exception:
            return None
        if parsed.tzinfo is None:
            if tz_name:
                try:
                    parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
                except Exception:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raw_date = _normalize_space(str(value.get("date") or ""))
    if raw_date:
        try:
            parsed_date = date.fromisoformat(raw_date)
        except Exception:
            return None
        if tz_name:
            try:
                zone = ZoneInfo(tz_name)
            except Exception:
                zone = timezone.utc
        else:
            zone = timezone.utc
        return datetime.combine(parsed_date, time(hour=0, minute=0), tzinfo=zone)
    return None


def _is_all_day_event(event: dict[str, Any]) -> bool:
    start = event.get("start")
    if not isinstance(start, dict):
        return False
    return bool(_normalize_space(str(start.get("date") or "")) and not _normalize_space(str(start.get("dateTime") or "")))


def _format_clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        logging.warning("Failed to write top-gifters state file: %s", path)


def _event_matches(summary: str, target_name: str) -> bool:
    normalized_summary = _normalize_token(summary)
    normalized_target = _normalize_token(target_name)
    if not normalized_summary or not normalized_target:
        return False
    return normalized_target in normalized_summary


def _event_key(event: dict[str, Any], start_dt: datetime, end_dt: datetime) -> str:
    event_id = _normalize_space(str(event.get("id") or ""))
    summary = _normalize_space(str(event.get("summary") or ""))
    base = event_id or summary or "event"
    return f"{base}|{start_dt.astimezone(timezone.utc).isoformat()}|{end_dt.astimezone(timezone.utc).isoformat()}"


def _truncate_text(text: str, width: int) -> str:
    raw = _normalize_space(text)
    if len(raw) <= width:
        return raw
    if width <= 3:
        return raw[:width]
    return raw[: width - 3] + "..."


def _build_member_block(member: str, rankings: list[tuple[str, int]]) -> str:
    header = f"Member: {member}"
    lines = [header, "#  Gifter                Diamonds      USD"]
    for idx, (gifter, diamonds) in enumerate(rankings, start=1):
        usd = diamonds * 0.005
        lines.append(
            f"{idx:<2} {_truncate_text(gifter, 20):<20} {diamonds:>10,}   ${usd:>8,.2f}"
        )
    return "```text\n" + "\n".join(lines) + "\n```"


def _chunk_messages(header: str, member_blocks: list[str], max_len: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = header.strip()
    for block in member_blocks:
        candidate = (current + "\n\n" + block).strip()
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks


@dataclass
class Config:
    calendar_id: str
    calendar_api_key: str
    event_name: str
    timezone_name: str
    poll_seconds: int
    state_file: Path
    webhook_url: str
    supabase_url: str
    supabase_key: str
    tiktok_username: str
    lookback_days: int
    send_empty: bool
    debug_enabled: bool


def _load_config() -> Config:
    supabase_url = _env("SUPABASE_URL", "").strip()
    project_id = _env("SUPABASE_PROJECT_ID", "").strip()
    if not supabase_url and project_id:
        supabase_url = f"https://{project_id}.supabase.co"
    return Config(
        calendar_id=_env("WILDCARDZ_CALENDAR_ID", "").strip(),
        calendar_api_key=_env("WILDCARDZ_CALENDAR_API_KEY", "").strip(),
        event_name=_normalize_space(_env("WILDCARDZ_TOPGIFTER_EVENT_NAME", _env("WILDCARDZ_REMINDER_EVENT_NAME", "Wildcardz Live")))
        or "Wildcardz Live",
        timezone_name=_env("WILDCARDZ_TOPGIFTER_TIMEZONE", _env("WILDCARDZ_REMINDER_TIMEZONE", "America/Denver")).strip() or "America/Denver",
        poll_seconds=max(15, _env_int("WILDCARDZ_TOPGIFTER_POLL_SECONDS", 60)),
        state_file=Path(
            _env("WILDCARDZ_TOPGIFTER_STATE_FILE", "/tmp/wildcardz_top_gifters_state.json").strip()
            or "/tmp/wildcardz_top_gifters_state.json"
        ),
        webhook_url=_env("WILDCARDZ_TOPGIFTER_WEBHOOK", "").strip(),
        supabase_url=supabase_url.rstrip("/"),
        supabase_key=_env("SUPABASE_SECRET_KEY", "").strip() or _env("SUPABASE_PUBLISHABLE_KEY", "").strip(),
        tiktok_username=_normalize_space(_env("WILDCARDZ_TOPGIFTER_TIKTOK_USERNAME", "wildcard_ns")).lstrip("@"),
        lookback_days=max(1, _env_int("WILDCARDZ_TOPGIFTER_LOOKBACK_DAYS", 2)),
        send_empty=_env_bool("WILDCARDZ_TOPGIFTER_SEND_EMPTY", True),
        debug_enabled=_env_bool("WILDCARDZ_TOPGIFTER_DEBUG", False),
    )


def _latest_completed_matching_event(
    *,
    events: list[dict[str, Any]],
    event_name: str,
    now_utc: datetime,
    target_local_date: date | None = None,
    local_tz: ZoneInfo | None = None,
) -> tuple[dict[str, Any], datetime, datetime] | None:
    latest: tuple[datetime, dict[str, Any], datetime, datetime] | None = None
    tz_for_filter = local_tz or timezone.utc
    for event in events:
        summary = _normalize_space(str(event.get("summary") or ""))
        if not _event_matches(summary, event_name):
            continue
        if _is_all_day_event(event):
            continue
        start_dt = _parse_event_datetime(event.get("start") if isinstance(event.get("start"), dict) else {})
        end_dt = _parse_event_datetime(event.get("end") if isinstance(event.get("end"), dict) else {})
        if start_dt is None or end_dt is None:
            continue
        if end_dt <= start_dt:
            continue
        if target_local_date is not None:
            if start_dt.astimezone(tz_for_filter).date() != target_local_date:
                continue
        end_utc = end_dt.astimezone(timezone.utc)
        if end_utc > now_utc:
            continue
        if latest is None or end_utc > latest[0]:
            latest = (end_utc, event, start_dt, end_dt)
    if latest is None:
        return None
    return latest[1], latest[2], latest[3]


async def _fetch_google_events(
    *,
    client: httpx.AsyncClient,
    calendar_id: str,
    api_key: str,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[dict[str, Any]]:
    encoded_calendar = quote(calendar_id, safe="")
    endpoint = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar}/events"
    params: dict[str, str] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": _format_google_dt(window_start_utc),
        "timeMax": _format_google_dt(window_end_utc),
    }
    if api_key:
        params["key"] = api_key
    response = await client.get(endpoint, params=params)
    if not response.is_success:
        raise RuntimeError(
            f"Google Calendar query failed status={response.status_code} body={(response.text or '').strip()[:600]}"
        )
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
    return out


async def _fetch_gift_rows_for_window(
    *,
    client: httpx.AsyncClient,
    tiktok_username: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    page_size = 1000
    max_pages = 250
    offset = 0
    pages = 0
    out: list[dict[str, Any]] = []
    while True:
        # Keep each filter as separate key in query string.
        # httpx supports repeated keys by list of tuples.
        query = [
            ("select", "id,iso_ts,to_member_nickname,from_username,amount_value,diamond_count,repeat_count,combo_count"),
            ("tiktok_username", f"eq.{tiktok_username}"),
            ("iso_ts", f"gte.{start_utc.astimezone(timezone.utc).isoformat()}"),
            ("iso_ts", f"lt.{end_utc.astimezone(timezone.utc).isoformat()}"),
            ("order", "id.asc"),
        ]
        headers = {
            "Range-Unit": "items",
            "Range": f"{offset}-{offset + page_size - 1}",
        }
        resp = await client.get("/gift_events", params=query, headers=headers)
        if not resp.is_success:
            raise RuntimeError(f"Supabase gift_events query failed status={resp.status_code} body={(resp.text or '').strip()[:800]}")
        rows = resp.json()
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected gift_events response type")
        out.extend([row for row in rows if isinstance(row, dict)])
        if len(rows) < page_size:
            break
        offset += page_size
        pages += 1
        if pages >= max_pages:
            raise RuntimeError("Exceeded gift_events pagination limit")
    return out


def _rank_top_gifters(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, int]]]:
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        member = _normalize_space(row.get("to_member_nickname") or "")
        gifter = _normalize_space(row.get("from_username") or "")
        if not member:
            continue
        if "the host" in _normalize_token(member):
            continue
        if not gifter:
            gifter = "(unknown)"
        diamonds = _gift_row_diamond_amount(row)
        if diamonds <= 0:
            continue
        member_bucket = totals.setdefault(member, {})
        member_bucket[gifter] = member_bucket.get(gifter, 0) + diamonds

    ranked: dict[str, list[tuple[str, int]]] = {}
    for member, donor_map in totals.items():
        top = sorted(donor_map.items(), key=lambda item: (-item[1], item[0].lower()))[:5]
        ranked[member] = top
    return ranked


async def _send_webhook_message(*, client: httpx.AsyncClient, webhook_url: str, content: str) -> None:
    payload = {"content": str(content or "")[:1900]}
    response = await client.post(webhook_url, json=payload)
    if not response.is_success:
        raise RuntimeError(f"Webhook failed status={response.status_code} body={(response.text or '').strip()[:500]}")


async def _send_report_for_event(
    *,
    supabase_client: httpx.AsyncClient,
    webhook_client: httpx.AsyncClient,
    cfg: Config,
    tz: ZoneInfo,
    event: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    logger: logging.Logger,
) -> None:
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    rows = await _fetch_gift_rows_for_window(
        client=supabase_client,
        tiktok_username=cfg.tiktok_username,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    ranked = _rank_top_gifters(rows)

    summary = _normalize_space(str(event.get("summary") or cfg.event_name)) or cfg.event_name
    start_local = start_utc.astimezone(tz)
    end_local = end_utc.astimezone(tz)
    header = (
        f"Wildcardz Top Gifters\n"
        f"Window: {start_local.strftime('%a %b %d')} {_format_clock(start_local)} - {_format_clock(end_local)} {tz.key}"
    )

    if not ranked:
        if not cfg.send_empty:
            logger.info("No gift rows for event '%s' (%s to %s); skipping empty report", summary, start_utc, end_utc)
            return
        await _send_webhook_message(
            client=webhook_client,
            webhook_url=cfg.webhook_url,
            content=header + "\n\nNo gift data found for this livestream window.",
        )
        logger.info("Sent empty top-gifters report for event '%s'", summary)
        return

    member_blocks: list[str] = []
    for member in sorted(ranked.keys(), key=lambda x: x.lower()):
        member_blocks.append(_build_member_block(member, ranked[member]))
    chunks = _chunk_messages(header=header, member_blocks=member_blocks, max_len=1900)
    for chunk in chunks:
        await _send_webhook_message(client=webhook_client, webhook_url=cfg.webhook_url, content=chunk)
    logger.info(
        "Sent top-gifters report for event '%s' with %s members (%s gift rows)",
        summary,
        len(ranked),
        len(rows),
    )


def _prune_state(state: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    sent = state.get("sent_events")
    if not isinstance(sent, dict):
        state["sent_events"] = {}
        return state
    keep: dict[str, Any] = {}
    cutoff = now_utc - timedelta(days=30)
    for key, meta in sent.items():
        if not isinstance(meta, dict):
            continue
        end_iso = _normalize_space(str(meta.get("event_end_utc") or ""))
        if not end_iso:
            keep[key] = meta
            continue
        try:
            parsed = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except Exception:
            keep[key] = meta
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            keep[key] = meta
    state["sent_events"] = keep
    return state


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("wildcardz-top-gifters")
    cfg = _load_config()

    if not cfg.calendar_id:
        raise SystemExit("Missing WILDCARDZ_CALENDAR_ID")
    if not cfg.calendar_api_key:
        raise SystemExit("Missing WILDCARDZ_CALENDAR_API_KEY")
    if not cfg.webhook_url:
        raise SystemExit("Missing WILDCARDZ_TOPGIFTER_WEBHOOK")
    if not cfg.supabase_url:
        raise SystemExit("Missing SUPABASE_URL (or SUPABASE_PROJECT_ID)")
    if not cfg.supabase_key:
        raise SystemExit("Missing SUPABASE_SECRET_KEY (or SUPABASE_PUBLISHABLE_KEY)")
    if not cfg.tiktok_username:
        raise SystemExit("Missing WILDCARDZ_TOPGIFTER_TIKTOK_USERNAME")

    try:
        tz = ZoneInfo(cfg.timezone_name)
    except Exception as exc:
        raise SystemExit(f"Invalid WILDCARDZ_TOPGIFTER_TIMEZONE '{cfg.timezone_name}': {exc}") from exc

    logger.info(
        "Starting top-gifters bot (event='%s' poll=%ss tz=%s daily_check=7:15PM local account=@%s debug=%s)",
        cfg.event_name,
        cfg.poll_seconds,
        cfg.timezone_name,
        cfg.tiktok_username,
        cfg.debug_enabled,
    )

    state = _load_state(cfg.state_file)
    state = _prune_state(state, datetime.now(timezone.utc))
    sent_events = state.setdefault("sent_events", {})
    if not isinstance(sent_events, dict):
        sent_events = {}
        state["sent_events"] = sent_events

    timeout = httpx.Timeout(connect=10.0, read=25.0, write=20.0, pool=10.0)
    supabase_headers = {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Accept": "application/json",
    }
    supabase_base = f"{cfg.supabase_url}/rest/v1"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as google_client, \
        httpx.AsyncClient(timeout=timeout, follow_redirects=True, base_url=supabase_base, headers=supabase_headers) as supabase_client, \
        httpx.AsyncClient(timeout=timeout, follow_redirects=True) as webhook_client:
        if cfg.debug_enabled:
            now_utc = datetime.now(timezone.utc)
            debug_window_start = now_utc - timedelta(days=cfg.lookback_days)
            debug_window_end = now_utc + timedelta(days=1)
            try:
                debug_events = await _fetch_google_events(
                    client=google_client,
                    calendar_id=cfg.calendar_id,
                    api_key=cfg.calendar_api_key,
                    window_start_utc=debug_window_start,
                    window_end_utc=debug_window_end,
                )
                latest = _latest_completed_matching_event(
                    events=debug_events,
                    event_name=cfg.event_name,
                    now_utc=now_utc,
                )
                if latest is None:
                    logger.warning(
                        "Debug mode enabled but no completed '%s' events found in last %s days",
                        cfg.event_name,
                        cfg.lookback_days,
                    )
                else:
                    debug_event, debug_start_dt, debug_end_dt = latest
                    debug_key = _event_key(debug_event, debug_start_dt, debug_end_dt)
                    logger.warning(
                        "Debug mode enabled: sending report now for latest live window "
                        "(start=%s end=%s summary='%s')",
                        debug_start_dt.astimezone(timezone.utc).isoformat(),
                        debug_end_dt.astimezone(timezone.utc).isoformat(),
                        _normalize_space(str(debug_event.get("summary") or cfg.event_name)),
                    )
                    await _send_report_for_event(
                        supabase_client=supabase_client,
                        webhook_client=webhook_client,
                        cfg=cfg,
                        tz=tz,
                        event=debug_event,
                        start_dt=debug_start_dt,
                        end_dt=debug_end_dt,
                        logger=logger,
                    )
                    sent_events[debug_key] = {
                        "sent_at_utc": datetime.now(timezone.utc).isoformat(),
                        "event_start_utc": debug_start_dt.astimezone(timezone.utc).isoformat(),
                        "event_end_utc": debug_end_dt.astimezone(timezone.utc).isoformat(),
                        "summary": _normalize_space(str(debug_event.get("summary") or "")),
                        "source": "debug",
                    }
                    state["sent_events"] = sent_events
                    _save_state(cfg.state_file, _prune_state(state, now_utc))
            except Exception as exc:
                logger.warning("Debug immediate run failed: %s", exc)

        while True:
            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone(tz)
            target_local_date = now_local.date()
            scheduled_local = datetime.combine(target_local_date, time(hour=19, minute=15), tzinfo=tz)
            last_daily_run_local_date = _normalize_space(str(state.get("last_daily_run_local_date") or ""))

            if now_local < scheduled_local:
                await asyncio.sleep(cfg.poll_seconds)
                continue
            if last_daily_run_local_date == target_local_date.isoformat():
                await asyncio.sleep(cfg.poll_seconds)
                continue

            day_start_local = datetime.combine(target_local_date, time(hour=0, minute=0), tzinfo=tz)
            next_day_local = day_start_local + timedelta(days=1)
            changed_state = False
            try:
                events = await _fetch_google_events(
                    client=google_client,
                    calendar_id=cfg.calendar_id,
                    api_key=cfg.calendar_api_key,
                    window_start_utc=day_start_local.astimezone(timezone.utc),
                    window_end_utc=next_day_local.astimezone(timezone.utc),
                )
            except Exception as exc:
                logger.warning("Calendar fetch failed for daily top-gifters check: %s", exc)
                await asyncio.sleep(cfg.poll_seconds)
                continue

            latest = _latest_completed_matching_event(
                events=events,
                event_name=cfg.event_name,
                now_utc=now_utc,
                target_local_date=target_local_date,
                local_tz=tz,
            )
            daily_check_completed = True
            if latest is not None:
                event, start_dt, end_dt = latest
                key = _event_key(event, start_dt, end_dt)
                if key not in sent_events:
                    try:
                        await _send_report_for_event(
                            supabase_client=supabase_client,
                            webhook_client=webhook_client,
                            cfg=cfg,
                            tz=tz,
                            event=event,
                            start_dt=start_dt,
                            end_dt=end_dt,
                            logger=logger,
                        )
                    except Exception as exc:
                        logger.warning("Failed to send daily top-gifters report for event key=%s: %s", key, exc)
                    else:
                        sent_events[key] = {
                            "sent_at_utc": datetime.now(timezone.utc).isoformat(),
                            "event_start_utc": start_dt.astimezone(timezone.utc).isoformat(),
                            "event_end_utc": end_dt.astimezone(timezone.utc).isoformat(),
                            "summary": _normalize_space(str(event.get("summary") or "")),
                            "source": "daily_1915_mt",
                        }
                        changed_state = True
                    if key not in sent_events:
                        daily_check_completed = False
            else:
                logger.info(
                    "Daily top-gifters check: no completed '%s' event found for %s",
                    cfg.event_name,
                    target_local_date.isoformat(),
                )

            if daily_check_completed:
                if last_daily_run_local_date != target_local_date.isoformat():
                    state["last_daily_run_local_date"] = target_local_date.isoformat()
                    changed_state = True
            else:
                logger.warning(
                    "Daily top-gifters check for %s not marked complete; will retry on next poll",
                    target_local_date.isoformat(),
                )
            if changed_state:
                state["sent_events"] = sent_events
                _save_state(cfg.state_file, _prune_state(state, now_utc))
            await asyncio.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    asyncio.run(run())
