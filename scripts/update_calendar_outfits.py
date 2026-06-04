from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from utils import _env


DEFAULT_TIMEZONE = "America/Denver"
DEFAULT_EVENT_NAME = "Wildcardz Live"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
JUNE_2026_OUTFITS = {
    2: "Racecar",
    3: "Streetwear",
    4: "Suits",
    5: "Batman & Robin",
    6: "Gabe's bday",
    9: "Biker",
    10: "Camo",
    11: "Jock",
    12: "Cherry Blossom",
    16: "Office Sirens",
    17: "Earthcore",
    18: "Denim",
    19: "Masquerade",
    23: "Floral",
    24: "Racecar",
    25: "Punk",
    26: "Kens",
    30: "Techwear",
}


@dataclass(frozen=True)
class ScriptConfig:
    calendar_id: str
    timezone_name: str
    event_name: str
    dry_run: bool
    year: int
    month: int


def _normalize_space(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _normalize_token(text: str) -> str:
    return _normalize_space(text).casefold()


def _load_google_service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SystemExit(
            "Missing Google Calendar writer dependencies. Install "
            "`google-api-python-client` and `google-auth` first."
        ) from exc

    raw_json = (
        _env("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        or _env("WILDCARDZ_GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    )
    service_account_file = (
        _env("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        or _env("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        or _env("WILDCARDZ_GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    )

    if raw_json:
        import json

        info = json.loads(raw_json)
        credentials = Credentials.from_service_account_info(info, scopes=[GOOGLE_CALENDAR_SCOPE])
    elif service_account_file:
        credentials = Credentials.from_service_account_file(service_account_file, scopes=[GOOGLE_CALENDAR_SCOPE])
    else:
        raise SystemExit(
            "Missing Google write credentials. app.env is loaded, but it only has "
            "WILDCARDZ_CALENDAR_ID/WILDCARDZ_CALENDAR_API_KEY right now. Add one of: "
            "GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_APPLICATION_CREDENTIALS, "
            "WILDCARDZ_GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SERVICE_ACCOUNT_JSON, or "
            "WILDCARDZ_GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _load_config() -> ScriptConfig:
    calendar_id = _env("WILDCARDZ_CALENDAR_ID", "").strip()
    if not calendar_id:
        raise SystemExit("Missing WILDCARDZ_CALENDAR_ID.")

    parser = argparse.ArgumentParser(
        description="Write outfit labels into Google Calendar event descriptions for the June 2026 outfit calendar."
    )
    parser.add_argument("--calendar-id", default=calendar_id, help="Google Calendar ID.")
    parser.add_argument("--timezone", default=_env("WILDCARDZ_REMINDER_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE)
    parser.add_argument(
        "--event-name",
        default=_env("WILDCARDZ_REMINDER_EVENT_NAME", DEFAULT_EVENT_NAME).strip() or DEFAULT_EVENT_NAME,
        help="Only update events whose summary matches this text (case-insensitive substring match).",
    )
    parser.add_argument("--year", type=int, default=2026, help="Calendar year to update.")
    parser.add_argument("--month", type=int, default=6, help="Calendar month to update.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned updates without writing to Google Calendar.")
    args = parser.parse_args()

    return ScriptConfig(
        calendar_id=args.calendar_id.strip(),
        timezone_name=args.timezone.strip() or DEFAULT_TIMEZONE,
        event_name=_normalize_space(args.event_name) or DEFAULT_EVENT_NAME,
        dry_run=bool(args.dry_run),
        year=int(args.year),
        month=int(args.month),
    )


def _month_outfits(year: int, month: int) -> dict[date, str]:
    if year == 2026 and month == 6:
        return {date(year, month, day): outfit for day, outfit in JUNE_2026_OUTFITS.items()}
    raise SystemExit(f"No built-in outfit transcription is available for {year:04d}-{month:02d}.")


def _month_bounds(year: int, month: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)
    return start, end


def _list_matching_events(
    *,
    service: Any,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
    event_name: str,
) -> list[dict[str, Any]]:
    page_token = None
    matches: list[dict[str, Any]] = []
    event_name_key = _normalize_token(event_name)
    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("items", []):
            if not isinstance(item, dict):
                continue
            summary = _normalize_space(str(item.get("summary") or ""))
            if event_name_key and event_name_key not in _normalize_token(summary):
                continue
            matches.append(item)
        page_token = response.get("nextPageToken")
        if not page_token:
            return matches


def _event_local_date(event: dict[str, Any], tz: ZoneInfo) -> date | None:
    start = event.get("start")
    if not isinstance(start, dict):
        return None
    raw_date = _normalize_space(str(start.get("date") or ""))
    if raw_date:
        return date.fromisoformat(raw_date)
    raw_dt = _normalize_space(str(start.get("dateTime") or ""))
    if not raw_dt:
        return None
    if raw_dt.endswith("Z"):
        raw_dt = raw_dt[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw_dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz).date()


def _is_section_header(line: str) -> bool:
    head = _normalize_space(line)
    if not head:
        return False
    if ":" not in head:
        return False
    label = head.split(":", 1)[0]
    return label.replace(" ", "").isalpha()


def _header_label(line: str) -> str:
    head = _normalize_space(line)
    if ":" not in head:
        return ""
    return _normalize_token(head.split(":", 1)[0])


def _upsert_outfit_description(description: str, outfit: str) -> str:
    lines = str(description or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    in_outfit_block = False

    for raw_line in lines:
        normalized = _normalize_space(raw_line)
        if _is_section_header(normalized):
            label = _header_label(normalized)
            if label in {"outfit", "outfits"}:
                in_outfit_block = True
                continue
            in_outfit_block = False
        if in_outfit_block:
            continue
        kept.append(raw_line.rstrip())

    while kept and not kept[-1].strip():
        kept.pop()
    if kept:
        kept.append("")
    kept.append(f"Outfit: {outfit}")
    return "\n".join(kept).strip() + "\n"


def _format_event_debug(event: dict[str, Any], tz: ZoneInfo) -> str:
    local_day = _event_local_date(event, tz)
    summary = _normalize_space(str(event.get("summary") or "Untitled event"))
    return f"{local_day.isoformat() if local_day else 'unknown-date'} | {summary}"


def _update_events(
    *,
    service: Any,
    cfg: ScriptConfig,
    tz: ZoneInfo,
    outfits_by_day: dict[date, str],
) -> tuple[int, int, list[dict[str, Any]]]:
    month_start, month_end = _month_bounds(cfg.year, cfg.month, tz)
    events = _list_matching_events(
        service=service,
        calendar_id=cfg.calendar_id,
        time_min=month_start,
        time_max=month_end,
        event_name=cfg.event_name,
    )

    matched = 0
    updated = 0
    for event in events:
        day = _event_local_date(event, tz)
        if day is None or day not in outfits_by_day:
            continue
        matched += 1
        outfit = outfits_by_day[day]
        existing_description = str(event.get("description") or "")
        new_description = _upsert_outfit_description(existing_description, outfit)
        if new_description == existing_description:
            print(f"Unchanged: {_format_event_debug(event, tz)} -> {outfit}")
            continue
        print(f"{'Would update' if cfg.dry_run else 'Updating'}: {_format_event_debug(event, tz)} -> {outfit}")
        if not cfg.dry_run:
            (
                service.events()
                .patch(
                    calendarId=cfg.calendar_id,
                    eventId=str(event["id"]),
                    body={"description": new_description},
                )
                .execute()
            )
        updated += 1
    return matched, updated, events


def main() -> int:
    cfg = _load_config()
    tz = ZoneInfo(cfg.timezone_name)
    outfits_by_day = _month_outfits(cfg.year, cfg.month)
    service = _load_google_service()
    matched, updated, events = _update_events(service=service, cfg=cfg, tz=tz, outfits_by_day=outfits_by_day)

    event_days = {_event_local_date(event, tz) for event in events}
    missing_days = [day for day in sorted(outfits_by_day) if day not in event_days]
    if missing_days:
        print("No matching event found for:", ", ".join(day.isoformat() for day in missing_days))

    print(
        f"{'Dry run complete' if cfg.dry_run else 'Update complete'}: "
        f"{matched} matching event(s), {updated} description change(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
