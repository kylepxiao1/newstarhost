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

NAME_MENTION_MAP = {
    "kyle": "@sztakin",
    "cardin": "@teru_shima_",
    "morgan": "@mysticgod",
    "gabe": "@kinginthemaking.",
    "ky": "@codename.ky",
    "kai": "@codename.ky",
    "mina": "@darkminathegone",
    "mk": "@darkminathegone",
    "ben": "@iambenxx",
    "jesus": "@iambenxx",
    "yeonnie": "@yeonniebaby",
    "angel": "@star_berry",
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


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_token(text: str) -> str:
    return _normalize_space(text).lower()


def _normalize_label(text: str) -> str:
    return re.sub(r"[^a-z]", "", _normalize_space(text).lower())


def _normalize_name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_space(text).lower())


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


async def _fetch_google_events(
    *,
    client: httpx.AsyncClient,
    calendar_id: str,
    api_key: str,
    day_start_local: datetime,
    day_end_local: datetime,
) -> list[dict[str, Any]]:
    encoded_calendar = quote(calendar_id, safe="")
    endpoint = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar}/events"
    params: dict[str, str] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": _format_google_dt(day_start_local),
        "timeMax": _format_google_dt(day_end_local),
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


async def _send_webhook_message(*, client: httpx.AsyncClient, webhook_url: str, content: str) -> None:
    payload = {"content": str(content or "")[:1900]}
    response = await client.post(webhook_url, json=payload)
    if not response.is_success:
        raise RuntimeError(f"Webhook failed status={response.status_code} body={(response.text or '').strip()[:500]}")


@dataclass
class Config:
    calendar_id: str
    calendar_api_key: str
    webhook_url: str
    event_name: str
    timezone_name: str
    run_hour: int
    run_minute: int
    poll_seconds: int
    state_file: Path
    send_no_event_message: bool
    test_fire_now: bool


def _load_config() -> Config:
    return Config(
        calendar_id=_env("WILDCARDZ_CALENDAR_ID", "").strip(),
        calendar_api_key=_env("WILDCARDZ_CALENDAR_API_KEY", "").strip(),
        webhook_url=_env("WILDCARDZ_REMINDER_WEBHOOK", DEFAULT_WEBHOOK_URL).strip(),
        event_name=_normalize_space(_env("WILDCARDZ_REMINDER_EVENT_NAME", "Wildcardz Live")) or "Wildcardz Live",
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
    events = await _fetch_google_events(
        client=client,
        calendar_id=cfg.calendar_id,
        api_key=cfg.calendar_api_key,
        day_start_local=day_start_local,
        day_end_local=day_end_local,
    )
    matching: list[dict[str, Any]] = []
    for event in events:
        summary = _normalize_space(str(event.get("summary") or ""))
        if _event_matches(summary, cfg.event_name):
            matching.append(event)
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
    if not cfg.calendar_api_key:
        raise SystemExit("Missing WILDCARDZ_CALENDAR_API_KEY")
    if not cfg.webhook_url:
        raise SystemExit("Missing WILDCARDZ_REMINDER_WEBHOOK")

    try:
        tz = ZoneInfo(cfg.timezone_name)
    except Exception as exc:
        raise SystemExit(f"Invalid WILDCARDZ_REMINDER_TIMEZONE '{cfg.timezone_name}': {exc}") from exc

    state = _load_state(cfg.state_file)
    last_sent_day = _normalize_space(str(state.get("last_sent_day") or ""))
    logger.info(
        "Starting Wildcardz reminder (tz=%s run_at=%02d:%02d poll=%ss event='%s' test_fire_now=%s)",
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
                logger.warning("Immediate test reminder failed for %s: %s", today_key, exc)
            else:
                if sent:
                    last_sent_day = today_key
                    _save_state(
                        cfg.state_file,
                        {"last_sent_day": last_sent_day, "updated_at": datetime.now(timezone.utc).isoformat()},
                    )

        while True:
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
                logger.warning("Reminder run failed for %s: %s", today_key, exc)
                await asyncio.sleep(cfg.poll_seconds)
                continue

            if sent:
                last_sent_day = today_key
                _save_state(cfg.state_file, {"last_sent_day": last_sent_day, "updated_at": datetime.now(timezone.utc).isoformat()})
            await asyncio.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
