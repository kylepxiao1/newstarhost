from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

try:
    from scripts.utils import _env
except ImportError:
    from utils import _env


GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_SHEET_TAB = "Sheet1"
HEADER_ROW = ["Song", "Artist", "Want to learn", "Has learned", "Difficulty", "Duo?", "Tags"]

REPO_ROOT = Path(__file__).resolve().parent.parent
SONGS_PATH = REPO_ROOT / "media" / "songs.json"
EXAMPLE_SHEET_PATH = REPO_ROOT / "sheet" / "Sheet1.html"


class SongSheetError(RuntimeError):
    pass


def _quote_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _load_google_sheets_service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SongSheetError(
            "Missing Google Sheets dependencies. Install `google-api-python-client` and `google-auth` first."
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
        info = json.loads(raw_json)
        credentials = Credentials.from_service_account_info(info, scopes=[GOOGLE_SHEETS_SCOPE])
    elif service_account_file:
        credentials = Credentials.from_service_account_file(service_account_file, scopes=[GOOGLE_SHEETS_SCOPE])
    else:
        raise SongSheetError(
            "Missing Google write credentials. Set one of: GOOGLE_SERVICE_ACCOUNT_FILE, "
            "GOOGLE_APPLICATION_CREDENTIALS, WILDCARDZ_GOOGLE_SERVICE_ACCOUNT_FILE, "
            "GOOGLE_SERVICE_ACCOUNT_JSON, or WILDCARDZ_GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _load_songs() -> dict[str, dict[str, Any]]:
    if not SONGS_PATH.exists():
        raise SongSheetError(f"Song library not found at {SONGS_PATH}")
    return json.loads(SONGS_PATH.read_text(encoding="utf-8"))


def _format_row(song: dict[str, Any], want_to_learn: str) -> list[str]:
    name = str(song.get("name") or "").strip()
    artist = str(song.get("artist") or "").strip()
    has_learned = ", ".join(d for d in (song.get("dancers") or []) if d)
    difficulty_raw = str(song.get("difficulty") or "").strip().lower()
    difficulty = difficulty_raw.capitalize() if difficulty_raw else ""
    duo = "Yes" if song.get("duo_dance") else ""
    tags = ", ".join(
        str(t.get("text") or "").strip()
        for t in (song.get("tags") or [])
        if isinstance(t, dict) and t.get("text")
    )
    return [name, artist, want_to_learn, has_learned, difficulty, duo, tags]


def _find_tab_gid(service, spreadsheet_id: str, tab: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab:
            return int(props.get("sheetId", 0))
    raise SongSheetError(f"Tab '{tab}' not found in spreadsheet {spreadsheet_id}.")


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cur_row: Optional[list[str]] = None
        self._cur_cell: Optional[list[str]] = None
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag):
        if tag == "tr" and self._cur_row is not None:
            self.rows.append(self._cur_row)
            self._cur_row = None
        elif tag in ("td", "th"):
            if self._cur_row is not None and self._cur_cell is not None:
                self._cur_row.append("".join(self._cur_cell).strip())
            self._in_cell = False
            self._cur_cell = None

    def handle_data(self, data):
        if self._in_cell and self._cur_cell is not None:
            self._cur_cell.append(data)


def _example_sheet_want_to_learn() -> dict[str, str]:
    """Seed 'Want to learn' from the historical example export, for songs the
    live sheet has never had a value for (e.g. right after the live sheet is
    first created)."""
    if not EXAMPLE_SHEET_PATH.exists():
        return {}
    parser = _TableParser()
    parser.feed(EXAMPLE_SHEET_PATH.read_text(encoding="utf-8"))
    header_row = None
    for row in parser.rows:
        if "Song" in row and "Want to learn" in row:
            header_row = row
            data_rows = parser.rows[parser.rows.index(row) + 1:]
            break
    else:
        return {}
    name_idx = header_row.index("Song")
    want_idx = header_row.index("Want to learn")
    seed: dict[str, str] = {}
    for row in data_rows:
        if len(row) <= max(name_idx, want_idx):
            continue
        name = row[name_idx].replace("​", "").strip()
        want = row[want_idx].replace("​", "").strip()
        if name and want:
            seed[name] = want
    return seed


def _existing_want_to_learn(service, spreadsheet_id: str, tab: str) -> dict[str, str]:
    """Read the currently published sheet and return {song name -> 'Want to learn' value}."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{_quote_tab(tab)}!A1:G")
            .execute()
        )
    except Exception:
        return {}
    values = result.get("values", [])
    if not values:
        return {}
    header = [str(c).strip() for c in values[0]]
    try:
        name_idx = header.index("Song")
        want_idx = header.index("Want to learn")
    except ValueError:
        return {}
    preserved: dict[str, str] = {}
    for row in values[1:]:
        if len(row) <= max(name_idx, want_idx):
            continue
        song_name = str(row[name_idx]).strip()
        want_val = str(row[want_idx]).strip()
        if song_name and want_val:
            preserved[song_name] = want_val
    return preserved


def sync_song_sheet(
    *,
    spreadsheet_id: Optional[str] = None,
    tab: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    spreadsheet_id = spreadsheet_id or _env("SONG_SHEET_ID", "").strip()
    if not spreadsheet_id:
        raise SongSheetError("Missing SONG_SHEET_ID (spreadsheet ID to sync into).")
    tab = tab or _env("SONG_SHEET_TAB", DEFAULT_SHEET_TAB).strip() or DEFAULT_SHEET_TAB

    songs = _load_songs()
    ordered_songs = sorted(
        songs.values(),
        key=lambda s: str(s.get("name") or "").strip().casefold(),
    )

    service = _load_google_sheets_service()
    gid = _find_tab_gid(service, spreadsheet_id, tab)
    # Live sheet values win (a human may have edited them since the last sync);
    # the historical example export fills in songs the live sheet never had a
    # value for (e.g. right after the live sheet is first created).
    want_to_learn = _example_sheet_want_to_learn()
    want_to_learn.update(_existing_want_to_learn(service, spreadsheet_id, tab))

    rows = [HEADER_ROW]
    for song in ordered_songs:
        name = str(song.get("name") or "").strip()
        if not name:
            continue
        want = want_to_learn.get(name, "")
        rows.append(_format_row(song, want))

    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={gid}"

    if dry_run:
        return {"spreadsheet_id": spreadsheet_id, "tab": tab, "rows": len(rows) - 1, "url": sheet_url, "dry_run": True}

    # Clear old values first so a shrinking song list doesn't leave stale rows behind.
    # This only touches cell values, not formatting, so the sheet's existing styling is preserved.
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"{_quote_tab(tab)}!A1:Z", body={}
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{_quote_tab(tab)}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    return {"spreadsheet_id": spreadsheet_id, "tab": tab, "rows": len(rows) - 1, "url": sheet_url}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sync the live song library into a Google Sheet.")
    parser.add_argument("--spreadsheet-id", default=None)
    parser.add_argument("--tab", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        result = sync_song_sheet(spreadsheet_id=args.spreadsheet_id, tab=args.tab, dry_run=args.dry_run)
    except SongSheetError as exc:
        print(f"Error: {exc}")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
