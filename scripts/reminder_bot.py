from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from utils import _env


DEFAULT_WEBHOOK_URL = ""
DISCORD_HOSTS_ROLE_ID = "1471258157459443785"
DISCORD_USER_ID_MAP = {
    "yeonnie": "324352448640712714",
    "angel": "260206573849280512",
    "ben": "1098834639285661840",
    "kyle": "763167828689289226",
    "morgan": "386020846835073024",
    "gabe": "976319207312621578",
    "cardin": "506958059462197251",
    "ky": "759636349661347910",
    "mina": "179870678567092224",
}


def _discord_user_mention(user_id: str) -> str:
    return f"<@{str(user_id).strip()}>"


def _discord_role_mention(role_id: str) -> str:
    return f"<@&{str(role_id).strip()}>"


NOTES_REMINDER_MESSAGE = (
    f"{_discord_role_mention(DISCORD_HOSTS_ROLE_ID)} don't forget to add a message note for today's livestream\n"
    "https://newstarhost.fly.dev/app#notes"
)
NOTES_FOLLOWUP_DELAY = timedelta(hours=1)
NOTES_FOLLOWUP_PRUNE_DAYS = 30

NAME_MENTION_MAP = {
    "yeonnie": _discord_user_mention(DISCORD_USER_ID_MAP["yeonnie"]),
    "angel": _discord_user_mention(DISCORD_USER_ID_MAP["angel"]),
    "ben": _discord_user_mention(DISCORD_USER_ID_MAP["ben"]),
    "jesus": _discord_user_mention(DISCORD_USER_ID_MAP["ben"]),
    "kyle": _discord_user_mention(DISCORD_USER_ID_MAP["kyle"]),
    "morgan": _discord_user_mention(DISCORD_USER_ID_MAP["morgan"]),
    "gabe": _discord_user_mention(DISCORD_USER_ID_MAP["gabe"]),
    "cardin": _discord_user_mention(DISCORD_USER_ID_MAP["cardin"]),
    "ky": _discord_user_mention(DISCORD_USER_ID_MAP["ky"]),
    "kai": _discord_user_mention(DISCORD_USER_ID_MAP["ky"]),
    "mina": _discord_user_mention(DISCORD_USER_ID_MAP["mina"]),
    "mk": _discord_user_mention(DISCORD_USER_ID_MAP["mina"]),
}


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


def _exc_text(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_token(text: str) -> str:
    return _normalize_space(text).lower()


def _resolve_supabase_url() -> str:
    explicit_url = _env("SUPABASE_URL", "").strip()
    if explicit_url:
        return explicit_url.rstrip("/")
    project_id = _env("SUPABASE_PROJECT_ID", "").strip()
    if project_id:
        return f"https://{project_id}.supabase.co"
    return ""


def _normalize_label(text: str) -> str:
    return re.sub(r"[^a-z]", "", _normalize_space(text).lower())


def _normalize_name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_space(text).lower())


_lark_token_cache: tuple[str, float] | None = None


async def _get_lark_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> str:
    import time as _time
    global _lark_token_cache
    now = _time.monotonic()
    if _lark_token_cache is not None:
        token, expires_at = _lark_token_cache
        if now + 60 < expires_at:
            return token
    response = await client.post(
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    if not response.is_success:
        raise RuntimeError(f"Lark auth failed: {response.status_code} {(response.text or '').strip()[:300]}")
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark auth error: {data.get('msg', '')} (code={data.get('code')})")
    token = str(data["tenant_access_token"])
    expire = int(data.get("expire", 7200))
    _lark_token_cache = (token, now + expire)
    return token


def _normalize_lark_event(event: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "summary": event.get("summary", ""),
        "description": event.get("description", ""),
    }
    for lark_key, gc_key in (("start_time", "start"), ("end_time", "end")):
        time_obj = event.get(lark_key)
        if not isinstance(time_obj, dict):
            continue
        ts = time_obj.get("timestamp")
        tz_name = str(time_obj.get("timezone") or "UTC")
        date_str = time_obj.get("date")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                out[gc_key] = {"dateTime": dt.isoformat(), "timeZone": tz_name}
            except (ValueError, TypeError):
                pass
        elif date_str:
            out[gc_key] = {"date": str(date_str), "timeZone": tz_name}
    return out


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


def _event_time_text(event: dict[str, Any], tz: ZoneInfo) -> str:
    if _is_all_day_event(event):
        return "All day (MT)"
    start_dt = _parse_event_datetime(event.get("start") if isinstance(event.get("start"), dict) else {})
    end_dt = _parse_event_datetime(event.get("end") if isinstance(event.get("end"), dict) else {})
    if start_dt is None:
        return "Time not listed"
    start_local = start_dt.astimezone(tz)
    if end_dt is None:
        return f"{_format_clock(start_local)} MT"
    end_local = end_dt.astimezone(tz)
    if end_local <= start_local:
        return f"{_format_clock(start_local)} MT"
    return f"{_format_clock(start_local)} - {_format_clock(end_local)} MT"


def _add_unique(items: list[str], value: str) -> None:
    cleaned = _normalize_space(re.sub(r"^[\-\*\u2022]+\s*", "", str(value or ""))).strip(" ,;")
    if not cleaned:
        return
    key = _normalize_token(cleaned)
    if not key:
        return
    for existing in items:
        if _normalize_token(existing) == key:
            return
    items.append(cleaned)


def _split_people(raw: str) -> list[str]:
    text = _normalize_space(str(raw or ""))
    if not text:
        return []
    parts = re.split(r"\s*(?:,|;|/|\\|\||&|\band\b|\+)\s*", text, flags=re.IGNORECASE)
    out: list[str] = []
    for part in parts:
        cleaned = _normalize_space(part).strip(" ,;")
        if not cleaned:
            continue
        out.append(cleaned)
    return out


def _split_outfit(raw: str) -> list[str]:
    text = _normalize_space(str(raw or ""))
    if not text:
        return []
    parts = re.split(r"\s*(?:,|;)\s*", text)
    out: list[str] = []
    for part in parts:
        cleaned = _normalize_space(part).strip(" ,;")
        if not cleaned:
            continue
        out.append(cleaned)
    return out or [text]


def _escape_discord_markdown(text: str) -> str:
    value = str(text or "")
    return re.sub(r"([\\*_`~|>])", r"\\\1", value)


def _map_name_to_mention(name: str) -> str:
    text = _normalize_space(str(name or ""))
    if not text:
        return text
    direct_key = _normalize_name_key(text)
    if direct_key in NAME_MENTION_MAP:
        return NAME_MENTION_MAP[direct_key]
    for token in re.split(r"[^a-z0-9]+", text.lower()):
        token_key = _normalize_name_key(token)
        if token_key and token_key in NAME_MENTION_MAP:
            return NAME_MENTION_MAP[token_key]
    return text


def _map_people_list(values: list[str]) -> list[str]:
    mapped: list[str] = []
    for value in values:
        resolved = _map_name_to_mention(value)
        if resolved.startswith("@"):
            resolved = _escape_discord_markdown(resolved)
        _add_unique(mapped, resolved)
    return mapped


def _parse_event_description(description: str) -> dict[str, list[str]]:
    labels = {
        "dancer": "dancers",
        "dancers": "dancers",
        "host": "hosts",
        "hosts": "hosts",
        "outfit": "outfit",
        "outfits": "outfit",
    }
    sections: dict[str, list[str]] = {"dancers": [], "hosts": [], "outfit": []}
    active_section = ""
    for raw_line in str(description or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _normalize_space(raw_line)
        if not line:
            continue
        line_no_bullet = _normalize_space(re.sub(r"^[\-\*\u2022]+\s*", "", line))
        header_match = re.match(r"^([A-Za-z][A-Za-z\s]+)\s*:\s*(.*)$", line_no_bullet)
        if header_match:
            label = labels.get(_normalize_label(header_match.group(1)), "")
            active_section = label
            trailing = _normalize_space(header_match.group(2))
            if label and trailing:
                if label in {"dancers", "hosts"}:
                    for name in _split_people(trailing):
                        _add_unique(sections[label], name)
                else:
                    for outfit_value in _split_outfit(trailing):
                        _add_unique(sections[label], outfit_value)
            continue
        if not active_section:
            continue
        if active_section in {"dancers", "hosts"}:
            for name in _split_people(line_no_bullet):
                _add_unique(sections[active_section], name)
        else:
            for outfit_value in _split_outfit(line_no_bullet):
                _add_unique(sections[active_section], outfit_value)
    return sections


def _build_message_for_events(
    *,
    target_day: date,
    events: list[dict[str, Any]],
    tz: ZoneInfo,
) -> str:
    header = f"Wildcardz Live reminder for {target_day.strftime('%A, %B %d, %Y')} (MT)"
    lines = [header, ""]
    for index, event in enumerate(events):
        summary = _normalize_space(str(event.get("summary") or "Wildcardz Live"))
        sections = _parse_event_description(str(event.get("description") or ""))
        dancers_list = _map_people_list(sections["dancers"])
        hosts_list = _map_people_list(sections["hosts"])
        dancers = ", ".join(dancers_list) if dancers_list else "Not listed"
        hosts = ", ".join(hosts_list) if hosts_list else "Not listed"
        outfit = ", ".join(sections["outfit"]) if sections["outfit"] else "Not listed"
        lines.append(summary)
        lines.append(f"Time: {_event_time_text(event, tz)}")
        lines.append(f"Dancers: {dancers}")
        lines.append(f"Hosts: {hosts}")
        lines.append(f"Outfit: {outfit}")
        if index != len(events) - 1:
            lines.append("")
    content = "\n".join(lines).strip()
    if len(content) <= 1900:
        return content
    return content[:1875].rstrip() + "\n...(truncated)"


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
        logging.warning("Failed to write reminder state file: %s", path)


async def _fetch_lark_events(
    *,
    client: httpx.AsyncClient,
    calendar_id: str,
    app_id: str,
    app_secret: str,
    day_start_local: datetime,
    day_end_local: datetime,
) -> list[dict[str, Any]]:
    token = await _get_lark_token(client=client, app_id=app_id, app_secret=app_secret)
    start_ts = str(int(day_start_local.astimezone(timezone.utc).timestamp()))
    end_ts = str(int(day_end_local.astimezone(timezone.utc).timestamp()))
    encoded_calendar = quote(calendar_id, safe="")
    endpoint = f"https://open.larksuite.com/open-apis/calendar/v4/calendars/{encoded_calendar}/events"
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, str] = {"start_time": start_ts, "end_time": end_ts, "page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        response = await client.get(endpoint, params=params, headers=headers)
        if not response.is_success:
            raise RuntimeError(
                f"Lark Calendar query failed status={response.status_code} body={(response.text or '').strip()[:600]}"
            )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"Lark Calendar error: {payload.get('msg', '')} (code={payload.get('code')})")
        data = payload.get("data") or {}
        for item in data.get("items") or []:
            if isinstance(item, dict):
                out.append(_normalize_lark_event(item))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or None
        if not page_token:
            break
    return out


async def _send_webhook_message(*, client: httpx.AsyncClient, webhook_url: str, content: str) -> None:
    payload = {"content": str(content or "")[:1900]}
    response = await client.post(webhook_url, json=payload)
    if not response.is_success:
        raise RuntimeError(f"Webhook failed status={response.status_code} body={(response.text or '').strip()[:500]}")


@dataclass
class Config:
    calendar_id: str
    lark_app_id: str
    lark_app_secret: str
    webhook_url: str
    event_name: str
    supabase_url: str
    supabase_key: str
    timezone_name: str
    run_hour: int
    run_minute: int
    poll_seconds: int
    state_file: Path
    send_no_event_message: bool
    test_fire_now: bool


def _load_config() -> Config:
    supabase_url = _resolve_supabase_url()
    return Config(
        calendar_id=_env("WILDCARDZ_CALENDAR_ID", "").strip(),
        lark_app_id=_env("WILDCARDZ_LARK_APP_ID", "").strip(),
        lark_app_secret=_env("WILDCARDZ_LARK_APP_SECRET", "").strip(),
        webhook_url=_env("WILDCARDZ_REMINDER_WEBHOOK", DEFAULT_WEBHOOK_URL).strip(),
        event_name=_normalize_space(_env("WILDCARDZ_REMINDER_EVENT_NAME", "Wildcardz Live")) or "Wildcardz Live",
        supabase_url=supabase_url,
        supabase_key=_env("SUPABASE_SECRET_KEY", "").strip() or _env("SUPABASE_PUBLISHABLE_KEY", "").strip(),
        timezone_name=_env("WILDCARDZ_REMINDER_TIMEZONE", "America/Denver").strip() or "America/Denver",
        run_hour=max(0, min(23, _env_int("WILDCARDZ_REMINDER_HOUR", 12))),
        run_minute=max(0, min(59, _env_int("WILDCARDZ_REMINDER_MINUTE", 0))),
        poll_seconds=max(15, _env_int("WILDCARDZ_REMINDER_POLL_SECONDS", 30)),
        state_file=Path(
            _env("WILDCARDZ_REMINDER_STATE_FILE", "/tmp/wildcardz_live_reminder_state.json").strip()
            or "/tmp/wildcardz_live_reminder_state.json"
        ),
        send_no_event_message=_env_bool("WILDCARDZ_REMINDER_SEND_IF_NONE", False),
        test_fire_now=_env_bool("WILDCARDZ_REMINDER_TEST_FIRE_NOW", False),
    )


def _event_matches(summary: str, target_name: str) -> bool:
    normalized_summary = _normalize_token(summary)
    normalized_target = _normalize_token(target_name)
    if not normalized_summary or not normalized_target:
        return False
    return normalized_target in normalized_summary


def _matching_live_events(
    *,
    events: list[dict[str, Any]],
    event_name: str,
) -> list[tuple[str, datetime, datetime, str]]:
    out: list[tuple[str, datetime, datetime, str]] = []
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
        event_id = _normalize_space(str(event.get("id") or ""))
        key_base = event_id or summary or "event"
        key = f"{key_base}|{start_dt.astimezone(timezone.utc).isoformat()}|{end_dt.astimezone(timezone.utc).isoformat()}"
        out.append((key, start_dt, end_dt, summary or "Wildcardz Live"))
    return out


def _prune_notes_followup_state(state: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    raw = state.get("notes_followups")
    if not isinstance(raw, dict):
        state["notes_followups"] = {}
        return state
    keep: dict[str, Any] = {}
    cutoff = now_utc - timedelta(days=NOTES_FOLLOWUP_PRUNE_DAYS)
    for key, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        due_iso = _normalize_space(str(meta.get("due_utc") or ""))
        if not due_iso:
            keep[key] = meta
            continue
        try:
            due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        except Exception:
            keep[key] = meta
            continue
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=timezone.utc)
        if due_dt >= cutoff:
            keep[key] = meta
    state["notes_followups"] = keep
    return state


async def _stream_note_exists_for_day(
    *,
    client: httpx.AsyncClient,
    supabase_url: str,
    supabase_key: str,
    stream_day_iso: str,
) -> bool:
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/stream_notes"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }
    params = {
        "select": "id",
        "stream_date": f"eq.{stream_day_iso}",
        "order": "id.desc",
        "limit": "1",
    }
    response = await client.get(endpoint, params=params, headers=headers)
    if not response.is_success:
        raise RuntimeError(
            f"Supabase stream_notes query failed status={response.status_code} body={(response.text or '').strip()[:600]}"
        )
    payload = response.json()
    return isinstance(payload, list) and len(payload) > 0


async def _run_notes_followups(
    *,
    client: httpx.AsyncClient,
    cfg: Config,
    tz: ZoneInfo,
    logger: logging.Logger,
    state: dict[str, Any],
) -> bool:
    now_utc = datetime.now(timezone.utc)
    state = _prune_notes_followup_state(state, now_utc)
    followups = state.setdefault("notes_followups", {})
    if not isinstance(followups, dict):
        followups = {}
        state["notes_followups"] = followups

    day_local = now_utc.astimezone(tz).date()
    day_start_local = datetime.combine(day_local, time(hour=0, minute=0), tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    events = await _fetch_lark_events(
        client=client,
        calendar_id=cfg.calendar_id,
        app_id=cfg.lark_app_id,
        app_secret=cfg.lark_app_secret,
        day_start_local=day_start_local,
        day_end_local=day_end_local,
    )
    matching = _matching_live_events(events=events, event_name=cfg.event_name)

    changed_state = False
    reminder_day_key = _normalize_space(str(state.get("notes_reminder_sent_day") or ""))
    for key, start_dt, end_dt, summary in matching:
        due_utc = end_dt.astimezone(timezone.utc) + NOTES_FOLLOWUP_DELAY
        meta = followups.get(key)
        if not isinstance(meta, dict):
            meta = {}
            followups[key] = meta
            changed_state = True
        if _normalize_space(str(meta.get("due_utc") or "")) != due_utc.isoformat():
            meta["due_utc"] = due_utc.isoformat()
            meta["event_start_utc"] = start_dt.astimezone(timezone.utc).isoformat()
            meta["event_end_utc"] = end_dt.astimezone(timezone.utc).isoformat()
            meta["summary"] = summary
            changed_state = True
        checked_at = _normalize_space(str(meta.get("checked_at_utc") or ""))
        if checked_at:
            continue
        if now_utc < due_utc:
            continue

        stream_day_iso = now_utc.astimezone(tz).date().isoformat()
        has_note = await _stream_note_exists_for_day(
            client=client,
            supabase_url=cfg.supabase_url,
            supabase_key=cfg.supabase_key,
            stream_day_iso=stream_day_iso,
        )
        meta["checked_at_utc"] = now_utc.isoformat()
        meta["checked_stream_date"] = stream_day_iso
        meta["has_stream_note"] = bool(has_note)
        changed_state = True

        if has_note:
            logger.info(
                "Notes follow-up check passed for event '%s': stream_notes has stream_date=%s",
                summary,
                stream_day_iso,
            )
            continue

        if reminder_day_key == stream_day_iso:
            logger.info(
                "Missing stream_notes entry for %s after '%s', but reminder already sent for this day",
                stream_day_iso,
                summary,
            )
            continue
        await _send_webhook_message(client=client, webhook_url=cfg.webhook_url, content=NOTES_REMINDER_MESSAGE)
        reminder_day_key = stream_day_iso
        state["notes_reminder_sent_day"] = reminder_day_key
        meta["reminder_sent_utc"] = now_utc.isoformat()
        changed_state = True
        logger.info(
            "Sent notes reminder for missing stream_notes entry stream_date=%s (event='%s')",
            stream_day_iso,
            summary,
        )

    return changed_state


async def _run_for_day(
    *,
    client: httpx.AsyncClient,
    cfg: Config,
    tz: ZoneInfo,
    target_day: date,
    logger: logging.Logger,
) -> bool:
    day_start_local = datetime.combine(target_day, time(hour=0, minute=0), tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    events = await _fetch_lark_events(
        client=client,
        calendar_id=cfg.calendar_id,
        app_id=cfg.lark_app_id,
        app_secret=cfg.lark_app_secret,
        day_start_local=day_start_local,
        day_end_local=day_end_local,
    )
    matching: list[dict[str, Any]] = []
    past_or_started_count = 0
    for event in events:
        summary = _normalize_space(str(event.get("summary") or ""))
        if _event_matches(summary, cfg.event_name):
            start_dt = _parse_event_datetime(event.get("start") if isinstance(event.get("start"), dict) else {})
            if start_dt is not None and now_local >= start_dt.astimezone(tz):
                past_or_started_count += 1
                continue
            matching.append(event)

    if past_or_started_count and not matching:
        logger.info(
            "Skipping Wildcardz reminder for %s because matching event(s) have already started or ended (now=%s MT, skipped=%s)",
            target_day.isoformat(),
            now_local.strftime("%Y-%m-%d %H:%M"),
            past_or_started_count,
        )
        return True

    if not matching:
        logger.info("No '%s' events found for %s", cfg.event_name, target_day.isoformat())
        if cfg.send_no_event_message:
            content = (
                f"Wildcardz Live reminder for {target_day.strftime('%A, %B %d, %Y')} (MT)\n"
                f"No '{cfg.event_name}' events were found on the calendar for today."
            )
            await _send_webhook_message(client=client, webhook_url=cfg.webhook_url, content=content)
        return True
    content = _build_message_for_events(target_day=target_day, events=matching, tz=tz)
    await _send_webhook_message(client=client, webhook_url=cfg.webhook_url, content=content)
    logger.info("Sent Wildcardz reminder for %s with %s matching event(s)", target_day.isoformat(), len(matching))
    return True


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("wildcardz-live-reminder")
    cfg = _load_config()

    if not cfg.calendar_id:
        raise SystemExit("Missing WILDCARDZ_CALENDAR_ID")
    if not cfg.lark_app_id:
        raise SystemExit("Missing WILDCARDZ_LARK_APP_ID")
    if not cfg.lark_app_secret:
        raise SystemExit("Missing WILDCARDZ_LARK_APP_SECRET")
    if not cfg.webhook_url:
        raise SystemExit("Missing WILDCARDZ_REMINDER_WEBHOOK")
    if not cfg.supabase_url:
        raise SystemExit("Missing SUPABASE_URL (or SUPABASE_PROJECT_ID)")
    if not cfg.supabase_key:
        raise SystemExit("Missing SUPABASE_SECRET_KEY (or SUPABASE_PUBLISHABLE_KEY)")

    try:
        tz = ZoneInfo(cfg.timezone_name)
    except Exception as exc:
        raise SystemExit(f"Invalid WILDCARDZ_REMINDER_TIMEZONE '{cfg.timezone_name}': {exc}") from exc

    state = _load_state(cfg.state_file)
    state = _prune_notes_followup_state(state, datetime.now(timezone.utc))
    last_sent_day = _normalize_space(str(state.get("last_sent_day") or ""))
    logger.info(
        "Starting Wildcardz reminder (tz=%s run_at=%02d:%02d poll=%ss event='%s' test_fire_now=%s notes_followup=+1h)",
        cfg.timezone_name,
        cfg.run_hour,
        cfg.run_minute,
        cfg.poll_seconds,
        cfg.event_name,
        cfg.test_fire_now,
    )

    timeout = httpx.Timeout(connect=10.0, read=25.0, write=20.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if cfg.test_fire_now:
            now_local = datetime.now(timezone.utc).astimezone(tz)
            today = now_local.date()
            today_key = today.isoformat()
            logger.info("WILDCARDZ_REMINDER_TEST_FIRE_NOW enabled; sending immediate reminder for %s", today_key)
            try:
                sent = await _run_for_day(client=client, cfg=cfg, tz=tz, target_day=today, logger=logger)
            except Exception as exc:
                logger.warning("Immediate test reminder failed for %s: %s", today_key, _exc_text(exc))
            else:
                if sent:
                    last_sent_day = today_key
                    state["last_sent_day"] = last_sent_day
                    state["updated_at"] = datetime.now(timezone.utc).isoformat()
                    _save_state(cfg.state_file, state)

        while True:
            try:
                followup_changed = await _run_notes_followups(
                    client=client,
                    cfg=cfg,
                    tz=tz,
                    logger=logger,
                    state=state,
                )
            except Exception as exc:
                logger.warning("Notes follow-up check failed: %s", _exc_text(exc))
            else:
                if followup_changed:
                    _save_state(cfg.state_file, state)

            now_local = datetime.now(timezone.utc).astimezone(tz)
            today = now_local.date()
            today_key = today.isoformat()
            run_at = datetime.combine(today, time(hour=cfg.run_hour, minute=cfg.run_minute), tzinfo=tz)

            if now_local < run_at:
                sleep_seconds = min(cfg.poll_seconds, max(1, int((run_at - now_local).total_seconds())))
                await asyncio.sleep(sleep_seconds)
                continue

            if last_sent_day == today_key:
                await asyncio.sleep(cfg.poll_seconds)
                continue

            try:
                sent = await _run_for_day(client=client, cfg=cfg, tz=tz, target_day=today, logger=logger)
            except Exception as exc:
                logger.warning("Reminder run failed for %s: %s", today_key, _exc_text(exc))
                await asyncio.sleep(cfg.poll_seconds)
                continue

            if sent:
                last_sent_day = today_key
                state["last_sent_day"] = last_sent_day
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                _save_state(cfg.state_file, state)
            await asyncio.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
