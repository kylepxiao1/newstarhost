import atexit
import argparse
import asyncio
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests


CREATIVE_CENTER_URL = (
    "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/{locale}"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_DISCOVER_DANCES_URLS = (
    "https://www.tiktok.com/discover/Trending-Dances-Right-Now?lang=en",
    "https://www.tiktok.com/discover/trending-kpop-dances?lang=en",
    "https://www.tiktok.com/discover/kpop-dances-that-went-viral-on-tiktok?lang=en"
)
DEFAULT_TIKTOKAPI_NAV_TIMEOUT_MS = 10_000
GENERIC_HASHTAGS = {
    "fyp",
    "foryou",
    "foryoupage",
    "viral",
    "trending",
    "trend",
    "tiktok",
    "capcut",
    "video",
    "fy",
    "4u",
}
TOPIC_STOPWORDS = {
    "and",
    "or",
    "for",
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "with",
    "vs",
}
TOPIC_EXPANSIONS: Dict[str, Set[str]] = {
    "dance": {
        "dance",
        "dancing",
        "dancer",
        "dancechallenge",
        "dancecover",
        "choreography",
        "choreo",
        "kpopdance",
        "hiphopdance",
        "freestyledance",
        "dancevideo",
    },
    "challenge": {
        "challenge",
        "challenges",
        "openchallenge",
        "viralchallenge",
        "dancechallenge",
        "songchallenge",
    },
    "music": {
        "music",
        "song",
        "songs",
        "musictrend",
        "musicchallenge",
        "sound",
        "soundtrend",
    },
    "kpop": {
        "kpop",
        "kpopdance",
        "kpopchallenge",
        "kpopcover",
        "kpopfyp",
        "kpopedit",
    },
    "boygroup": {
        "boygroup",
        "boygroups",
        "boygroupdance",
        "boygroupchallenge",
        "bgdance",
        "bgchallenge",
        "boygrouptrend",
        "kpopboygroup",
        "boygroupcover",
    },
    "boygroups": {
        "boygroup",
        "boygroups",
        "boygroupdance",
        "boygroupchallenge",
        "bgdance",
        "bgchallenge",
        "boygrouptrend",
        "kpopboygroup",
        "boygroupcover",
    },
    "bg": {
        "bgdance",
        "bgchallenge",
        "boygroup",
        "boygroupdance",
        "boygroupchallenge",
    },
    "fitness": {
        "fitness",
        "workout",
        "gym",
        "training",
        "homeworkout",
        "fitcheck",
    },
    "fashion": {
        "fashion",
        "outfit",
        "ootd",
        "style",
        "streetstyle",
        "fitcheck",
    },
    "gaming": {
        "gaming",
        "gamer",
        "gameplay",
        "gamingclips",
        "esports",
    },
    "food": {
        "food",
        "recipe",
        "cooking",
        "foodtok",
        "easyrecipe",
    },
    "beauty": {
        "beauty",
        "makeup",
        "skincare",
        "hair",
        "hairstyle",
        "grwm",
    },
}

BOYGROUP_DANCE_CHALLENGE_TERMS: Set[str] = {
    "boygroup",
    "boygroups",
    "boygroupdance",
    "boygroupchallenge",
    "bgdance",
    "bgchallenge",
    "kpopboygroup",
    "boygroupcover",
}

VERBOSE_PROGRESS = True
VERBOSE_PROGRESS_INTERVAL_SECONDS = 20.0
_VERBOSE_PROGRESS_LAST: Dict[str, float] = {}
DISCOVER_NO_VIDEO_LINKS_ERROR = "no /video/ links found on rendered Discover page"
EARLY_BLOCK_ABORT_ATTEMPTS = 2


def set_progress_logging(enabled: bool, interval_seconds: float) -> None:
    global VERBOSE_PROGRESS
    global VERBOSE_PROGRESS_INTERVAL_SECONDS
    global _VERBOSE_PROGRESS_LAST
    VERBOSE_PROGRESS = bool(enabled)
    VERBOSE_PROGRESS_INTERVAL_SECONDS = max(1.0, float(interval_seconds))
    _VERBOSE_PROGRESS_LAST = {}


def progress_log(message: str, key: str = "", force: bool = False) -> None:
    if not VERBOSE_PROGRESS:
        return
    now_monotonic = time.monotonic()
    if key and not force:
        last = _VERBOSE_PROGRESS_LAST.get(key)
        if last is not None and (now_monotonic - last) < VERBOSE_PROGRESS_INTERVAL_SECONDS:
            return
        _VERBOSE_PROGRESS_LAST[key] = now_monotonic
    utc_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{utc_stamp} UTC] [progress] {safe_console_text(message)}", flush=True)


def is_tiktokapi_session_block_error(message: Any) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    indicators = (
        "failed to create minimum required sessions",
        "page.goto: timeout",
        "timeout 30000ms exceeded",
        "timeout 10000ms exceeded",
        "timeout",
        "emptyresponseexception",
        "zero videos returned",
        "they are detecting you're a bot",
        "device_blocked",
        "rate_limit",
        "too many connections",
    )
    return any(marker in text for marker in indicators)


def is_discover_no_video_links_error(message: Any) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return DISCOVER_NO_VIDEO_LINKS_ERROR.lower() in text


@dataclass
class HashtagSignal:
    hashtag: str
    sources: Set[str] = field(default_factory=set)
    from_videos: int = 0
    recent_play_count: int = 0
    recent_digg_count: int = 0
    age_hours_total: float = 0.0
    age_samples: int = 0
    creative_rank: Optional[int] = None
    creative_rank_diff: Optional[int] = None
    creative_video_views: Optional[int] = None
    creative_publish_cnt: Optional[int] = None
    creative_curve: List[float] = field(default_factory=list)
    global_video_count: Optional[int] = None
    global_view_count: Optional[int] = None

    def add_video(self, play_count: int, digg_count: int, age_hours: Optional[float]) -> None:
        self.from_videos += 1
        self.recent_play_count += max(0, play_count)
        self.recent_digg_count += max(0, digg_count)
        if age_hours is not None and age_hours >= 0:
            self.age_hours_total += age_hours
            self.age_samples += 1

    @property
    def avg_age_hours(self) -> Optional[float]:
        if self.age_samples <= 0:
            return None
        return self.age_hours_total / self.age_samples


@dataclass
class SongSignal:
    key: str
    title: str
    author: str
    song_id: Optional[str] = None
    sources: Set[str] = field(default_factory=set)
    video_count: int = 0
    recent_play_count: int = 0
    recent_digg_count: int = 0
    age_hours_total: float = 0.0
    age_samples: int = 0
    topic_score_total: float = 0.0
    topic_score_samples: int = 0
    hashtags: Set[str] = field(default_factory=set)
    video_urls: Set[str] = field(default_factory=set)
    video_post_times: Dict[str, int] = field(default_factory=dict)
    source_urls: Set[str] = field(default_factory=set)
    example_video_url: Optional[str] = None
    example_video_create_time: Optional[int] = None
    example_source_url: Optional[str] = None

    def add_video(
        self,
        play_count: int,
        digg_count: int,
        age_hours: Optional[float],
        topic_score: float,
        hashtags: Set[str],
        video_url: str = "",
        video_create_time: Optional[int] = None,
        source_page_url: str = "",
    ) -> None:
        self.video_count += 1
        self.recent_play_count += max(0, play_count)
        self.recent_digg_count += max(0, digg_count)
        self.topic_score_total += max(0.0, topic_score)
        self.topic_score_samples += 1
        self.hashtags.update(hashtags)
        if video_url:
            self.video_urls.add(video_url)
            if not self.example_video_url:
                self.example_video_url = video_url
        if video_url and video_create_time is not None:
            self.video_post_times[video_url] = int(video_create_time)
        if video_create_time is not None and self.example_video_create_time is None:
            self.example_video_create_time = int(video_create_time)
        if source_page_url:
            self.source_urls.add(source_page_url)
            if not self.example_source_url:
                self.example_source_url = source_page_url
        if age_hours is not None and age_hours >= 0:
            self.age_hours_total += age_hours
            self.age_samples += 1

    @property
    def avg_age_hours(self) -> Optional[float]:
        if self.age_samples <= 0:
            return None
        return self.age_hours_total / self.age_samples

    @property
    def avg_topic_score(self) -> float:
        if self.topic_score_samples <= 0:
            return 0.0
        return self.topic_score_total / self.topic_score_samples


def configure_stdout_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_max_video_age_hours(max_video_age_days: float) -> Optional[float]:
    try:
        days = float(max_video_age_days)
    except Exception:
        return None
    if days <= 0:
        return None
    return days * 24.0


def compute_video_age_hours(create_time: Optional[int], now_ts: float) -> Optional[float]:
    if create_time is None:
        return None
    try:
        age_hours = (now_ts - float(create_time)) / 3600.0
    except Exception:
        return None
    if age_hours < 0:
        return 0.0
    return age_hours


def is_video_too_old(
    create_time: Optional[int], now_ts: float, max_video_age_hours: Optional[float]
) -> bool:
    if max_video_age_hours is None:
        return False
    age_hours = compute_video_age_hours(create_time=create_time, now_ts=now_ts)
    if age_hours is None:
        return False
    return age_hours > max_video_age_hours


def format_unix_timestamp_utc(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
    except Exception:
        return None
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def format_unix_timestamp_iso(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
    except Exception:
        return None
    return dt.isoformat()


def load_env_file_values(path: Path) -> Dict[str, str]:
    env_values: Dict[str, str] = {}
    if not path.exists():
        return env_values
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return env_values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env_values.setdefault(key, value)
    return env_values


def first_non_empty(values: List[str]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def resolve_supabase_url_and_key(
    repo_root: Path,
    url_override: str = "",
    project_id_override: str = "",
    key_override: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    app_env = load_env_file_values(repo_root / "app.env")
    url = first_non_empty(
        [
            url_override,
            os.environ.get("SUPABASE_URL", ""),
            app_env.get("SUPABASE_URL", ""),
        ]
    )
    project_id = first_non_empty(
        [
            project_id_override,
            os.environ.get("SUPABASE_PROJECT_ID", ""),
            app_env.get("SUPABASE_PROJECT_ID", ""),
        ]
    )
    if not url and project_id:
        url = f"https://{project_id}.supabase.co"

    key = first_non_empty(
        [
            key_override,
            os.environ.get("SUPABASE_SECRET_KEY", ""),
            app_env.get("SUPABASE_SECRET_KEY", ""),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            app_env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            os.environ.get("SUPABASE_SERVICE_ROLE", ""),
            app_env.get("SUPABASE_SERVICE_ROLE", ""),
            os.environ.get("SUPABASE_API_KEY", ""),
            app_env.get("SUPABASE_API_KEY", ""),
        ]
    )
    return (url or None), (key or None)


def clean_supabase_table_name(value: str) -> Optional[str]:
    table = str(value or "").strip()
    if not table:
        return None
    if table.lower().startswith("public."):
        table = table.split(".", 1)[1]
    table = table.replace('"', "")
    if not re.match(r"^[A-Za-z0-9_]+$", table):
        return None
    return table


def extract_missing_supabase_column(response: requests.Response) -> Optional[str]:
    if response.status_code < 400:
        return None
    try:
        payload = response.json()
    except Exception:
        payload = {}
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or "")
    if not message:
        message = str(response.text or "")
    if "schema cache" not in message.lower():
        return None
    match = re.search(
        r"Could not find the '([A-Za-z0-9_]+)' column of '[^']+' in the schema cache",
        message,
    )
    if not match:
        return None
    return match.group(1)


def build_supabase_topic_song_rows(
    song_rows: List[Dict[str, Any]],
    topic_query: str,
    generated_at_iso: str,
) -> List[Dict[str, Any]]:
    prepared_rows: List[Dict[str, Any]] = []
    for row in song_rows:
        song = str(row.get("song_title") or "").strip()
        artist = str(row.get("song_author") or "").strip()
        song_link = str(
            row.get("sample_video_url")
            or row.get("source_page_url")
            or row.get("link")
            or ""
        ).strip()
        if not song or not artist or not song_link:
            continue

        posted_at_unix = safe_int(row.get("sample_video_posted_at_unix"))
        posted_at_iso = format_unix_timestamp_iso(posted_at_unix)
        hashtags = row.get("hashtags")
        if not isinstance(hashtags, list):
            hashtags = []
        hashtags = [str(tag).strip() for tag in hashtags if str(tag).strip()]
        recent_digg_count = safe_int(row.get("recent_digg_count")) or 0
        likes = safe_int(row.get("likes"))
        if likes is None:
            likes = recent_digg_count

        prepared_rows.append(
            {
                "topic": str(topic_query or "").strip(),
                "song": song,
                "artist": artist,
                "song_link": song_link,
                "posted_at": posted_at_iso,
                "source_page_url": str(row.get("source_page_url") or "").strip() or None,
                "status": str(row.get("status") or "").strip() or None,
                "score": float(row.get("score") or 0.0),
                "video_count": safe_int(row.get("video_count")) or 0,
                "recent_play_count": safe_int(row.get("recent_play_count")) or 0,
                "recent_digg_count": recent_digg_count,
                "likes": likes,
                "velocity_views_per_hour": float(row.get("velocity_views_per_hour") or 0.0),
                "topic_score": float(row.get("topic_score") or 0.0),
                "hashtags": hashtags,
                "generated_at": generated_at_iso,
            }
        )
    return prepared_rows


def upsert_topic_songs_to_supabase(
    session: requests.Session,
    supabase_url: str,
    supabase_key: str,
    table_name: str,
    rows: List[Dict[str, Any]],
    on_conflict: str = "song,artist",
    timeout_seconds: float = 30.0,
) -> Tuple[int, List[str]]:
    uploaded = 0
    errors: List[str] = []
    if not rows:
        return uploaded, errors

    rest_url = supabase_url.rstrip("/") + "/rest/v1"
    endpoint = f"{rest_url}/{table_name}"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    primary_conflict = str(on_conflict or "").strip() or "song,artist"
    timeout = max(1.0, float(timeout_seconds))
    missing_columns_cache: Set[str] = set()
    progress_log(
        f"Supabase row upsert loop starting: table={table_name} rows={len(rows)} timeout={timeout:.1f}s",
        force=True,
    )

    for index, row in enumerate(rows, start=1):
        if index == 1 or index % 10 == 0:
            progress_log(
                f"Supabase row upsert progress: {index}/{len(rows)} uploaded={uploaded} errors={len(errors)}",
                key="supabase-upsert-loop",
            )
        request_payload = {k: v for k, v in row.items() if k not in missing_columns_cache}

        def _request(conflict_target: str, payload: Dict[str, Any]) -> requests.Response:
            return session.post(
                endpoint,
                params={"on_conflict": conflict_target},
                json=payload,
                headers=headers,
                timeout=timeout,
            )

        conflict_targets: List[str] = [primary_conflict]
        if primary_conflict != "song_link" and request_payload.get("song_link"):
            conflict_targets.append("song_link")

        last_error_response: Optional[requests.Response] = None
        last_error_conflict: Optional[str] = None
        request_failed_exc: Optional[Exception] = None
        row_uploaded = False

        for conflict_target in conflict_targets:
            payload_for_conflict = dict(request_payload)
            for _ in range(8):
                try:
                    response = _request(conflict_target, payload_for_conflict)
                except Exception as exc:
                    request_failed_exc = exc
                    break

                if response.status_code in {200, 201, 204}:
                    uploaded += 1
                    row_uploaded = True
                    break

                missing_column = extract_missing_supabase_column(response)
                if missing_column and missing_column in payload_for_conflict:
                    payload_for_conflict.pop(missing_column, None)
                    missing_columns_cache.add(missing_column)
                    continue

                last_error_response = response
                last_error_conflict = conflict_target
                break

            if row_uploaded:
                break

        if row_uploaded:
            continue

        if request_failed_exc is not None and last_error_response is None:
            errors.append(
                f"row {index}: request failed ({type(request_failed_exc).__name__}: {request_failed_exc})"
            )
            continue

        if last_error_response is None:
            errors.append(f"row {index}: upsert failed (no response captured)")
            continue

        try:
            payload = last_error_response.json()
            body = json.dumps(payload, ensure_ascii=False)
        except Exception:
            body = last_error_response.text
        body = (body or "").strip().replace("\n", " ")
        if len(body) > 320:
            body = body[:317] + "..."
        errors.append(
            f"row {index}: upsert failed status={last_error_response.status_code} "
            f"conflict={last_error_conflict or primary_conflict} body={body}"
        )

    return uploaded, errors


def safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        try:
            return int(float(text))
        except Exception:
            return None


def clean_hashtag(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text.startswith("#"):
        text = text[1:]
    text = re.sub(r"[^a-z0-9_.]", "", text)
    if len(text) < 2:
        return None
    return text


def build_topic_terms(topic: str, extra_terms_csv: str = "") -> Set[str]:
    terms: Set[str] = set()
    if not topic and not extra_terms_csv:
        return terms

    token_matches = re.findall(r"[a-z0-9]+", (topic or "").lower())
    base_tokens = [t for t in token_matches if t and t not in TOPIC_STOPWORDS]
    for token in base_tokens:
        cleaned = clean_hashtag(token)
        if cleaned:
            terms.add(cleaned)
            terms.update(TOPIC_EXPANSIONS.get(cleaned, set()))

    phrase = clean_hashtag("".join(base_tokens))
    if phrase and len(base_tokens) > 1:
        terms.add(phrase)

    for i in range(len(base_tokens)):
        for j in range(i + 1, len(base_tokens)):
            merged = clean_hashtag(base_tokens[i] + base_tokens[j])
            if merged:
                terms.add(merged)

    for raw in (extra_terms_csv or "").split(","):
        cleaned = clean_hashtag(raw)
        if cleaned:
            terms.add(cleaned)

    token_set = set(base_tokens)
    has_dance_context = bool(
        token_set.intersection(
            {
                "dance",
                "dancing",
                "dancer",
                "dancechallenge",
                "dancechallenges",
            }
        )
    )
    has_challenge_context = bool(
        token_set.intersection(
            {
                "challenge",
                "challenges",
                "dancechallenge",
                "dancechallenges",
                "viralchallenge",
                "openchallenge",
            }
        )
    )
    if has_dance_context and has_challenge_context:
        terms.update(BOYGROUP_DANCE_CHALLENGE_TERMS)

    return {term for term in terms if term and len(term) >= 2}


def topic_relevance_score(hashtag: str, topic_terms: Set[str]) -> float:
    if not hashtag or not topic_terms:
        return 0.0
    if hashtag in topic_terms:
        return 1.0

    best = 0.0
    for term in topic_terms:
        if not term:
            continue
        if term in hashtag or hashtag in term:
            ratio = min(len(term), len(hashtag)) / max(len(term), len(hashtag))
            best = max(best, 0.55 + (0.4 * ratio))
            continue
        if hashtag.startswith(term) or term.startswith(hashtag):
            best = max(best, 0.65)
    return min(1.0, best)


def is_noise_hashtag(tag: str) -> bool:
    if not tag:
        return True
    if tag in GENERIC_HASHTAGS:
        return True
    if "instagram" in tag or tag.startswith("insta"):
        return True
    if tag.isdigit():
        return True
    for prefix in ("fyp", "foryou", "viral", "trend", "xyz"):
        if tag.startswith(prefix):
            return True
    return False


def human_number(value: Optional[int]) -> str:
    if value is None:
        return "-"
    abs_value = abs(value)
    if abs_value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def safe_console_text(value: Any) -> str:
    return str(value).encode("ascii", errors="replace").decode("ascii")


def slugify_topic(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", (value or "").lower())
    if not tokens:
        return ""
    return "_".join(tokens[:6])[:64]


def build_playwright_proxy(
    proxy_url: str,
    proxy_username: str = "",
    proxy_password: str = "",
) -> Optional[Dict[str, str]]:
    raw = (proxy_url or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if not parsed.scheme:
        raw = "http://" + raw
        parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        return None

    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    username = proxy_username or (parsed.username or "")
    password = proxy_password or (parsed.password or "")

    proxy: Dict[str, str] = {"server": server}
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy


def ensure_headed_display_with_xvfb(
    requested_headless: bool,
    xvfb_display: str,
) -> Tuple[bool, Optional[str]]:
    if requested_headless:
        return True, None
    # Use runtime platform detection to avoid editor static-analysis false positives.
    if platform.system().lower().startswith("win"):
        return False, None
    if str(os.environ.get("DISPLAY") or "").strip():
        return False, None

    xvfb_bin = shutil.which("Xvfb")
    if not xvfb_bin:
        return True, "Headed mode requested but Xvfb is unavailable; falling back to headless mode."

    display = str(xvfb_display or "").strip() or ":99"
    command = [
        xvfb_bin,
        display,
        "-screen",
        "0",
        "1920x1080x24",
        "-nolisten",
        "tcp",
        "-ac",
    ]
    try:
        xvfb_proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return (
            True,
            f"Headed mode requested but Xvfb failed to start ({exc}); falling back to headless mode.",
        )

    time.sleep(0.3)
    if xvfb_proc.poll() is not None:
        return True, "Headed mode requested but Xvfb exited early; falling back to headless mode."

    os.environ["DISPLAY"] = display

    def _cleanup_xvfb() -> None:
        try:
            if xvfb_proc.poll() is None:
                xvfb_proc.terminate()
                xvfb_proc.wait(timeout=2)
        except Exception:
            try:
                xvfb_proc.kill()
            except Exception:
                pass

    atexit.register(_cleanup_xvfb)
    return False, f"Started Xvfb on DISPLAY={display} for headed browser mode."


def load_netscape_cookie_file(path: Path) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    if not path.exists():
        return cookies

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return cookies

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not line.startswith("#HttpOnly_"):
                continue
            line = line[len("#HttpOnly_") :]

        parts = line.split("\t")
        if len(parts) != 7:
            continue
        name = (parts[5] or "").strip()
        value = (parts[6] or "").strip()
        if name:
            cookies[name] = value
    return cookies


def ensure_tiktokapi_path(repo_root: Path) -> bool:
    try:
        import TikTokApi  # noqa: F401
        return True
    except Exception:
        pass

    candidates = [
        repo_root / "testing" / "tiktok-scraper" / "Lib" / "site-packages",
        repo_root / "tiktok-scraper" / "Lib" / "site-packages",  # legacy fallback
    ]
    for candidate in candidates:
        if candidate.exists():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)
    try:
        import TikTokApi  # noqa: F401
        return True
    except Exception:
        return False


def extract_hashtags_from_video(video: Dict[str, Any]) -> Set[str]:
    hashtags: Set[str] = set()

    for challenge in video.get("challenges", []) or []:
        if not isinstance(challenge, dict):
            continue
        cleaned = clean_hashtag(challenge.get("title"))
        if cleaned and not is_noise_hashtag(cleaned):
            hashtags.add(cleaned)

    for text_extra in video.get("textExtra", []) or []:
        if not isinstance(text_extra, dict):
            continue
        cleaned = clean_hashtag(text_extra.get("hashtagName"))
        if cleaned and not is_noise_hashtag(cleaned):
            hashtags.add(cleaned)

    desc = video.get("desc") or ""
    if isinstance(desc, str):
        for match in re.findall(r"#([A-Za-z0-9_.]{2,})", desc):
            cleaned = clean_hashtag(match)
            if cleaned and not is_noise_hashtag(cleaned):
                hashtags.add(cleaned)

    return hashtags


def parse_song_from_video(video: Dict[str, Any]) -> Optional[Tuple[str, str, str, Optional[str]]]:
    music = video.get("music")
    if not isinstance(music, dict):
        return None

    title = str(music.get("title") or music.get("musicName") or "").strip()
    author = str(music.get("authorName") or music.get("owner_handle") or music.get("author") or "").strip()
    song_id_raw = music.get("id")
    song_id = str(song_id_raw).strip() if song_id_raw is not None else None

    if not title:
        return None
    # Skip placeholder labels that are usually not useful song identifiers.
    if title.lower() in {"original sound", "original sound -"}:
        return None

    if not author:
        author = "unknown"
    key = f"{title.lower()}|{author.lower()}"
    return key, title, author, song_id


def build_tiktok_video_url(video: Dict[str, Any], fallback_item_id: Any = None) -> Optional[str]:
    share_url = str(video.get("shareUrl") or video.get("share_url") or "").strip()
    if share_url.startswith("http://") or share_url.startswith("https://"):
        return share_url

    item_id = (
        video.get("id")
        or video.get("awemeId")
        or video.get("aweme_id")
        or fallback_item_id
    )
    item_id_text = str(item_id).strip() if item_id is not None else ""
    if not item_id_text:
        return None

    author = video.get("author")
    author_handle = ""
    if isinstance(author, dict):
        author_handle = str(author.get("uniqueId") or author.get("unique_id") or "").strip()
    if author_handle:
        return f"https://www.tiktok.com/@{author_handle}/video/{item_id_text}"
    return f"https://www.tiktok.com/@_/video/{item_id_text}"


def normalize_tiktok_video_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("/"):
        text = "https://www.tiktok.com" + text
    elif text.startswith("http://"):
        text = "https://" + text[len("http://") :]

    if "/video/" not in text:
        return None
    if not text.startswith("https://www.tiktok.com/"):
        return None

    item_id = extract_video_id_from_url(text)
    if not item_id:
        return None
    # Keep canonical path without query/fragment noise.
    prefix = text.split("?", 1)[0].split("#", 1)[0]
    if "/video/" not in prefix:
        return None
    return prefix


def video_topic_match_score(
    video: Dict[str, Any],
    topic_terms: Set[str],
    hashtags: Optional[Set[str]] = None,
) -> float:
    if not topic_terms:
        return 0.0

    tags = hashtags if hashtags is not None else extract_hashtags_from_video(video)
    best = 0.0
    for tag in tags:
        best = max(best, topic_relevance_score(tag, topic_terms))

    desc = str(video.get("desc") or "").lower()
    challenges = video.get("challenges") or []
    challenge_titles = []
    if isinstance(challenges, list):
        for entry in challenges:
            if isinstance(entry, dict):
                text = str(entry.get("title") or "").strip().lower()
                if text:
                    challenge_titles.append(text)

    music = video.get("music") if isinstance(video.get("music"), dict) else {}
    music_title = str(music.get("title") or music.get("musicName") or "").lower()

    for term in topic_terms:
        if term in desc:
            best = max(best, 0.45)
        if music_title and term in music_title:
            best = max(best, 0.5)
        if any(term in challenge_title for challenge_title in challenge_titles):
            best = max(best, 0.55)
    return min(1.0, best)


def video_has_challenge_context(video: Dict[str, Any], hashtags: Optional[Set[str]] = None) -> bool:
    tags = hashtags if hashtags is not None else extract_hashtags_from_video(video)
    if any("challenge" in tag for tag in tags):
        return True

    desc = str(video.get("desc") or "").lower()
    if "challenge" in desc:
        return True

    challenges = video.get("challenges") or []
    if isinstance(challenges, list):
        for entry in challenges:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").lower()
            if "challenge" in title:
                return True
    return False


def add_song_signal_from_video(
    song_signals: Dict[str, SongSignal],
    video: Dict[str, Any],
    topic_terms: Set[str],
    play_count: int,
    digg_count: int,
    age_hours: Optional[float],
    hashtags: Optional[Set[str]] = None,
    source: str = "topic_video",
    challenge_context_override: bool = False,
    min_topic_score_override: Optional[float] = None,
    video_url: str = "",
    video_create_time: Optional[int] = None,
    source_page_url: str = "",
) -> bool:
    if not topic_terms:
        return False
    tags = hashtags if hashtags is not None else extract_hashtags_from_video(video)
    topic_score = video_topic_match_score(video, topic_terms, hashtags=tags)
    require_challenge_context = any("challenge" in term for term in topic_terms)
    min_topic_score = 0.4 if require_challenge_context else 0.35
    if min_topic_score_override is not None:
        min_topic_score = max(0.0, min(1.0, min_topic_score_override))
    if topic_score < min_topic_score:
        return False
    if (
        require_challenge_context
        and not challenge_context_override
        and not video_has_challenge_context(video, hashtags=tags)
    ):
        return False

    parsed = parse_song_from_video(video)
    if not parsed:
        return False
    key, title, author, song_id = parsed
    signal = song_signals.setdefault(
        key,
        SongSignal(
            key=key,
            title=title,
            author=author,
            song_id=song_id,
        ),
    )
    signal.sources.add(source)
    if song_id and not signal.song_id:
        signal.song_id = song_id
    signal.add_video(
        play_count=play_count,
        digg_count=digg_count,
        age_hours=age_hours,
        topic_score=topic_score,
        hashtags=tags,
        video_url=video_url,
        video_create_time=video_create_time,
        source_page_url=source_page_url,
    )
    return True


def merge_signals(
    existing: Dict[str, HashtagSignal], incoming: Dict[str, HashtagSignal]
) -> Dict[str, HashtagSignal]:
    for tag, src in incoming.items():
        if tag not in existing:
            existing[tag] = src
            continue

        dst = existing[tag]
        dst.sources.update(src.sources)
        dst.from_videos += src.from_videos
        dst.recent_play_count += src.recent_play_count
        dst.recent_digg_count += src.recent_digg_count
        dst.age_hours_total += src.age_hours_total
        dst.age_samples += src.age_samples

        if src.creative_rank is not None:
            if dst.creative_rank is None or src.creative_rank < dst.creative_rank:
                dst.creative_rank = src.creative_rank
        if src.creative_rank_diff is not None:
            dst.creative_rank_diff = src.creative_rank_diff
        if src.creative_video_views is not None:
            dst.creative_video_views = src.creative_video_views
        if src.creative_publish_cnt is not None:
            dst.creative_publish_cnt = src.creative_publish_cnt
        if src.creative_curve:
            dst.creative_curve = src.creative_curve
        if src.global_video_count is not None:
            dst.global_video_count = src.global_video_count
        if src.global_view_count is not None:
            dst.global_view_count = src.global_view_count

    return existing


def merge_song_signals(
    existing: Dict[str, SongSignal], incoming: Dict[str, SongSignal]
) -> Dict[str, SongSignal]:
    for key, src in incoming.items():
        if key not in existing:
            existing[key] = src
            continue
        dst = existing[key]
        dst.sources.update(src.sources)
        dst.video_count += src.video_count
        dst.recent_play_count += src.recent_play_count
        dst.recent_digg_count += src.recent_digg_count
        dst.age_hours_total += src.age_hours_total
        dst.age_samples += src.age_samples
        dst.topic_score_total += src.topic_score_total
        dst.topic_score_samples += src.topic_score_samples
        dst.hashtags.update(src.hashtags)
        dst.video_urls.update(src.video_urls)
        dst.video_post_times.update(src.video_post_times)
        dst.source_urls.update(src.source_urls)
        if not dst.song_id and src.song_id:
            dst.song_id = src.song_id
        if not dst.example_video_url and src.example_video_url:
            dst.example_video_url = src.example_video_url
        if dst.example_video_create_time is None and src.example_video_create_time is not None:
            dst.example_video_create_time = src.example_video_create_time
        if not dst.example_source_url and src.example_source_url:
            dst.example_source_url = src.example_source_url
    return existing


def fetch_creative_center_signals(
    session: requests.Session, locale: str
) -> Tuple[Dict[str, HashtagSignal], Optional[str]]:
    url = CREATIVE_CENTER_URL.format(locale=locale)
    headers = {"User-Agent": USER_AGENT}
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        html = response.text
    except Exception as exc:
        return {}, f"Creative Center fetch failed: {exc}"

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return {}, "Creative Center page did not include __NEXT_DATA__ payload."

    try:
        payload = json.loads(match.group(1))
    except Exception as exc:
        return {}, f"Creative Center payload parse failed: {exc}"

    queries = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    if not isinstance(queries, list):
        return {}, "Creative Center payload format changed (queries missing)."

    list_query = None
    for query in queries:
        query_key = query.get("queryKey")
        if (
            isinstance(query_key, list)
            and len(query_key) >= 3
            and query_key[0] == "trend"
            and query_key[1] == "hashtag"
            and query_key[2] == "list"
        ):
            list_query = query
            break

    if not list_query:
        return {}, "Creative Center hashtag list query not found."

    pages = (
        list_query.get("state", {})
        .get("data", {})
        .get("pages", [])
    )
    if not isinstance(pages, list):
        return {}, "Creative Center hashtag pages missing."

    signals: Dict[str, HashtagSignal] = {}
    for page in pages:
        for item in page.get("list", []) or []:
            if not isinstance(item, dict):
                continue
            tag = clean_hashtag(item.get("hashtagName"))
            if not tag or is_noise_hashtag(tag):
                continue

            signal = signals.setdefault(tag, HashtagSignal(hashtag=tag))
            signal.sources.add("creative_center")
            signal.creative_rank = safe_int(item.get("rank"))
            signal.creative_rank_diff = safe_int(item.get("rankDiff"))
            signal.creative_video_views = safe_int(item.get("videoViews"))
            signal.creative_publish_cnt = safe_int(item.get("publishCnt"))
            curve = []
            for point in item.get("trend", []) or []:
                if not isinstance(point, dict):
                    continue
                try:
                    curve.append(float(point.get("value", 0)))
                except Exception:
                    continue
            if curve:
                signal.creative_curve = curve

    return signals, None


def fetch_creative_center_music_rows(
    session: requests.Session,
    locale: str,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    url = f"https://ads.tiktok.com/business/creativecenter/inspiration/popular/music/pc/{locale}"
    headers = {"User-Agent": USER_AGENT}
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        html = response.text
    except Exception as exc:
        return [], f"Creative Center music fetch failed: {exc}"

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return [], "Creative Center music page missing __NEXT_DATA__."

    try:
        payload = json.loads(match.group(1))
    except Exception as exc:
        return [], f"Creative Center music payload parse failed: {exc}"

    page_props = payload.get("props", {}).get("pageProps", {})
    data = page_props.get("data", {}) if isinstance(page_props, dict) else {}
    sound_list = data.get("soundList", []) if isinstance(data, dict) else []
    if not isinstance(sound_list, list):
        return [], "Creative Center music payload format changed (soundList missing)."

    rows: List[Dict[str, Any]] = []
    for item in sound_list:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or "").strip()
        if not title:
            continue
        trend_values: List[float] = []
        for point in item.get("trend", []) or []:
            if not isinstance(point, dict):
                continue
            try:
                trend_values.append(float(point.get("value", 0)))
            except Exception:
                continue

        rows.append(
            {
                "title": title,
                "author": author or "unknown",
                "rank": safe_int(item.get("rank")),
                "song_id": str(item.get("songId")).strip() if item.get("songId") is not None else None,
                "link": item.get("link"),
                "trend_values": trend_values,
                "related_item_ids": [
                    str(rel.get("itemId")).strip()
                    for rel in (item.get("relatedItems") or [])
                    if isinstance(rel, dict) and rel.get("itemId")
                ],
            }
        )
    return rows, None


def fetch_topic_web_candidates(
    session: requests.Session,
    topic_terms: Set[str],
    candidate_limit: int,
) -> Tuple[List[str], List[str]]:
    if not topic_terms:
        return [], []

    errors: List[str] = []
    headers = {"User-Agent": USER_AGENT}
    terms = sorted(topic_terms, key=lambda x: (-len(x), x))[:8]
    source_urls: List[str] = []
    for term in terms:
        source_urls.append(f"https://best-hashtags.com/hashtag/{term}/")
        source_urls.append(f"https://tiktokhashtags.com/hashtag/{term}/")

    freq: Dict[str, int] = {}
    for url in source_urls:
        try:
            response = session.get(url, headers=headers, timeout=20)
            if response.status_code != 200:
                errors.append(f"topic candidate source {url} returned status {response.status_code}")
                continue
            tags = re.findall(r"#([A-Za-z0-9_]{3,40})", response.text)
            for raw in tags:
                tag = clean_hashtag(raw)
                if not tag:
                    continue
                if is_noise_hashtag(tag):
                    continue
                if topic_relevance_score(tag, topic_terms) < 0.25:
                    continue
                freq[tag] = freq.get(tag, 0) + 1
        except Exception as exc:
            errors.append(f"topic candidate source {url} failed: {exc}")

    ranked = sorted(freq.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))
    return [tag for tag, _ in ranked[: max(1, candidate_limit)]], errors


def parse_discover_source_urls(*values: Any) -> List[str]:
    candidates: List[str] = []

    def _append_value(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _append_value(item)
            return
        text = str(value).strip()
        if not text:
            return
        for part in text.split(","):
            part_text = str(part).strip()
            if part_text:
                candidates.append(part_text)

    for value in values:
        _append_value(value)
    seen: Set[str] = set()
    urls: List[str] = []
    for raw in candidates:
        url = str(raw or "").strip()
        if not url:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def extract_discover_video_urls_from_html(html: str, max_urls: int) -> List[str]:
    if not html:
        return []

    max_count = max(1, int(max_urls))
    sources = [html]
    if "\\/" in html:
        sources.append(html.replace("\\/", "/"))

    candidates: List[str] = []
    seen_raw: Set[str] = set()

    def add_candidate(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        if text in seen_raw:
            return
        seen_raw.add(text)
        candidates.append(text)

    url_patterns = [
        r"https?://www\.tiktok\.com/[^\s\"'<>]*/video/\d+",
        r"//www\.tiktok\.com/[^\s\"'<>]*/video/\d+",
        r"/@[A-Za-z0-9._-]+/video/\d+",
        r"/video/\d+",
    ]
    for source in sources:
        for pattern in url_patterns:
            for match in re.findall(pattern, source):
                add_candidate(match)

    id_patterns = [
        r'"itemId":"(\d{15,22})"',
        r'"aweme_id":"(\d{15,22})"',
        r'"awemeId":"(\d{15,22})"',
    ]
    for source in sources:
        for pattern in id_patterns:
            for item_id in re.findall(pattern, source):
                add_candidate(f"https://www.tiktok.com/@_/video/{item_id}")

    urls: List[str] = []
    seen_urls: Set[str] = set()
    seen_item_ids: Set[str] = set()
    for candidate in candidates:
        item_id = extract_video_id_from_url(candidate)
        candidate_lc = str(candidate or "").strip().lower()
        if item_id and (
            candidate_lc.startswith("/video/")
            or candidate_lc.startswith("https://www.tiktok.com/video/")
            or candidate_lc.startswith("//www.tiktok.com/video/")
        ):
            normalized = f"https://www.tiktok.com/@_/video/{item_id}"
        else:
            normalized = normalize_tiktok_video_url(candidate)
            if not normalized and item_id:
                normalized = f"https://www.tiktok.com/@_/video/{item_id}"
        normalized_item_id = extract_video_id_from_url(normalized)
        if not normalized:
            continue
        if normalized_item_id and normalized_item_id in seen_item_ids:
            continue
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        if normalized_item_id:
            seen_item_ids.add(normalized_item_id)
        urls.append(normalized)
        if len(urls) >= max_count:
            break
    return urls


async def fetch_discover_video_urls_once(
    repo_root: Path,
    discover_url: str,
    browser: str,
    headless: bool,
    max_urls: int,
    scroll_rounds: int,
    headful_wait_seconds: float,
    proxy: Optional[Dict[str, str]],
) -> Tuple[List[str], Optional[str]]:
    progress_log(
        f"Discover attempt start: url={discover_url} browser={browser} headless={headless}",
        force=True,
    )
    if not ensure_tiktokapi_path(repo_root):
        return [], "Playwright package not available for Discover scraping."

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return [], f"Playwright import failed for Discover scraping: {exc}"

    launch_kwargs: Dict[str, Any] = {"headless": headless}
    if proxy:
        launch_kwargs["proxy"] = proxy

    urls: List[str] = []
    seen: Set[str] = set()
    try:
        async with async_playwright() as playwright:
            launcher = getattr(playwright, browser)
            browser_obj = await launcher.launch(**launch_kwargs)
            context = await browser_obj.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            goto_started = time.monotonic()
            progress_log(f"Discover goto start: {discover_url}", force=True)
            await page.goto(discover_url, wait_until="networkidle", timeout=90_000)
            progress_log(
                f"Discover goto complete in {time.monotonic() - goto_started:.1f}s: {discover_url}",
                force=True,
            )
            if not headless and headful_wait_seconds > 0:
                await page.wait_for_timeout(int(headful_wait_seconds * 1000))

            total_scroll_rounds = max(0, scroll_rounds)
            for idx in range(total_scroll_rounds):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(1200)
                progress_log(
                    f"Discover scrolling: {discover_url} round {idx + 1}/{total_scroll_rounds}",
                    key=f"discover-scroll-{discover_url}",
                )

            hrefs = await page.eval_on_selector_all(
                "a[href*='/video/']",
                "els => els.map(e => e.getAttribute('href'))",
            )
            progress_log(
                f"Discover extracted anchor hrefs: {discover_url} count={len(hrefs or [])}",
                force=True,
            )
            for href in hrefs or []:
                normalized = normalize_tiktok_video_url(href)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                urls.append(normalized)
                if len(urls) >= max(1, max_urls):
                    break

            if len(urls) < max(1, max_urls):
                page_html = await page.content()
                fallback_urls = extract_discover_video_urls_from_html(
                    html=page_html,
                    max_urls=max(1, max_urls),
                )
                progress_log(
                    f"Discover html fallback extracted: {discover_url} count={len(fallback_urls)}",
                    force=True,
                )
                for fallback_url in fallback_urls:
                    if fallback_url in seen:
                        continue
                    seen.add(fallback_url)
                    urls.append(fallback_url)
                    if len(urls) >= max(1, max_urls):
                        break

            await context.close()
            await browser_obj.close()
    except Exception as exc:
        progress_log(
            f"Discover attempt exception: {discover_url} browser={browser} headless={headless} "
            f"error={type(exc).__name__}: {exc}",
            force=True,
        )
        return [], f"{type(exc).__name__}: {exc}"

    if not urls:
        progress_log(f"Discover attempt ended with zero urls: {discover_url}", force=True)
        return [], DISCOVER_NO_VIDEO_LINKS_ERROR
    progress_log(f"Discover attempt success: {discover_url} urls={len(urls)}", force=True)
    return urls, None


async def fetch_discover_video_urls(
    repo_root: Path,
    discover_url: str,
    browser: str,
    headless: bool,
    max_attempts: int,
    max_urls: int,
    scroll_rounds: int,
    headful_wait_seconds: float,
    proxy: Optional[Dict[str, str]],
) -> Tuple[List[str], Optional[str]]:
    strategies = build_tiktokapi_strategies(
        preferred_browser=browser,
        preferred_headless=headless,
        max_attempts=max_attempts,
    )
    attempt_errors: List[str] = []
    no_video_link_failures = 0
    block_signature_failures = 0
    raw_html_fallback_attempted = False

    for attempt_index, (attempt_browser, attempt_headless) in enumerate(strategies, start=1):
        progress_log(
            f"Discover retries attempt {attempt_index}/{len(strategies)} "
            f"browser={attempt_browser} headless={attempt_headless} url={discover_url}",
            force=True,
        )
        urls, error = await fetch_discover_video_urls_once(
            repo_root=repo_root,
            discover_url=discover_url,
            browser=attempt_browser,
            headless=attempt_headless,
            max_urls=max_urls,
            scroll_rounds=scroll_rounds,
            headful_wait_seconds=headful_wait_seconds,
            proxy=proxy,
        )
        if urls:
            progress_log(
                f"Discover retries success at attempt {attempt_index}: {discover_url} urls={len(urls)}",
                force=True,
            )
            return urls, None
        reason = error or "unknown"
        attempt_errors.append(
            f"attempt {attempt_index} ({attempt_browser}, headless={attempt_headless}) failed: {reason}"
        )
        progress_log(
            f"Discover retries failed attempt {attempt_index}: {discover_url} reason={reason}",
            force=True,
        )
        if is_discover_no_video_links_error(reason):
            no_video_link_failures += 1
            if not raw_html_fallback_attempted:
                raw_html_fallback_attempted = True
                progress_log(
                    f"Discover raw-html fallback fetch start: {discover_url}",
                    force=True,
                )
                try:
                    response = requests.get(
                        discover_url,
                        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                        timeout=20,
                    )
                    if response.status_code == 200:
                        fallback_urls = extract_discover_video_urls_from_html(
                            html=response.text,
                            max_urls=max(1, max_urls),
                        )
                        progress_log(
                            f"Discover raw-html fallback parsed: {discover_url} urls={len(fallback_urls)}",
                            force=True,
                        )
                        if fallback_urls:
                            return fallback_urls, None
                        attempt_errors.append(
                            f"raw-html fallback ({discover_url}) found zero /video/ links"
                        )
                    else:
                        attempt_errors.append(
                            f"raw-html fallback ({discover_url}) returned status {response.status_code}"
                        )
                except Exception as exc:
                    attempt_errors.append(
                        f"raw-html fallback ({discover_url}) failed: {type(exc).__name__}: {exc}"
                    )
            if no_video_link_failures >= EARLY_BLOCK_ABORT_ATTEMPTS:
                attempt_errors.append(
                    "stopped Discover retries early after repeated no-video-link responses"
                )
                progress_log(
                    "Discover retries early-stop: repeated no-video-link responses",
                    force=True,
                )
                break
        elif is_tiktokapi_session_block_error(reason):
            block_signature_failures += 1
            if block_signature_failures >= EARLY_BLOCK_ABORT_ATTEMPTS:
                attempt_errors.append(
                    "stopped Discover retries early after repeated timeout/block signatures"
                )
                progress_log(
                    "Discover retries early-stop: repeated timeout/block signatures",
                    force=True,
                )
                break

    return [], "Discover page scrape failed across retries: " + " | ".join(attempt_errors)


def build_tiktokapi_strategies(
    preferred_browser: str,
    preferred_headless: bool,
    max_attempts: int,
) -> List[Tuple[str, bool]]:
    primary_mode = bool(preferred_headless)
    fallback_mode = not primary_mode
    candidates: List[Tuple[str, bool]] = [
        (preferred_browser, primary_mode),
        ("firefox", primary_mode),
        ("webkit", primary_mode),
        ("chromium", primary_mode),
        (preferred_browser, fallback_mode),
        ("firefox", fallback_mode),
        ("webkit", fallback_mode),
        ("chromium", fallback_mode),
    ]
    unique: List[Tuple[str, bool]] = []
    seen: Set[Tuple[str, bool]] = set()
    for row in candidates:
        if row in seen:
            continue
        seen.add(row)
        unique.append(row)
    return unique[: max(1, max_attempts)]


async def collect_tiktokapi_once(
    TikTokApi: Any,
    browser: str,
    headless: bool,
    video_count: int,
    max_lookups: int,
    lookup_delay: float,
    api_navigation_timeout_ms: int,
    cookies: Optional[Dict[str, str]],
    headful_wait_seconds: float,
    proxy: Optional[Dict[str, str]],
    topic_terms: Optional[Set[str]] = None,
    max_video_age_hours: Optional[float] = None,
) -> Tuple[Dict[str, HashtagSignal], Dict[str, SongSignal], int, Optional[str]]:
    signals: Dict[str, HashtagSignal] = {}
    song_signals: Dict[str, SongSignal] = {}
    current_ts = time.time()
    fetched_videos = 0

    try:
        async with TikTokApi(logging_level=50) as api:
            session_kwargs: Dict[str, Any] = {
                "num_sessions": 1,
                "browser": browser,
                "headless": headless,
                "allow_partial_sessions": True,
                "starting_url": "https://www.tiktok.com/foryou",
                "timeout": max(1, int(api_navigation_timeout_ms)),
            }
            if proxy:
                session_kwargs["proxies"] = [proxy]
            if cookies:
                session_kwargs["cookies"] = [cookies]
                if cookies.get("msToken"):
                    session_kwargs["ms_tokens"] = [cookies["msToken"]]

            progress_log(
                f"TikTokApi create_sessions start: browser={browser} headless={headless}",
                force=True,
            )
            session_started = time.monotonic()
            await api.create_sessions(**session_kwargs)
            progress_log(
                f"TikTokApi create_sessions complete in {time.monotonic() - session_started:.1f}s: "
                f"browser={browser} headless={headless}",
                force=True,
            )
            if not headless and headful_wait_seconds > 0:
                await asyncio.sleep(headful_wait_seconds)

            async for video in api.trending.videos(count=max(1, video_count)):
                fetched_videos += 1
                if fetched_videos % 25 == 0:
                    progress_log(
                        f"TikTokApi trending fetch progress: browser={browser} fetched={fetched_videos}/{max(1, video_count)}",
                        key=f"api-videos-{browser}-{headless}",
                    )
                payload = getattr(video, "as_dict", None) or {}
                stats = payload.get("stats", {}) or {}
                play_count = safe_int(stats.get("playCount")) or 0
                digg_count = safe_int(stats.get("diggCount")) or 0
                create_time = safe_int(payload.get("createTime"))
                if is_video_too_old(
                    create_time=create_time,
                    now_ts=current_ts,
                    max_video_age_hours=max_video_age_hours,
                ):
                    continue
                age_hours = compute_video_age_hours(create_time=create_time, now_ts=current_ts)

                video_hashtags = extract_hashtags_from_video(payload)
                for tag in video_hashtags:
                    signal = signals.setdefault(tag, HashtagSignal(hashtag=tag))
                    signal.sources.add("trending_feed")
                    signal.add_video(play_count=play_count, digg_count=digg_count, age_hours=age_hours)
                if topic_terms:
                    video_url = build_tiktok_video_url(payload) if isinstance(payload, dict) else None
                    add_song_signal_from_video(
                        song_signals=song_signals,
                        video=payload,
                        topic_terms=topic_terms,
                        play_count=play_count,
                        digg_count=digg_count,
                        age_hours=age_hours,
                        hashtags=video_hashtags,
                        source="trending_feed_song",
                        video_url=video_url or "",
                        video_create_time=create_time,
                    )

            lookup_candidates = sorted(
                signals.values(),
                key=lambda row: (row.from_videos, row.recent_play_count),
                reverse=True,
            )[: max(0, max_lookups)]
            progress_log(
                f"TikTokApi hashtag info lookup candidates: browser={browser} count={len(lookup_candidates)}",
                force=True,
            )

            for index, signal in enumerate(lookup_candidates, start=1):
                try:
                    info = await api.hashtag(name=signal.hashtag).info()
                except Exception:
                    continue
                stats = info.get("challengeInfo", {}).get("stats", {}) if isinstance(info, dict) else {}
                if isinstance(stats, dict):
                    signal.global_video_count = safe_int(stats.get("videoCount"))
                    signal.global_view_count = safe_int(stats.get("viewCount"))
                    signal.sources.add("hashtag_info")
                if lookup_delay > 0:
                    await asyncio.sleep(lookup_delay)
                if index % 10 == 0:
                    progress_log(
                        f"TikTokApi hashtag info progress: browser={browser} {index}/{len(lookup_candidates)}",
                        key=f"api-lookup-{browser}-{headless}",
                    )
    except Exception as exc:
        progress_log(
            f"TikTokApi collect attempt exception: browser={browser} headless={headless} "
            f"error={type(exc).__name__}: {exc}",
            force=True,
        )
        return signals, song_signals, fetched_videos, f"{type(exc).__name__}: {exc}"

    progress_log(
        f"TikTokApi collect attempt done: browser={browser} headless={headless} "
        f"fetched_videos={fetched_videos} hashtags={len(signals)} songs={len(song_signals)}",
        force=True,
    )
    return signals, song_signals, fetched_videos, None


async def fetch_tiktokapi_signals(
    repo_root: Path,
    video_count: int,
    browser: str,
    headless: bool,
    max_lookups: int,
    lookup_delay: float,
    api_navigation_timeout_ms: int,
    max_attempts: int,
    cookies: Optional[Dict[str, str]],
    headful_wait_seconds: float,
    proxy: Optional[Dict[str, str]],
    topic_terms: Optional[Set[str]] = None,
    max_video_age_hours: Optional[float] = None,
) -> Tuple[Dict[str, HashtagSignal], Dict[str, SongSignal], Optional[str]]:
    if not ensure_tiktokapi_path(repo_root):
        return {}, {}, "TikTokApi package not available (skipping direct TikTok trend feed)."

    try:
        from TikTokApi import TikTokApi
    except Exception as exc:
        return {}, {}, f"TikTokApi import failed: {exc}"

    strategies = build_tiktokapi_strategies(
        preferred_browser=browser,
        preferred_headless=headless,
        max_attempts=max_attempts,
    )
    progress_log(
        f"TikTokApi strategies prepared: {len(strategies)} attempt(s)",
        force=True,
    )
    attempt_errors: List[str] = []
    block_signature_failures = 0
    for attempt_index, (attempt_browser, attempt_headless) in enumerate(strategies, start=1):
        progress_log(
            f"TikTokApi attempt {attempt_index}/{len(strategies)} starting: "
            f"browser={attempt_browser} headless={attempt_headless}",
            force=True,
        )
        attempt_started = time.monotonic()
        signals, song_signals, fetched_videos, attempt_error = await collect_tiktokapi_once(
            TikTokApi=TikTokApi,
            browser=attempt_browser,
            headless=attempt_headless,
            video_count=video_count,
            max_lookups=max_lookups,
            lookup_delay=lookup_delay,
            api_navigation_timeout_ms=api_navigation_timeout_ms,
            cookies=cookies,
            headful_wait_seconds=headful_wait_seconds,
            proxy=proxy,
            topic_terms=topic_terms,
            max_video_age_hours=max_video_age_hours,
        )
        progress_log(
            f"TikTokApi attempt {attempt_index} completed in {time.monotonic() - attempt_started:.1f}s: "
            f"fetched_videos={fetched_videos} hashtags={len(signals)} songs={len(song_signals)}",
            force=True,
        )
        if fetched_videos > 0:
            if signals or song_signals:
                return signals, song_signals, None
            attempt_errors.append(
                f"attempt {attempt_index} ({attempt_browser}, headless={attempt_headless}) had videos but no usable hashtags/songs after filtering"
            )
        else:
            reason = attempt_error or "zero videos returned"
            attempt_errors.append(
                f"attempt {attempt_index} ({attempt_browser}, headless={attempt_headless}) failed: {reason}"
            )
            progress_log(
                f"TikTokApi attempt {attempt_index} failed: {reason}",
                force=True,
            )
            if is_tiktokapi_session_block_error(reason):
                block_signature_failures += 1
                if block_signature_failures >= EARLY_BLOCK_ABORT_ATTEMPTS:
                    attempt_errors.append(
                        "stopped retries early after repeated TikTokApi timeout/session-block signatures"
                    )
                    progress_log(
                        "TikTokApi retries early-stop: repeated timeout/session-block signatures",
                        force=True,
                    )
                    break

    return {}, {}, "TikTokApi trend feed failed across retries: " + " | ".join(attempt_errors)


async def fetch_topic_seed_signals(
    repo_root: Path,
    topic_terms: Set[str],
    candidate_terms: List[str],
    browser: str,
    headless: bool,
    api_navigation_timeout_ms: int,
    max_attempts: int,
    cookies: Optional[Dict[str, str]],
    headful_wait_seconds: float,
    seed_limit: int,
    lookup_delay: float,
    proxy: Optional[Dict[str, str]],
    topic_video_samples: int,
    max_video_age_hours: Optional[float] = None,
) -> Tuple[Dict[str, HashtagSignal], Dict[str, SongSignal], Optional[str]]:
    if not topic_terms:
        return {}, {}, None
    if not ensure_tiktokapi_path(repo_root):
        return {}, {}, "TikTokApi package not available (skipping topic seed lookups)."

    try:
        from TikTokApi import TikTokApi
    except Exception as exc:
        return {}, {}, f"TikTokApi import failed for topic seed lookup: {exc}"

    candidate_term_set = set(candidate_terms)
    merged_seed_terms: List[str] = []
    for tag in candidate_terms:
        if tag not in merged_seed_terms:
            merged_seed_terms.append(tag)
    for tag in sorted(topic_terms):
        if tag not in merged_seed_terms:
            merged_seed_terms.append(tag)
    seed_terms = merged_seed_terms[: max(1, seed_limit)]
    strategies = build_tiktokapi_strategies(
        preferred_browser=browser,
        preferred_headless=headless,
        max_attempts=max_attempts,
    )
    progress_log(
        f"Topic seed strategies prepared: seeds={len(seed_terms)} attempts={len(strategies)}",
        force=True,
    )
    attempt_errors: List[str] = []
    block_signature_failures = 0

    for attempt_index, (attempt_browser, attempt_headless) in enumerate(strategies, start=1):
        signals: Dict[str, HashtagSignal] = {}
        song_signals: Dict[str, SongSignal] = {}
        current_ts = time.time()
        progress_log(
            f"Topic seed attempt {attempt_index}/{len(strategies)} starting: "
            f"browser={attempt_browser} headless={attempt_headless}",
            force=True,
        )
        attempt_started = time.monotonic()
        try:
            async with TikTokApi(logging_level=50) as api:
                session_kwargs: Dict[str, Any] = {
                    "num_sessions": 1,
                    "browser": attempt_browser,
                    "headless": attempt_headless,
                    "allow_partial_sessions": True,
                    "starting_url": "https://www.tiktok.com/tag/" + seed_terms[0],
                    "timeout": max(1, int(api_navigation_timeout_ms)),
                }
                if proxy:
                    session_kwargs["proxies"] = [proxy]
                if cookies:
                    session_kwargs["cookies"] = [cookies]
                    if cookies.get("msToken"):
                        session_kwargs["ms_tokens"] = [cookies["msToken"]]
                progress_log(
                    "Topic seed create_sessions start: "
                    f"browser={attempt_browser} headless={attempt_headless} seed={seed_terms[0]}",
                    force=True,
                )
                session_started = time.monotonic()
                await api.create_sessions(**session_kwargs)
                progress_log(
                    f"Topic seed create_sessions complete in {time.monotonic() - session_started:.1f}s: "
                    f"browser={attempt_browser} headless={attempt_headless}",
                    force=True,
                )
                if not attempt_headless and headful_wait_seconds > 0:
                    await asyncio.sleep(headful_wait_seconds)

                successful = 0
                for term_index, term in enumerate(seed_terms, start=1):
                    if term_index == 1 or term_index % 5 == 0:
                        progress_log(
                            f"Topic seed info progress: {term_index}/{len(seed_terms)} term=#{term}",
                            key=f"topic-seed-terms-{attempt_index}",
                        )
                    hashtag_obj = api.hashtag(name=term)
                    try:
                        info = await hashtag_obj.info()
                    except Exception:
                        continue

                    challenge = info.get("challengeInfo", {}).get("challenge", {}) if isinstance(info, dict) else {}
                    stats = info.get("challengeInfo", {}).get("stats", {}) if isinstance(info, dict) else {}
                    if not isinstance(stats, dict):
                        continue

                    canonical = clean_hashtag(challenge.get("title")) or clean_hashtag(term)
                    if not canonical:
                        continue

                    view_count = safe_int(stats.get("viewCount"))
                    video_count = safe_int(stats.get("videoCount"))
                    signal = signals.setdefault(canonical, HashtagSignal(hashtag=canonical))
                    signal.sources.add("topic_web_seed" if term in candidate_term_set else "topic_seed")
                    if video_count is not None:
                        signal.global_video_count = max(signal.global_video_count or 0, video_count)
                    if view_count is not None:
                        signal.global_view_count = max(signal.global_view_count or 0, view_count)
                    successful += 1
                    if lookup_delay > 0:
                        await asyncio.sleep(lookup_delay)

                    if topic_video_samples > 0:
                        pulled = 0
                        try:
                            async for video in hashtag_obj.videos(count=max(1, topic_video_samples)):
                                payload = getattr(video, "as_dict", None) or {}
                                stats_payload = payload.get("stats", {}) if isinstance(payload, dict) else {}
                                play_count = safe_int(stats_payload.get("playCount")) or 0
                                digg_count = safe_int(stats_payload.get("diggCount")) or 0
                                create_time = safe_int(payload.get("createTime")) if isinstance(payload, dict) else None
                                if is_video_too_old(
                                    create_time=create_time,
                                    now_ts=current_ts,
                                    max_video_age_hours=max_video_age_hours,
                                ):
                                    continue
                                age_hours = compute_video_age_hours(
                                    create_time=create_time, now_ts=current_ts
                                )
                                video_hashtags = extract_hashtags_from_video(payload) if isinstance(payload, dict) else set()
                                video_url = build_tiktok_video_url(payload if isinstance(payload, dict) else {})
                                add_song_signal_from_video(
                                    song_signals=song_signals,
                                    video=payload if isinstance(payload, dict) else {},
                                    topic_terms=topic_terms,
                                    play_count=play_count,
                                    digg_count=digg_count,
                                    age_hours=age_hours,
                                    hashtags=video_hashtags,
                                    source="topic_hashtag_video",
                                    video_url=video_url or "",
                                    video_create_time=create_time,
                                    source_page_url=f"https://www.tiktok.com/tag/{term}",
                                )
                                pulled += 1
                                if pulled >= max(1, topic_video_samples):
                                    break
                        except Exception:
                            pass

                if successful > 0 or song_signals:
                    progress_log(
                        "Topic seed attempt succeeded: "
                        f"successful={successful} songs={len(song_signals)} "
                        f"elapsed={time.monotonic() - attempt_started:.1f}s",
                        force=True,
                    )
                    return signals, song_signals, None
                attempt_errors.append(
                    f"attempt {attempt_index} ({attempt_browser}, headless={attempt_headless}) returned no topic seed hashtag stats"
                )
                progress_log(
                    f"Topic seed attempt {attempt_index} returned no data "
                    f"(elapsed={time.monotonic() - attempt_started:.1f}s)",
                    force=True,
                )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            attempt_errors.append(
                f"attempt {attempt_index} ({attempt_browser}, headless={attempt_headless}) failed: {reason}"
            )
            progress_log(
                f"Topic seed attempt {attempt_index} exception: {reason}",
                force=True,
            )
            if is_tiktokapi_session_block_error(reason):
                block_signature_failures += 1
                if block_signature_failures >= EARLY_BLOCK_ABORT_ATTEMPTS:
                    attempt_errors.append(
                        "stopped topic-seed retries early after repeated timeout/session-block signatures"
                    )
                    progress_log(
                        "Topic seed retries early-stop: repeated timeout/session-block signatures",
                        force=True,
                    )
                    break

    return {}, {}, "Topic seed lookup failed across retries: " + " | ".join(attempt_errors)


def compute_creative_momentum(curve: List[float]) -> float:
    if len(curve) < 2:
        return 0.0
    first = curve[0]
    last = curve[-1]
    prev_mean = statistics.mean(curve[:-1]) if len(curve) > 1 else first
    linear_gain = max(0.0, last - first)
    spike_gain = max(0.0, last - prev_mean)
    return min(1.0, (linear_gain * 0.7) + (spike_gain * 0.8))


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def score_hashtags(
    signals: Dict[str, HashtagSignal],
    history_payload: Dict[str, Any],
    topic_terms: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    history_rows = history_payload.get("hashtags", {})
    if not isinstance(history_rows, dict):
        history_rows = {}

    max_mentions = max((row.from_videos for row in signals.values()), default=1)
    max_velocity = 1.0
    velocity_map: Dict[str, float] = {}
    for tag, row in signals.items():
        age_hours = row.avg_age_hours if row.avg_age_hours is not None else 24.0
        velocity = row.recent_play_count / max(1.0, age_hours)
        velocity_map[tag] = velocity
        max_velocity = max(max_velocity, velocity)

    results: List[Dict[str, Any]] = []
    for tag, row in signals.items():
        avg_age_hours = row.avg_age_hours
        velocity = velocity_map.get(tag, 0.0)
        prev = history_rows.get(tag) if isinstance(history_rows.get(tag), dict) else None

        mentions_norm = row.from_videos / max(1, max_mentions)
        velocity_norm = velocity / max_velocity
        recency_norm = 0.0
        if avg_age_hours is not None:
            recency_norm = max(0.0, 1.0 - (avg_age_hours / 96.0))
        creative_norm = compute_creative_momentum(row.creative_curve)
        rank_norm = 0.0
        if row.creative_rank and row.creative_rank > 0:
            rank_norm = max(0.0, (21 - min(row.creative_rank, 21)) / 20.0)
        global_norm = 0.0
        novelty_norm = 0.0
        if row.global_view_count and row.global_view_count > 0:
            global_norm = min(1.0, math.log10(row.global_view_count + 1) / 12.0)
            novelty_norm = max(0.0, 1.0 - global_norm)
        elif topic_terms:
            novelty_norm = 0.5
        topic_relevance = topic_relevance_score(tag, topic_terms or set()) if topic_terms else 0.0

        base_score = (
            (mentions_norm * 0.30)
            + (velocity_norm * 0.24)
            + (recency_norm * 0.12)
            + (creative_norm * 0.14)
            + (rank_norm * 0.10)
            + (global_norm * 0.10)
        ) * 100.0
        if topic_terms:
            base_score += topic_relevance * 20.0
            base_score += novelty_norm * 12.0

        delta_mentions = None
        delta_recent_views = None
        delta_global_views = None
        delta_rank = None
        growth_bonus = 0.0

        if prev is None:
            growth_bonus += 12.0
        else:
            prev_mentions = safe_int(prev.get("from_videos")) or 0
            prev_recent = safe_int(prev.get("recent_play_count")) or 0
            prev_global = safe_int(prev.get("global_view_count"))
            prev_rank = safe_int(prev.get("creative_rank"))

            delta_mentions = row.from_videos - prev_mentions
            delta_recent_views = row.recent_play_count - prev_recent
            if row.global_view_count is not None and prev_global is not None:
                delta_global_views = row.global_view_count - prev_global
            if row.creative_rank is not None and prev_rank is not None:
                delta_rank = prev_rank - row.creative_rank

            if delta_mentions > 0:
                growth_bonus += min(8.0, delta_mentions * 1.5)
            if delta_recent_views > 0:
                growth_bonus += min(8.0, math.log10(delta_recent_views + 1) * 2.0)
            if delta_global_views is not None and delta_global_views > 0:
                growth_bonus += min(6.0, math.log10(delta_global_views + 1) * 1.5)
            if delta_rank is not None and delta_rank > 0:
                growth_bonus += min(6.0, delta_rank * 0.6)

        score = min(100.0, base_score + growth_bonus)

        if prev is None:
            status = "new"
        elif (delta_mentions is not None and delta_mentions > 0) or (delta_rank is not None and delta_rank > 0) or growth_bonus >= 5.0:
            status = "rising"
        elif score >= 25.0:
            status = "watch"
        else:
            status = "stable"

        results.append(
            {
                "hashtag": tag,
                "status": status,
                "score": round(score, 2),
                "from_videos": row.from_videos,
                "recent_play_count": row.recent_play_count,
                "recent_digg_count": row.recent_digg_count,
                "avg_age_hours": round(avg_age_hours, 2) if avg_age_hours is not None else None,
                "velocity_views_per_hour": round(velocity, 2),
                "creative_rank": row.creative_rank,
                "creative_rank_diff": row.creative_rank_diff,
                "creative_video_views": row.creative_video_views,
                "creative_publish_cnt": row.creative_publish_cnt,
                "global_video_count": row.global_video_count,
                "global_view_count": row.global_view_count,
                "sources": sorted(row.sources),
                "delta_mentions": delta_mentions,
                "delta_recent_views": delta_recent_views,
                "delta_global_views": delta_global_views,
                "delta_rank": delta_rank,
                "topic_relevance": round(topic_relevance, 3),
            }
        )

    priority = {"new": 0, "rising": 1, "watch": 2, "stable": 3}
    results.sort(
        key=lambda row: (
            -(row.get("topic_relevance") or 0.0),
            priority.get(row["status"], 9),
            -row["score"],
            -(row.get("from_videos") or 0),
            row["hashtag"],
        )
    )
    return results


def update_history(
    history_payload: Dict[str, Any],
    results: List[Dict[str, Any]],
    timestamp_iso: str,
) -> Dict[str, Any]:
    old_rows = history_payload.get("hashtags", {})
    if not isinstance(old_rows, dict):
        old_rows = {}

    new_rows: Dict[str, Dict[str, Any]] = {}
    for tag, row in old_rows.items():
        if not isinstance(row, dict):
            continue
        copied = dict(row)
        copied["missing_runs"] = (safe_int(copied.get("missing_runs")) or 0) + 1
        new_rows[tag] = copied

    for row in results:
        tag = row["hashtag"]
        previous = new_rows.get(tag, {})
        first_seen = previous.get("first_seen") or timestamp_iso
        seen_count = (safe_int(previous.get("seen_count")) or 0) + 1

        new_rows[tag] = {
            "first_seen": first_seen,
            "last_seen": timestamp_iso,
            "seen_count": seen_count,
            "missing_runs": 0,
            "score": row.get("score"),
            "status": row.get("status"),
            "from_videos": row.get("from_videos"),
            "recent_play_count": row.get("recent_play_count"),
            "global_view_count": row.get("global_view_count"),
            "creative_rank": row.get("creative_rank"),
        }

    pruned_rows = {
        tag: row
        for tag, row in new_rows.items()
        if (safe_int(row.get("missing_runs")) or 0) <= 60
    }
    return {"last_run_at": timestamp_iso, "hashtags": pruned_rows}


def apply_topic_filter(
    results: List[Dict[str, Any]],
    topic_terms: Set[str],
    min_relevance: float,
    strict: bool,
    allow_generic_topic_tags: bool,
    max_topic_global_views: Optional[int],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not topic_terms:
        return results, None

    min_rel = max(0.0, min(1.0, min_relevance))
    matched = [row for row in results if (row.get("topic_relevance") or 0.0) >= min_rel]
    if not allow_generic_topic_tags:
        specific = [row for row in matched if row.get("hashtag") not in topic_terms]
        if specific:
            matched = specific
        elif matched:
            return matched, "All topic matches are generic anchors; rerun with --allow-generic-topic-tags to include them."

    if max_topic_global_views is not None and max_topic_global_views > 0:
        narrowed = []
        for row in matched:
            gv = safe_int(row.get("global_view_count"))
            if gv is None or gv <= max_topic_global_views:
                narrowed.append(row)
        if narrowed:
            matched = narrowed
        elif matched:
            return (
                matched,
                (
                    "No topic matches passed the max global views filter; "
                    "showing broader/high-volume topic tags instead."
                ),
            )

    if strict:
        if matched:
            return matched, None

        loose = [row for row in results if (row.get("topic_relevance") or 0.0) > 0.0]
        if loose:
            return (
                loose,
                (
                    f"No hashtags met topic relevance >= {min_rel:.2f}; "
                    "showing closest topic matches instead."
                ),
            )
        return [], f"No hashtags matched topic terms at relevance >= {min_rel:.2f}."

    return matched if matched else results, None


def prefer_emerging_rows(results: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    emerging: List[Dict[str, Any]] = []
    for row in results:
        status = str(row.get("status") or "").lower()
        if status in {"new", "rising"}:
            emerging.append(row)
            continue
        if (safe_int(row.get("delta_mentions")) or 0) > 0:
            emerging.append(row)
            continue
        if (safe_int(row.get("delta_rank")) or 0) > 0:
            emerging.append(row)
            continue
        if (safe_int(row.get("delta_global_views")) or 0) > 0:
            emerging.append(row)
            continue
    if emerging:
        return emerging, True
    return results, False


def score_songs(song_signals: Dict[str, SongSignal]) -> List[Dict[str, Any]]:
    if not song_signals:
        return []

    max_videos = max((row.video_count for row in song_signals.values()), default=1)
    velocity_map: Dict[str, float] = {}
    max_velocity = 1.0
    for key, row in song_signals.items():
        age_hours = row.avg_age_hours if row.avg_age_hours is not None else 24.0
        velocity = row.recent_play_count / max(1.0, age_hours)
        velocity_map[key] = velocity
        max_velocity = max(max_velocity, velocity)

    results: List[Dict[str, Any]] = []
    for key, row in song_signals.items():
        age_hours = row.avg_age_hours
        velocity = velocity_map.get(key, 0.0)
        videos_norm = row.video_count / max(1, max_videos)
        velocity_norm = velocity / max_velocity
        recency_norm = 0.0
        if age_hours is not None:
            recency_norm = max(0.0, 1.0 - (age_hours / 96.0))
        score = (
            (videos_norm * 0.46)
            + (velocity_norm * 0.34)
            + (row.avg_topic_score * 0.14)
            + (recency_norm * 0.06)
        ) * 100.0

        status = "rising" if row.video_count >= 3 or velocity > 50000 else "watch"
        top_hashtags = sorted(row.hashtags)[:5]
        video_samples: List[Dict[str, Any]] = []
        for url in row.video_urls:
            posted_unix = row.video_post_times.get(url)
            video_samples.append(
                {
                    "url": url,
                    "posted_at_unix": posted_unix,
                    "posted_at": format_unix_timestamp_utc(posted_unix),
                }
            )
        video_samples.sort(
            key=lambda entry: (
                0 if entry.get("posted_at_unix") is not None else 1,
                -(entry.get("posted_at_unix") or 0),
                str(entry.get("url") or ""),
            )
        )
        video_urls = [str(entry.get("url") or "") for entry in video_samples if entry.get("url")]
        source_urls = sorted(row.source_urls)
        sample_video_url = row.example_video_url or (video_urls[0] if video_urls else None)
        sample_video_posted_unix = row.example_video_create_time
        if video_samples:
            sample_video_url = str(video_samples[0].get("url") or sample_video_url or "")
            sample_video_posted_unix = video_samples[0].get("posted_at_unix")
        sample_video_posted_at = format_unix_timestamp_utc(
            safe_int(sample_video_posted_unix) if sample_video_posted_unix is not None else None
        )
        source_page_url = row.example_source_url or (source_urls[0] if source_urls else None)
        results.append(
            {
                "song_key": key,
                "song_title": row.title,
                "song_author": row.author,
                "song_id": row.song_id,
                "status": status,
                "score": round(score, 2),
                "video_count": row.video_count,
                "recent_play_count": row.recent_play_count,
                "recent_digg_count": row.recent_digg_count,
                "velocity_views_per_hour": round(velocity, 2),
                "avg_age_hours": round(age_hours, 2) if age_hours is not None else None,
                "topic_score": round(row.avg_topic_score, 3),
                "hashtags": top_hashtags,
                "sources": sorted(row.sources),
                "sample_video_url": sample_video_url,
                "sample_video_posted_at": sample_video_posted_at,
                "sample_video_posted_at_unix": safe_int(sample_video_posted_unix)
                if sample_video_posted_unix is not None
                else None,
                "source_page_url": source_page_url,
                "video_urls": video_urls[:12],
                "video_samples": video_samples[:12],
                "source_urls": source_urls[:12],
            }
        )

    priority = {"rising": 0, "watch": 1}
    results.sort(
        key=lambda row: (
            priority.get(row["status"], 9),
            -row["score"],
            -row.get("video_count", 0),
            row["song_title"].lower(),
        )
    )
    return results


def score_creative_center_music_rows(
    rows: List[Dict[str, Any]],
    topic_terms: Set[str],
) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        author = str(row.get("author") or "").strip() or "unknown"
        if not title:
            continue
        song_key = f"{title.lower()}|{author.lower()}"
        rank = safe_int(row.get("rank"))
        trend_values = row.get("trend_values") if isinstance(row.get("trend_values"), list) else []
        trend_score = compute_creative_momentum([float(v) for v in trend_values if isinstance(v, (int, float))])
        rank_norm = 0.0
        if rank and rank > 0:
            rank_norm = max(0.0, (21 - min(rank, 21)) / 20.0)
        topic_match = 0.0
        if topic_terms:
            lowered_title = title.lower()
            lowered_author = author.lower()
            for term in topic_terms:
                if term in lowered_title:
                    topic_match = max(topic_match, 0.7)
                if term in lowered_author:
                    topic_match = max(topic_match, 0.55)
        base_topic = 0.2 if topic_terms else 0.0
        total_topic = max(base_topic, topic_match)
        score = ((trend_score * 0.5) + (rank_norm * 0.35) + (total_topic * 0.15)) * 100.0
        status = "rising" if trend_score >= 0.55 else "watch"

        scored.append(
            {
                "song_key": song_key,
                "song_title": title,
                "song_author": author,
                "song_id": row.get("song_id"),
                "status": status,
                "score": round(score, 2),
                "video_count": 0,
                "recent_play_count": 0,
                "recent_digg_count": 0,
                "velocity_views_per_hour": 0.0,
                "avg_age_hours": None,
                "topic_score": round(total_topic, 3),
                "hashtags": [],
                "sources": ["creative_center_music"],
                "sample_video_url": None,
                "sample_video_posted_at": None,
                "sample_video_posted_at_unix": None,
                "source_page_url": row.get("link"),
                "video_urls": [],
                "video_samples": [],
                "source_urls": [row.get("link")] if row.get("link") else [],
                "creative_rank": rank,
                "link": row.get("link"),
            }
        )

    priority = {"rising": 0, "watch": 1}
    scored.sort(
        key=lambda item: (
            priority.get(item.get("status"), 9),
            -float(item.get("score") or 0.0),
            safe_console_text(item.get("song_title") or ""),
        )
    )
    return scored


def parse_item_struct_from_tiktok_video_page(html: str) -> Optional[Dict[str, Any]]:
    if not html:
        return None

    # Prefer universal hydration payload when available.
    marker = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
    start = html.find(marker)
    if start != -1:
        start += len(marker)
        end = html.find("</script>", start)
        if end != -1:
            raw = html[start:end]
            try:
                payload = json.loads(raw)
                item_struct = (
                    payload.get("__DEFAULT_SCOPE__", {})
                    .get("webapp.video-detail", {})
                    .get("itemInfo", {})
                    .get("itemStruct")
                )
                if isinstance(item_struct, dict):
                    return item_struct
            except Exception:
                pass

    # Fallback for older response shape.
    marker = '<script id="SIGI_STATE" type="application/json">'
    start = html.find(marker)
    if start != -1:
        start += len(marker)
        end = html.find("</script>", start)
        if end != -1:
            raw = html[start:end]
            try:
                payload = json.loads(raw)
                item_module = payload.get("ItemModule", {})
                if isinstance(item_module, dict) and item_module:
                    first = next(iter(item_module.values()))
                    if isinstance(first, dict):
                        return first
            except Exception:
                pass
    return None


def build_item_struct_from_embed_video_data(video_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item_infos = video_data.get("itemInfos")
    music_infos = video_data.get("musicInfos")
    if not isinstance(item_infos, dict) or not isinstance(music_infos, dict):
        return None

    challenges: List[Dict[str, Any]] = []
    for row in video_data.get("challengeInfoList", []) or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("challengeName") or "").strip()
        if not title:
            continue
        challenges.append({"title": title})

    text_extra: List[Dict[str, Any]] = []
    for row in video_data.get("textExtra", []) or []:
        if not isinstance(row, dict):
            continue
        hashtag_name = row.get("HashtagName")
        if hashtag_name is None:
            hashtag_name = row.get("hashtagName")
        if hashtag_name:
            text_extra.append({"hashtagName": hashtag_name})

    return {
        "id": item_infos.get("id"),
        "desc": item_infos.get("text") or "",
        "createTime": safe_int(item_infos.get("createTime")),
        "stats": {
            "playCount": safe_int(item_infos.get("playCount")) or 0,
            "diggCount": safe_int(item_infos.get("diggCount")) or 0,
            "shareCount": safe_int(item_infos.get("shareCount")) or 0,
            "commentCount": safe_int(item_infos.get("commentCount")) or 0,
        },
        "music": {
            "id": music_infos.get("musicId"),
            "title": music_infos.get("musicName"),
            "musicName": music_infos.get("musicName"),
            "authorName": music_infos.get("authorName"),
        },
        "challenges": challenges,
        "textExtra": text_extra,
    }


def parse_item_struct_from_tiktok_embed_page(
    html: str,
    expected_item_id: str = "",
) -> Optional[Dict[str, Any]]:
    if not html:
        return None

    marker = '<script id="__FRONTITY_CONNECT_STATE__" type="application/json">'
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = html.find("</script>", start)
    if end == -1:
        return None

    try:
        payload = json.loads(html[start:end])
    except Exception:
        return None

    source = payload.get("source")
    source_data = source.get("data") if isinstance(source, dict) else None
    if not isinstance(source_data, dict):
        return None

    expected = str(expected_item_id or "").strip()
    for row in source_data.values():
        if not isinstance(row, dict):
            continue
        video_data = row.get("videoData")
        if not isinstance(video_data, dict):
            continue
        item_infos = video_data.get("itemInfos")
        if not isinstance(item_infos, dict):
            continue
        item_id = str(item_infos.get("id") or "").strip()
        if expected and item_id and item_id != expected:
            continue

        item_struct = build_item_struct_from_embed_video_data(video_data)
        if isinstance(item_struct, dict):
            return item_struct
    return None


def fetch_item_struct_by_video_id(
    session: requests.Session,
    item_id: str,
    headers: Dict[str, str],
    timeout_seconds: int = 20,
) -> Optional[Dict[str, Any]]:
    for url, parser in [
        (f"https://www.tiktok.com/@_/video/{item_id}", parse_item_struct_from_tiktok_video_page),
        (
            f"https://www.tiktok.com/embed/v2/{item_id}",
            lambda html: parse_item_struct_from_tiktok_embed_page(html, expected_item_id=item_id),
        ),
    ]:
        try:
            response = session.get(url, headers=headers, timeout=timeout_seconds)
        except Exception:
            continue
        if response.status_code != 200:
            continue
        item_struct = parser(response.text)
        if isinstance(item_struct, dict):
            return item_struct
    return None


def extract_video_id_from_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return text
    match = re.search(r"/video/(\d+)", text)
    if match:
        return match.group(1)
    return None


def fetch_creative_center_hashtag_detail(
    session: requests.Session,
    locale: str,
    hashtag: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    cleaned = clean_hashtag(hashtag)
    if not cleaned:
        return None, "Topic hashtag detail lookup skipped: invalid hashtag."

    url = f"https://ads.tiktok.com/business/creativecenter/hashtag/{cleaned}/pc/{locale}"
    headers = {"User-Agent": USER_AGENT}
    try:
        response = session.get(url, headers=headers, timeout=25)
    except Exception as exc:
        return None, f"Creative Center hashtag detail request failed for #{cleaned}: {exc}"

    if response.status_code != 200:
        return None, (
            f"Creative Center hashtag detail request for #{cleaned} "
            f"returned status {response.status_code}"
        )

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    if not match:
        return None, f"Creative Center hashtag detail page for #{cleaned} is missing __NEXT_DATA__."

    try:
        payload = json.loads(match.group(1))
    except Exception as exc:
        return None, f"Creative Center hashtag detail payload parse failed for #{cleaned}: {exc}"

    page_props = payload.get("props", {}).get("pageProps", {})
    data = page_props.get("data") if isinstance(page_props, dict) else None
    if not isinstance(data, dict) or not data:
        return None, f"Creative Center hashtag detail data missing for #{cleaned}."
    return data, None


def extract_related_item_ids_from_hashtag_detail(data: Dict[str, Any]) -> List[str]:
    ordered_ids: List[str] = []
    seen: Set[str] = set()

    def add_item_id(raw: Any) -> None:
        item_id = extract_video_id_from_url(raw)
        if not item_id:
            return
        if item_id in seen:
            return
        seen.add(item_id)
        ordered_ids.append(item_id)

    for row in data.get("relatedItems", []) or []:
        if not isinstance(row, dict):
            continue
        add_item_id(row.get("itemId"))
        add_item_id(row.get("id"))
        add_item_id(row.get("videoId"))
        add_item_id(row.get("awemeId"))
        add_item_id(row.get("videoUrl"))
        add_item_id(row.get("url"))

    for row in data.get("recList", []) or []:
        if not isinstance(row, dict):
            continue
        add_item_id(row.get("itemId"))
        add_item_id(row.get("id"))
        add_item_id(row.get("videoId"))
        add_item_id(row.get("awemeId"))
        add_item_id(row.get("videoUrl"))
        add_item_id(row.get("url"))

    add_item_id(data.get("videoUrl"))
    return ordered_ids


def rank_topic_hashtag_candidates(
    topic_terms: Set[str],
    candidate_terms: List[str],
    limit: int,
) -> List[str]:
    if not topic_terms:
        return []

    generic_tags = {
        "dance",
        "dancing",
        "dancer",
        "challenge",
        "challenges",
        "music",
        "song",
        "songs",
    }
    prefer_challenge = any("challenge" in term for term in topic_terms)
    pool: Set[str] = set()
    for tag in candidate_terms:
        cleaned = clean_hashtag(tag)
        if cleaned:
            pool.add(cleaned)
    for term in topic_terms:
        cleaned = clean_hashtag(term)
        if cleaned:
            pool.add(cleaned)

    ranked: List[str] = []
    for tag in pool:
        if is_noise_hashtag(tag):
            continue
        relevance = topic_relevance_score(tag, topic_terms)
        if relevance < 0.25:
            continue
        ranked.append(tag)

    ranked.sort(
        key=lambda tag: (
            -(1 if tag in topic_terms else 0),
            -(1 if prefer_challenge and "challenge" in tag else 0),
            1 if tag in generic_tags else 0,
            -topic_relevance_score(tag, topic_terms),
            -len(tag),
            tag,
        )
    )
    return ranked[: max(1, limit)]


def collect_song_signals_from_discover_video_urls(
    session: requests.Session,
    discover_url: str,
    video_urls: List[str],
    topic_terms: Set[str],
    request_delay: float,
    max_video_age_hours: Optional[float] = None,
) -> Tuple[Dict[str, SongSignal], List[str], List[str]]:
    song_signals: Dict[str, SongSignal] = {}
    candidate_tags: Set[str] = set()
    notes: List[str] = []
    if not topic_terms or not video_urls:
        return song_signals, [], notes

    headers = {"User-Agent": USER_AGENT}
    now_ts = time.time()
    parsed_count = 0
    matched_count = 0
    age_filtered_count = 0
    progress_log(
        f"Discover song extraction starting: source={discover_url} links={len(video_urls)}",
        force=True,
    )

    for index, video_url in enumerate(video_urls, start=1):
        if index == 1 or index % 20 == 0:
            progress_log(
                f"Discover song extraction progress: {index}/{len(video_urls)} from {discover_url}",
                key=f"discover-song-scan-{discover_url}",
            )
        item_id = extract_video_id_from_url(video_url)
        if not item_id:
            continue
        item_struct = fetch_item_struct_by_video_id(
            session=session,
            item_id=item_id,
            headers=headers,
            timeout_seconds=20,
        )
        if not isinstance(item_struct, dict):
            continue
        parsed_count += 1

        stats = item_struct.get("stats", {}) if isinstance(item_struct.get("stats"), dict) else {}
        play_count = safe_int(stats.get("playCount")) or 0
        digg_count = safe_int(stats.get("diggCount")) or 0
        create_time = safe_int(item_struct.get("createTime"))
        if is_video_too_old(
            create_time=create_time,
            now_ts=now_ts,
            max_video_age_hours=max_video_age_hours,
        ):
            age_filtered_count += 1
            continue
        age_hours = compute_video_age_hours(create_time=create_time, now_ts=now_ts)

        video_hashtags = extract_hashtags_from_video(item_struct)
        for tag in video_hashtags:
            if topic_relevance_score(tag, topic_terms) >= 0.22:
                candidate_tags.add(tag)

        added = add_song_signal_from_video(
            song_signals=song_signals,
            video=item_struct,
            topic_terms=topic_terms,
            play_count=play_count,
            digg_count=digg_count,
            age_hours=age_hours,
            hashtags=video_hashtags,
            source="discover_trending_dances",
            challenge_context_override=True,
            min_topic_score_override=0.2,
            video_url=video_url,
            video_create_time=create_time,
            source_page_url=discover_url,
        )
        if added:
            matched_count += 1
        if request_delay > 0:
            time.sleep(request_delay)

    notes.append(
        f"Discover dances source matched songs in {matched_count}/{parsed_count} parsed videos "
        f"(from {len(video_urls)} collected links)."
    )
    if age_filtered_count > 0:
        notes.append(
            f"Discover dances source skipped {age_filtered_count} videos older than the configured age limit."
        )
    progress_log(
        f"Discover song extraction complete: source={discover_url} parsed={parsed_count} matched={matched_count}",
        force=True,
    )
    return song_signals, sorted(candidate_tags), notes


def collect_song_signals_from_topic_hashtag_related_videos(
    session: requests.Session,
    locale: str,
    topic_terms: Set[str],
    candidate_terms: List[str],
    max_hashtag_pages: int,
    max_related_per_hashtag: int,
    max_total_related_videos: int,
    request_delay: float,
    max_video_age_hours: Optional[float] = None,
) -> Tuple[Dict[str, SongSignal], List[str]]:
    song_signals: Dict[str, SongSignal] = {}
    notes: List[str] = []
    if not topic_terms:
        return song_signals, notes

    target_hashtags = rank_topic_hashtag_candidates(
        topic_terms=topic_terms,
        candidate_terms=candidate_terms,
        limit=max(1, max_hashtag_pages) * 3,
    )
    if not target_hashtags:
        notes.append("No topic hashtags were eligible for Creative Center hashtag detail lookup.")
        return song_signals, notes
    progress_log(
        "Topic hashtag related-video scan starting: "
        f"candidate_hashtags={len(target_hashtags)} max_pages={max(1, max_hashtag_pages)}",
        force=True,
    )

    headers = {"User-Agent": USER_AGENT}
    now_ts = time.time()
    hashtag_pages_attempted = 0
    hashtag_pages_readable = 0
    detail_errors = 0
    related_attempts = 0
    related_matches = 0
    age_filtered_count = 0
    seen_item_ids: Set[str] = set()
    related_cap = max(1, max_total_related_videos)

    max_attempted_pages = max(1, max_hashtag_pages) * 3
    queue: List[str] = list(target_hashtags)
    queued: Set[str] = set(queue)
    queue_index = 0
    while queue_index < len(queue):
        progress_log(
            "Topic hashtag scan checkpoint: "
            f"queue={queue_index}/{len(queue)} attempted={hashtag_pages_attempted} "
            f"readable={hashtag_pages_readable} related={related_attempts} matches={related_matches}",
            key="topic-hashtag-related-loop",
        )
        hashtag = queue[queue_index]
        queue_index += 1
        if hashtag_pages_attempted >= max_attempted_pages:
            break
        if hashtag_pages_readable >= max(1, max_hashtag_pages):
            break
        if related_attempts >= related_cap:
            break
        hashtag_pages_attempted += 1
        detail, detail_error = fetch_creative_center_hashtag_detail(
            session=session,
            locale=locale,
            hashtag=hashtag,
        )
        if detail_error or not isinstance(detail, dict):
            detail_errors += 1
            continue
        hashtag_pages_readable += 1
        item_ids = extract_related_item_ids_from_hashtag_detail(detail)
        related_hashtags = detail.get("relatedHashtags", []) if isinstance(detail, dict) else []
        if isinstance(related_hashtags, list):
            for rel in related_hashtags:
                if not isinstance(rel, dict):
                    continue
                rel_tag = clean_hashtag(rel.get("hashtagName") or rel.get("challengeName"))
                if not rel_tag or rel_tag in queued:
                    continue
                if topic_relevance_score(rel_tag, topic_terms) < 0.22:
                    continue
                queued.add(rel_tag)
                if len(queue) < max_attempted_pages:
                    queue.append(rel_tag)
        if not item_ids:
            continue

        hashtag_page_url = (
            f"https://ads.tiktok.com/business/creativecenter/hashtag/{hashtag}/pc/{locale}"
        )
        challenge_hint = "challenge" in hashtag
        for item_id in item_ids[: max(0, max_related_per_hashtag)]:
            if not item_id:
                continue
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            if related_attempts >= related_cap:
                break
            related_attempts += 1
            item_struct = fetch_item_struct_by_video_id(
                session=session,
                item_id=item_id,
                headers=headers,
                timeout_seconds=20,
            )
            if not isinstance(item_struct, dict):
                continue

            stats = item_struct.get("stats", {}) if isinstance(item_struct.get("stats"), dict) else {}
            play_count = safe_int(stats.get("playCount")) or 0
            digg_count = safe_int(stats.get("diggCount")) or 0
            create_time = safe_int(item_struct.get("createTime"))
            if is_video_too_old(
                create_time=create_time,
                now_ts=now_ts,
                max_video_age_hours=max_video_age_hours,
            ):
                age_filtered_count += 1
                continue
            age_hours = compute_video_age_hours(create_time=create_time, now_ts=now_ts)

            video_hashtags = extract_hashtags_from_video(item_struct)
            added = add_song_signal_from_video(
                song_signals=song_signals,
                video=item_struct,
                topic_terms=topic_terms,
                play_count=play_count,
                digg_count=digg_count,
                age_hours=age_hours,
                hashtags=video_hashtags,
                source="creative_center_topic_hashtag_video",
                challenge_context_override=challenge_hint,
                min_topic_score_override=0.18 if challenge_hint else 0.28,
                video_url=f"https://www.tiktok.com/@_/video/{item_id}",
                video_create_time=create_time,
                source_page_url=hashtag_page_url,
            )
            if added:
                related_matches += 1
            if request_delay > 0:
                time.sleep(request_delay)

    notes.append(
        "Topic hashtag related-video scan matched "
        f"{related_matches}/{related_attempts} videos across "
        f"{hashtag_pages_readable}/{hashtag_pages_attempted} hashtag pages."
    )
    if detail_errors > 0:
        notes.append(
            f"{detail_errors}/{hashtag_pages_attempted} Creative Center topic hashtag pages were not readable."
        )
    if age_filtered_count > 0:
        notes.append(
            "Topic hashtag related-video scan skipped "
            f"{age_filtered_count} videos older than the configured age limit."
        )
    progress_log(
        "Topic hashtag related-video scan complete: "
        f"matched={related_matches}/{related_attempts} "
        f"readable_pages={hashtag_pages_readable}/{hashtag_pages_attempted}",
        force=True,
    )
    return song_signals, notes


def collect_song_signals_from_creative_music_related_videos(
    session: requests.Session,
    music_rows: List[Dict[str, Any]],
    topic_terms: Set[str],
    max_related_per_song: int,
    request_delay: float,
    max_video_age_hours: Optional[float] = None,
) -> Tuple[Dict[str, SongSignal], List[str]]:
    song_signals: Dict[str, SongSignal] = {}
    notes: List[str] = []
    if not topic_terms:
        return song_signals, notes

    headers = {"User-Agent": USER_AGENT}
    related_attempts = 0
    related_matches = 0
    age_filtered_count = 0
    now_ts = time.time()

    for row in music_rows:
        item_ids = row.get("related_item_ids") if isinstance(row.get("related_item_ids"), list) else []
        if not item_ids:
            continue
        for item_id in item_ids[: max(0, max_related_per_song)]:
            if not item_id:
                continue
            related_attempts += 1
            item_struct = fetch_item_struct_by_video_id(
                session=session,
                item_id=item_id,
                headers=headers,
                timeout_seconds=20,
            )
            if not isinstance(item_struct, dict):
                continue

            stats = item_struct.get("stats", {}) if isinstance(item_struct.get("stats"), dict) else {}
            play_count = safe_int(stats.get("playCount")) or 0
            digg_count = safe_int(stats.get("diggCount")) or 0
            create_time = safe_int(item_struct.get("createTime"))
            if is_video_too_old(
                create_time=create_time,
                now_ts=now_ts,
                max_video_age_hours=max_video_age_hours,
            ):
                age_filtered_count += 1
                continue
            age_hours = compute_video_age_hours(create_time=create_time, now_ts=now_ts)
            video_hashtags = extract_hashtags_from_video(item_struct)
            added = add_song_signal_from_video(
                song_signals=song_signals,
                video=item_struct,
                topic_terms=topic_terms,
                play_count=play_count,
                digg_count=digg_count,
                age_hours=age_hours,
                hashtags=video_hashtags,
                source="creative_center_music_related_video",
                min_topic_score_override=0.3,
                video_url=f"https://www.tiktok.com/@_/video/{item_id}",
                video_create_time=create_time,
                source_page_url=str(row.get("link") or ""),
            )
            if added:
                related_matches += 1
            if request_delay > 0:
                time.sleep(request_delay)

    if related_attempts > 0:
        notes.append(
            f"Creative Center related-video scan matched {related_matches}/{related_attempts} videos to topic terms."
        )
    if age_filtered_count > 0:
        notes.append(
            "Creative Center related-video scan skipped "
            f"{age_filtered_count} videos older than the configured age limit."
        )
    return song_signals, notes


def print_song_report(song_rows: List[Dict[str, Any]], top_n: int, min_score: float) -> int:
    selected = [row for row in song_rows if (row.get("score") or 0.0) >= min_score][: max(1, top_n)]
    if not selected and song_rows:
        selected = song_rows[: max(1, top_n)]

    print()
    print("=" * 104)
    print("Topic Songs (From Matched Videos)")
    print("=" * 104)
    if not selected:
        print("No topic-matched songs could be extracted from accessible videos in this run.")
        return 0

    print(
        f"{'#':>2} {'song':<28} {'artist':<16} {'state':<7} {'score':>6} {'vids':>4} "
        f"{'vph':>9} {'plays':>9} {'posted':<10} {'tags':<13} {'link':<31}"
    )
    for index, row in enumerate(selected, start=1):
        tags = ",".join((row.get("hashtags") or [])[:3])
        if len(tags) > 13:
            tags = tags[:10] + "..."
        song_text = safe_console_text(row.get("song_title") or "-")
        artist_text = safe_console_text(row.get("song_author") or "-")
        if len(song_text) > 28:
            song_text = song_text[:25] + "..."
        if len(artist_text) > 16:
            artist_text = artist_text[:13] + "..."
        posted_at = str(row.get("sample_video_posted_at") or "").strip()
        posted_date = posted_at[:10] if posted_at else "-"
        link = (
            str(
                row.get("sample_video_url")
                or row.get("source_page_url")
                or row.get("link")
                or "-"
            ).strip()
        )
        short_link = link
        if len(short_link) > 31:
            short_link = short_link[:28] + "..."
        print(
            f"{index:>2} "
            f"{song_text: <28} "
            f"{artist_text: <16} "
            f"{row.get('status', '-'): <7} "
            f"{(row.get('score') or 0.0):>6.1f} "
            f"{(row.get('video_count') or 0):>4} "
            f"{human_number(safe_int(row.get('velocity_views_per_hour'))):>9} "
            f"{human_number(safe_int(row.get('recent_play_count'))):>9} "
            f"{safe_console_text(posted_date):<10} "
            f"{safe_console_text(tags):<13} "
            f"{safe_console_text(short_link):<31}"
        )
    print()
    print("Song links:")
    for index, row in enumerate(selected, start=1):
        link = (
            str(
                row.get("sample_video_url")
                or row.get("source_page_url")
                or row.get("link")
                or "-"
            ).strip()
        )
        posted_at = str(row.get("sample_video_posted_at") or "").strip() or "-"
        print(f"{index:>2}. {safe_console_text(link)} | posted: {safe_console_text(posted_at)}")
    return len(selected)


def print_report(results: List[Dict[str, Any]], top_n: int, min_score: float) -> int:
    filtered = [row for row in results if (row.get("score") or 0.0) >= min_score]
    selected = filtered[: max(1, top_n)]
    fallback_used = False
    if not selected and results:
        selected = results[: max(1, top_n)]
        fallback_used = True

    print()
    print("=" * 104)
    print("TikTok Early Trend Radar (public data only)")
    print("=" * 104)
    if not selected:
        if results:
            print("No hashtags met the score threshold. Try lowering --min-score or increasing --videos.")
        else:
            print("No hashtags matched the current filters for this run window.")
        return 0

    print(
        f"{'#':>2} {'hashtag':<26} {'state':<7} {'score':>6} {'vids':>4} "
        f"{'vph':>9} {'global':>10} {'cc#':>4} {'dV':>4} {'dR':>4} {'sources':<24}"
    )
    for index, row in enumerate(selected, start=1):
        sources = ",".join(row.get("sources") or [])
        if len(sources) > 24:
            sources = sources[:21] + "..."
        delta_mentions = row.get("delta_mentions")
        delta_rank = row.get("delta_rank")
        print(
            f"{index:>2} "
            f"{safe_console_text('#' + row['hashtag']):<26} "
            f"{row['status']:<7} "
            f"{row['score']:>6.1f} "
            f"{(row.get('from_videos') or 0):>4} "
            f"{human_number(safe_int(row.get('velocity_views_per_hour'))):>9} "
            f"{human_number(row.get('global_view_count')):>10} "
            f"{(row.get('creative_rank') or '-'):>4} "
            f"{(delta_mentions if delta_mentions is not None else '-'):>4} "
            f"{(delta_rank if delta_rank is not None else '-'):>4} "
            f"{sources:<24}"
        )

    print()
    print("Legend: vph=views/hour from trending-feed samples, dV=change in sampled videos vs previous run, dR=rank gain.")
    if fallback_used:
        print(
            "Note: No rows met --min-score, so top rows are shown anyway for visibility."
        )
    return len(selected)


def parse_args(default_history: Path, default_output: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find early-rising TikTok hashtags using publicly available data."
    )
    parser.add_argument(
        "--videos",
        type=int,
        default=120,
        help="How many public TikTok trending videos to sample (TikTokApi source).",
    )
    parser.add_argument(
        "--max-lookups",
        type=int,
        default=35,
        help="How many top hashtags to enrich with public hashtag stats.",
    )
    parser.add_argument(
        "--lookup-delay",
        type=float,
        default=0.2,
        help="Delay (seconds) between hashtag stat lookups.",
    )
    parser.add_argument(
        "--browser",
        choices=["webkit", "firefox", "chromium"],
        default="webkit",
        help="Browser backend for TikTokApi signed requests.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Prefer headed browser mode (default behavior).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Force headless browser mode.",
    )
    parser.add_argument(
        "--api-max-attempts",
        type=int,
        default=5,
        help="How many browser/session strategies to try for TikTokApi before giving up.",
    )
    parser.add_argument(
        "--api-navigation-timeout-ms",
        type=int,
        default=DEFAULT_TIKTOKAPI_NAV_TIMEOUT_MS,
        help=(
            "Playwright navigation timeout in ms for TikTokApi session startup "
            "(default: 10000). Lower values fail faster when blocked."
        ),
    )
    parser.add_argument(
        "--cookies-file",
        default="",
        help="Optional Netscape-format cookies file (defaults to repo cookies.txt if present).",
    )
    parser.add_argument(
        "--headful-wait-seconds",
        type=float,
        default=3.0,
        help="Delay in seconds after headed browser startup before scraping.",
    )
    parser.add_argument(
        "--xvfb-display",
        default=":99",
        help="Xvfb DISPLAY value used when headed mode runs on Linux without DISPLAY.",
    )
    parser.add_argument(
        "--proxy-url",
        default="",
        help="Optional proxy URL for TikTokApi, e.g. http://user:pass@host:port.",
    )
    parser.add_argument(
        "--proxy-username",
        default="",
        help="Optional proxy username (overrides username from --proxy-url).",
    )
    parser.add_argument(
        "--proxy-password",
        default="",
        help="Optional proxy password (overrides password from --proxy-url).",
    )
    parser.add_argument(
        "--topic",
        default="",
        help="Optional topic query, e.g. 'dance challenges'.",
    )
    parser.add_argument(
        "--topic-terms",
        default="",
        help="Optional comma-separated extra topic terms, e.g. 'kpopdance,choreography'.",
    )
    parser.add_argument(
        "--topic-min-relevance",
        type=float,
        default=0.35,
        help="Minimum topic relevance for strict topic filtering.",
    )
    parser.add_argument(
        "--topic-loose",
        action="store_true",
        help="Do not strictly filter by topic; just prioritize topic-relevant hashtags.",
    )
    parser.add_argument(
        "--topic-seed-limit",
        type=int,
        default=25,
        help="Max topic seed hashtags to probe via public hashtag stats lookup.",
    )
    parser.add_argument(
        "--topic-video-samples",
        type=int,
        default=5,
        help="How many videos to sample per topic seed hashtag when extracting songs.",
    )
    parser.add_argument(
        "--max-video-age-days",
        type=float,
        default=365.0,
        help=(
            "Exclude videos older than this many days when extracting signals/songs. "
            "Set to 0 to disable age filtering."
        ),
    )
    parser.add_argument(
        "--topic-candidate-limit",
        type=int,
        default=120,
        help="Max public web topic candidates to consider before seed lookups.",
    )
    parser.add_argument(
        "--discover-dances-url",
        default="",
        help="Optional single Discover page URL to append (backward-compatible flag).",
    )
    parser.add_argument(
        "--discover-dances-urls",
        default=DEFAULT_DISCOVER_DANCES_URLS,
        help=(
            "Discover page URLs to scrape (comma-separated). "
            "Defaults include general + K-pop dance pages."
        ),
    )
    parser.add_argument(
        "--discover-dances-videos",
        type=int,
        default=180,
        help="Max video links to collect from the Discover dances source.",
    )
    parser.add_argument(
        "--discover-scroll-rounds",
        type=int,
        default=12,
        help="How many downward scroll rounds to perform on the Discover page.",
    )
    parser.add_argument(
        "--no-discover-dances",
        action="store_true",
        help="Disable Discover dances page scraping source.",
    )
    parser.add_argument(
        "--allow-generic-topic-tags",
        action="store_true",
        help="Include broad anchor tags (e.g. #dance, #challenge) in topic mode output.",
    )
    parser.add_argument(
        "--max-topic-global-views",
        type=int,
        default=50000000000,
        help="Hide ultra-broad topic tags above this lifetime view threshold in strict topic mode.",
    )
    parser.add_argument(
        "--allow-stable-topic-fallback",
        action="store_true",
        help="If no emerging topic hashtags are found, still show stable/watch topic matches.",
    )
    parser.add_argument(
        "--force-topic-seed-when-api-blocked",
        action="store_true",
        help=(
            "Still run TikTokApi topic-seed lookups even when trend-feed startup appears blocked. "
            "Default behavior skips seed retries in this case to avoid long stalls."
        ),
    )
    parser.add_argument(
        "--locale",
        default="en",
        help="Creative Center locale path, e.g. en, fr, es.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Number of hashtag rows to print.",
    )
    parser.add_argument(
        "--show-hashtag-report",
        action="store_true",
        help="Show the hashtag radar table in topic mode (hidden by default).",
    )
    parser.add_argument(
        "--song-top",
        type=int,
        default=20,
        help="Number of song rows to print in topic mode.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=20.0,
        help="Minimum score required for printed rows.",
    )
    parser.add_argument(
        "--song-min-score",
        type=float,
        default=15.0,
        help="Minimum score required for printed song rows in topic mode.",
    )
    parser.add_argument(
        "--topic-hashtag-pages",
        type=int,
        default=24,
        help="How many topic hashtag detail pages to inspect on Creative Center for related videos.",
    )
    parser.add_argument(
        "--topic-hashtag-video-samples",
        type=int,
        default=20,
        help="How many related videos per topic hashtag page to inspect for song extraction.",
    )
    parser.add_argument(
        "--topic-max-related-videos",
        type=int,
        default=400,
        help="Global cap on total related videos to inspect across topic hashtag pages.",
    )
    parser.add_argument(
        "--music-related-video-samples",
        type=int,
        default=3,
        help="How many related videos per Creative Center song to inspect for fallback topic relevance.",
    )
    parser.add_argument(
        "--allow-broad-song-fallback",
        action="store_true",
        help="Allow broad Creative Center top music fallback if no topic-linked song videos can be read.",
    )
    parser.add_argument(
        "--history-file",
        default=str(default_history),
        help="JSON history file path (used only with --save-local-files).",
    )
    parser.add_argument(
        "--output-json",
        default=str(default_output),
        help="Output report JSON path (used only with --save-local-files).",
    )
    parser.add_argument(
        "--save-local-files",
        action="store_true",
        help="Save local report/history JSON files (disabled by default).",
    )
    parser.add_argument(
        "--no-supabase-upload",
        action="store_true",
        help="Disable uploading topic song rows to Supabase.",
    )
    parser.add_argument(
        "--supabase-url",
        default="",
        help="Optional Supabase project URL override (defaults to SUPABASE_URL/app.env).",
    )
    parser.add_argument(
        "--supabase-project-id",
        default="",
        help="Optional Supabase project id override (used if URL is omitted).",
    )
    parser.add_argument(
        "--supabase-key",
        default="",
        help="Optional Supabase service key override (defaults to SUPABASE_SECRET_KEY/app.env).",
    )
    parser.add_argument(
        "--supabase-table",
        default="topic_trends",
        help="Supabase table name for topic song upserts.",
    )
    parser.add_argument(
        "--supabase-on-conflict",
        default="song,artist",
        help="on_conflict target for Supabase upsert (default: song,artist).",
    )
    parser.add_argument(
        "--supabase-timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for each Supabase upsert request.",
    )
    parser.add_argument(
        "--supabase-min-velocity",
        type=float,
        default=500,
        help="Only upload topic song rows with velocity_views_per_hour strictly greater than this value.",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip TikTokApi trending feed source and use Creative Center only.",
    )
    parser.add_argument(
        "--no-creative-center",
        action="store_true",
        help="Skip Creative Center source and use TikTokApi only.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce progress logging output.",
    )
    parser.add_argument(
        "--progress-interval-seconds",
        type=float,
        default=20.0,
        help="Minimum interval for repeated progress checkpoints.",
    )
    return parser.parse_args()


def main() -> None:
    configure_stdout_utf8()

    script_path = Path(__file__).resolve()
    repo_root = script_path.parent.parent
    default_data_dir = repo_root / "testing"
    default_history = default_data_dir / "viral_trend_history.json"
    default_output = default_data_dir / "viral_trend_report.json"
    args = parse_args(default_history=default_history, default_output=default_output)
    set_progress_logging(enabled=not args.quiet, interval_seconds=args.progress_interval_seconds)
    progress_log("find_viral_trends run starting", force=True)
    progress_log(f"Script path: {script_path}", force=True)
    progress_log(f"Repo root: {repo_root}", force=True)

    save_local_files = bool(args.save_local_files)
    topic_slug = slugify_topic(args.topic)
    history_file = Path(args.history_file)
    output_json = Path(args.output_json)
    if save_local_files and topic_slug:
        if args.history_file == str(default_history):
            history_file = script_path.parent / f"viral_trend_history_{topic_slug}.json"
        if args.output_json == str(default_output):
            output_json = script_path.parent / f"viral_trend_report_{topic_slug}.json"
    timestamp_iso = now_iso()
    cookie_path = Path(args.cookies_file) if args.cookies_file else (repo_root / "cookies.txt")
    cookie_dict = load_netscape_cookie_file(cookie_path) if cookie_path.exists() else {}
    topic_terms = build_topic_terms(args.topic, args.topic_terms)
    max_video_age_hours = resolve_max_video_age_hours(args.max_video_age_days)
    requested_headless = bool(args.headless and not args.headful)
    effective_headless = requested_headless
    effective_headful_wait_seconds = max(0.0, args.headful_wait_seconds)
    proxy_config = build_playwright_proxy(
        proxy_url=args.proxy_url,
        proxy_username=args.proxy_username,
        proxy_password=args.proxy_password,
    )
    api_navigation_timeout_ms = max(1, args.api_navigation_timeout_ms)
    progress_log(
        "Runtime config: "
        f"topic_terms={len(topic_terms)} "
        f"browser={args.browser} "
        f"api_attempts={max(1, args.api_max_attempts)} "
        f"videos={max(1, args.videos)}",
        force=True,
    )
    progress_log(
        f"Cookie file {'found' if cookie_path.exists() else 'missing'}: {cookie_path}",
        force=True,
    )
    if args.proxy_url and proxy_config is None:
        print("Invalid --proxy-url format. Expected host:port or scheme://host:port")
        sys.exit(2)

    combined_signals: Dict[str, HashtagSignal] = {}
    combined_song_signals: Dict[str, SongSignal] = {}
    source_errors: List[str] = []
    source_notes: List[str] = []
    source_status: Dict[str, bool] = {
        "creative_center": False,
        "tiktok_api": False,
        "discover_dances": False,
    }
    if max_video_age_hours is not None:
        source_notes.append(
            f"Video age filter active: ignoring videos older than {max_video_age_hours / 24.0:.1f} days."
        )
    effective_headless, display_note = ensure_headed_display_with_xvfb(
        requested_headless=effective_headless,
        xvfb_display=args.xvfb_display,
    )
    if effective_headless:
        effective_headful_wait_seconds = 0.0
    if display_note:
        source_notes.append(display_note)
    source_notes.append(
        f"Browser mode: {'headless' if effective_headless else 'headed'}."
    )
    source_notes.append(f"TikTokApi navigation timeout: {api_navigation_timeout_ms}ms.")
    progress_log(
        f"Browser mode resolved to {'headless' if effective_headless else 'headed'}; "
        f"timeout={api_navigation_timeout_ms}ms",
        force=True,
    )

    session = requests.Session()
    topic_candidates: List[str] = []
    discover_source_urls = parse_discover_source_urls(
        args.discover_dances_urls,
        args.discover_dances_url,
    )
    discover_video_url_count = 0
    if topic_terms:
        progress_log("Collecting topic web hashtag candidates...", force=True)
        topic_candidate_started = time.monotonic()
        topic_candidates, candidate_errors = fetch_topic_web_candidates(
            session=session,
            topic_terms=topic_terms,
            candidate_limit=max(1, args.topic_candidate_limit),
        )
        progress_log(
            f"Topic web candidates complete: {len(topic_candidates)} candidates in "
            f"{time.monotonic() - topic_candidate_started:.1f}s",
            force=True,
        )
        for msg in candidate_errors:
            source_notes.append(msg)

        if not args.no_discover_dances and discover_source_urls:
            progress_log(
                f"Discover source scan starting for {len(discover_source_urls)} URL(s)",
                force=True,
            )
            seen_discover_video_urls: Set[str] = set()
            for discover_source_url in discover_source_urls:
                progress_log(
                    f"Discover scrape start: {discover_source_url}",
                    force=True,
                )
                discover_started = time.monotonic()
                discover_video_urls, discover_error = asyncio.run(
                    fetch_discover_video_urls(
                        repo_root=repo_root,
                        discover_url=discover_source_url,
                        browser=args.browser,
                        headless=effective_headless,
                        max_attempts=max(1, args.api_max_attempts),
                        max_urls=max(1, args.discover_dances_videos),
                        scroll_rounds=max(0, args.discover_scroll_rounds),
                        headful_wait_seconds=effective_headful_wait_seconds,
                        proxy=proxy_config,
                    )
                )
                if discover_error:
                    source_notes.append(f"Discover source {discover_source_url} failed: {discover_error}")
                    progress_log(
                        f"Discover scrape failed in {time.monotonic() - discover_started:.1f}s: "
                        f"{discover_source_url} -> {discover_error}",
                        force=True,
                    )
                    continue
                if not discover_video_urls:
                    progress_log(
                        f"Discover scrape returned zero links in {time.monotonic() - discover_started:.1f}s: "
                        f"{discover_source_url}",
                        force=True,
                    )
                    continue

                unique_video_urls: List[str] = []
                for video_url in discover_video_urls:
                    if video_url in seen_discover_video_urls:
                        continue
                    seen_discover_video_urls.add(video_url)
                    unique_video_urls.append(video_url)

                if not unique_video_urls:
                    source_notes.append(
                        f"Discover source {discover_source_url} returned only duplicate links."
                    )
                    progress_log(
                        f"Discover links all duplicates in {time.monotonic() - discover_started:.1f}s: "
                        f"{discover_source_url}",
                        force=True,
                    )
                    continue

                discover_video_url_count += len(unique_video_urls)
                progress_log(
                    f"Discover scrape ok in {time.monotonic() - discover_started:.1f}s: "
                    f"{discover_source_url} -> raw={len(discover_video_urls)} unique={len(unique_video_urls)}",
                    force=True,
                )
                source_status["discover_dances"] = True
                discover_song_started = time.monotonic()
                discover_song_signals, discover_candidate_tags, discover_notes = (
                    collect_song_signals_from_discover_video_urls(
                        session=session,
                        discover_url=discover_source_url,
                        video_urls=unique_video_urls,
                        topic_terms=topic_terms,
                        request_delay=max(0.0, args.lookup_delay),
                        max_video_age_hours=max_video_age_hours,
                    )
                )
                for msg in discover_notes:
                    source_notes.append(msg)
                if discover_song_signals:
                    before_count = len(combined_song_signals)
                    merge_song_signals(combined_song_signals, discover_song_signals)
                    added_count = max(0, len(combined_song_signals) - before_count)
                    source_notes.append(
                        "Discover dances source contributed song candidates."
                        + (f" Added {added_count} songs." if added_count > 0 else "")
                    )
                progress_log(
                    "Discover song extraction complete: "
                    f"{discover_source_url} -> songs={len(discover_song_signals)} "
                    f"candidate_tags={len(discover_candidate_tags)} "
                    f"in {time.monotonic() - discover_song_started:.1f}s",
                    force=True,
                )
                if discover_candidate_tags:
                    added_tags = 0
                    existing = set(topic_candidates)
                    for tag in discover_candidate_tags:
                        if tag in existing:
                            continue
                        topic_candidates.append(tag)
                        existing.add(tag)
                        added_tags += 1
                    if added_tags > 0:
                        source_notes.append(
                            f"Discover dances source added {added_tags} topic hashtag candidates."
                        )

    if not args.no_creative_center:
        progress_log("Fetching Creative Center hashtag signals...", force=True)
        creative_started = time.monotonic()
        creative_signals, creative_error = fetch_creative_center_signals(session=session, locale=args.locale)
        if creative_error:
            source_errors.append(creative_error)
        if creative_signals:
            source_status["creative_center"] = True
            merge_signals(combined_signals, creative_signals)
        progress_log(
            f"Creative Center hashtag signals complete: {len(creative_signals)} hashtags "
            f"in {time.monotonic() - creative_started:.1f}s",
            force=True,
        )

    api_blocked = False
    if not args.no_api:
        progress_log("Starting TikTokApi trend feed scan...", force=True)
        api_started = time.monotonic()
        api_signals, api_song_signals, api_error = asyncio.run(
            fetch_tiktokapi_signals(
                repo_root=repo_root,
                video_count=max(1, args.videos),
                browser=args.browser,
                headless=effective_headless,
                max_lookups=max(0, args.max_lookups),
                lookup_delay=max(0.0, args.lookup_delay),
                api_navigation_timeout_ms=api_navigation_timeout_ms,
                max_attempts=max(1, args.api_max_attempts),
                cookies=cookie_dict or None,
                headful_wait_seconds=effective_headful_wait_seconds,
                proxy=proxy_config,
                topic_terms=topic_terms if topic_terms else None,
                max_video_age_hours=max_video_age_hours,
            )
        )
        if api_error:
            source_errors.append(api_error)
            if is_tiktokapi_session_block_error(api_error):
                api_blocked = True
        if api_signals:
            source_status["tiktok_api"] = True
            merge_signals(combined_signals, api_signals)
        if api_song_signals:
            source_status["tiktok_api"] = True
            merge_song_signals(combined_song_signals, api_song_signals)
        progress_log(
            "TikTokApi trend feed scan complete: "
            f"hashtags={len(api_signals)} songs={len(api_song_signals)} "
            f"elapsed={time.monotonic() - api_started:.1f}s",
            force=True,
        )

        if topic_terms:
            skip_topic_seed_due_to_api_block = api_blocked and not args.force_topic_seed_when_api_blocked
            if skip_topic_seed_due_to_api_block:
                note = (
                    "Skipping TikTokApi topic seed scan because trend-feed session bootstrap appears blocked; "
                    "relying on Discover/Creative Center topic sources."
                )
                source_notes.append(note)
                progress_log(note, force=True)
            else:
                progress_log("Starting TikTokApi topic seed scan...", force=True)
                seed_started = time.monotonic()
                seed_signals, seed_song_signals, seed_error = asyncio.run(
                    fetch_topic_seed_signals(
                        repo_root=repo_root,
                        topic_terms=topic_terms,
                        candidate_terms=topic_candidates,
                        browser=args.browser,
                        headless=effective_headless,
                        api_navigation_timeout_ms=api_navigation_timeout_ms,
                        max_attempts=max(1, args.api_max_attempts),
                        cookies=cookie_dict or None,
                        headful_wait_seconds=effective_headful_wait_seconds,
                        seed_limit=max(1, args.topic_seed_limit),
                        lookup_delay=max(0.0, args.lookup_delay),
                        proxy=proxy_config,
                        topic_video_samples=max(0, args.topic_video_samples),
                        max_video_age_hours=max_video_age_hours,
                    )
                )
                if seed_error:
                    source_notes.append(seed_error)
                if seed_signals:
                    source_status["tiktok_api"] = True
                    merge_signals(combined_signals, seed_signals)
                if seed_song_signals:
                    source_status["tiktok_api"] = True
                    merge_song_signals(combined_song_signals, seed_song_signals)
                progress_log(
                    "TikTokApi topic seed scan complete: "
                    f"hashtags={len(seed_signals)} songs={len(seed_song_signals)} "
                    f"elapsed={time.monotonic() - seed_started:.1f}s",
                    force=True,
                )

    if not combined_signals:
        print("No trend signals could be collected.")
        for err in source_errors:
            print(f"- {safe_console_text(err)}")
        sys.exit(1)

    history_payload = {"last_run_at": None, "hashtags": {}}
    if save_local_files:
        history_payload = load_json_file(history_file, history_payload)
    results = score_hashtags(
        signals=combined_signals,
        history_payload=history_payload,
        topic_terms=topic_terms if topic_terms else None,
    )
    if topic_terms:
        results, topic_note = apply_topic_filter(
            results=results,
            topic_terms=topic_terms,
            min_relevance=args.topic_min_relevance,
            strict=not args.topic_loose,
            allow_generic_topic_tags=args.allow_generic_topic_tags,
            max_topic_global_views=safe_int(args.max_topic_global_views),
        )
        if topic_note:
            source_notes.append(topic_note)
        results, used_emerging = prefer_emerging_rows(results)
        if not used_emerging:
            trend_feed_blocked = any(
                "trend feed failed across retries" in str(err).lower()
                for err in source_errors
            )
            auto_fallback_due_to_block = trend_feed_blocked and len(results) > 0
            if args.allow_stable_topic_fallback or auto_fallback_due_to_block:
                source_notes.append(
                    "No new/rising topic hashtags in this run window; showing closest specific topic matches."
                )
                if auto_fallback_due_to_block and not args.allow_stable_topic_fallback:
                    source_notes.append(
                        "Auto-fallback enabled because TikTok live trend feed is currently blocked."
                    )
            else:
                source_notes.append(
                    "No new/rising topic hashtags found in this run window."
                )
                results = []
    new_history = update_history(history_payload=history_payload, results=results, timestamp_iso=timestamp_iso)
    target_song_count = max(1, args.song_top)
    song_results = score_songs(combined_song_signals) if topic_terms else []
    if topic_terms and len(song_results) < target_song_count:
        progress_log(
            "Starting topic hashtag related-video song scan: "
            f"need={target_song_count} current={len(song_results)}",
            force=True,
        )
        hashtag_song_started = time.monotonic()
        topic_hashtag_song_signals, topic_hashtag_notes = collect_song_signals_from_topic_hashtag_related_videos(
            session=session,
            locale=args.locale,
            topic_terms=topic_terms,
            candidate_terms=topic_candidates,
            max_hashtag_pages=max(1, args.topic_hashtag_pages),
            max_related_per_hashtag=max(0, args.topic_hashtag_video_samples),
            max_total_related_videos=max(1, args.topic_max_related_videos),
            request_delay=max(0.0, args.lookup_delay),
            max_video_age_hours=max_video_age_hours,
        )
        for note in topic_hashtag_notes:
            source_notes.append(note)

        if topic_hashtag_song_signals:
            before_count = len(combined_song_signals)
            merge_song_signals(combined_song_signals, topic_hashtag_song_signals)
            song_results = score_songs(combined_song_signals)
            added_count = max(0, len(combined_song_signals) - before_count)
            source_notes.append(
                "Songs were inferred from videos linked to topic hashtag pages on Creative Center."
                + (f" Added {added_count} song candidates." if added_count > 0 else "")
            )
        progress_log(
            "Topic hashtag related-video song scan complete: "
            f"added={len(topic_hashtag_song_signals)} "
            f"elapsed={time.monotonic() - hashtag_song_started:.1f}s",
            force=True,
        )

    if topic_terms and len(song_results) < target_song_count:
        progress_log(
            "Starting Creative Center music fallback scan: "
            f"need={target_song_count} current={len(song_results)}",
            force=True,
        )
        music_fallback_started = time.monotonic()
        creative_music_rows, creative_music_error = fetch_creative_center_music_rows(
            session=session,
            locale=args.locale,
        )
        if creative_music_error:
            source_notes.append(creative_music_error)
        related_song_signals, related_music_notes = collect_song_signals_from_creative_music_related_videos(
            session=session,
            music_rows=creative_music_rows,
            topic_terms=topic_terms,
            max_related_per_song=max(0, args.music_related_video_samples),
            request_delay=max(0.0, args.lookup_delay),
            max_video_age_hours=max_video_age_hours,
        )
        for note in related_music_notes:
            source_notes.append(note)

        if related_song_signals:
            before_count = len(combined_song_signals)
            merge_song_signals(combined_song_signals, related_song_signals)
            song_results = score_songs(combined_song_signals)
            added_count = max(0, len(combined_song_signals) - before_count)
            source_notes.append(
                "Songs were augmented using topic-matched related videos on Creative Center music."
                + (f" Added {added_count} song candidates." if added_count > 0 else "")
            )
        elif not song_results and args.allow_broad_song_fallback:
            generic_fallback = score_creative_center_music_rows(
                rows=creative_music_rows,
                topic_terms=topic_terms,
            )
            if generic_fallback:
                song_results = generic_fallback
                source_notes.append(
                    "No topic-linked videos were readable; showing broad Creative Center music fallback because --allow-broad-song-fallback was set."
                )
            else:
                source_notes.append(
                    "No topic-linked song names could be extracted from accessible videos in this run."
                )
        elif not song_results:
            source_notes.append(
                "No topic-linked song names could be extracted from accessible videos in this run."
            )
        progress_log(
            "Creative Center music fallback scan complete: "
            f"songs_now={len(song_results)} "
            f"elapsed={time.monotonic() - music_fallback_started:.1f}s",
            force=True,
        )

    supabase_upload_summary: Dict[str, Any] = {
        "enabled": bool(topic_terms and not args.no_supabase_upload),
        "table": args.supabase_table,
        "on_conflict": args.supabase_on_conflict,
        "min_velocity": float(max(0.0, args.supabase_min_velocity)),
        "filtered_out_rows": 0,
        "prepared_rows": 0,
        "uploaded_rows": 0,
        "failed_rows": 0,
        "errors": [],
    }
    if supabase_upload_summary["enabled"]:
        progress_log("Starting Supabase topic song upload stage...", force=True)
        table_name = clean_supabase_table_name(args.supabase_table)
        if not table_name:
            msg = "Supabase upload skipped: invalid --supabase-table name."
            source_notes.append(msg)
            supabase_upload_summary["errors"].append(msg)
        else:
            supabase_upload_summary["table"] = table_name
            upload_source_rows = song_results
            min_velocity = float(max(0.0, args.supabase_min_velocity))
            if min_velocity > 0.0:
                filtered_rows: List[Dict[str, Any]] = []
                for row in song_results:
                    try:
                        velocity = float(row.get("velocity_views_per_hour") or 0.0)
                    except Exception:
                        velocity = 0.0
                    if velocity > min_velocity:
                        filtered_rows.append(row)
                supabase_upload_summary["filtered_out_rows"] = max(0, len(song_results) - len(filtered_rows))
                upload_source_rows = filtered_rows
                source_notes.append(
                    "Supabase upload velocity filter active: "
                    f"{len(upload_source_rows)}/{len(song_results)} rows above "
                    f"{min_velocity:.2f} views/hour."
                )
            prepared_rows = build_supabase_topic_song_rows(
                song_rows=upload_source_rows,
                topic_query=args.topic,
                generated_at_iso=timestamp_iso,
            )
            supabase_upload_summary["prepared_rows"] = len(prepared_rows)
            progress_log(
                f"Supabase upload prepared rows: {len(prepared_rows)}",
                force=True,
            )
            if not prepared_rows:
                source_notes.append("Supabase upload skipped: no topic song rows with link data to upload.")
            else:
                supabase_url, supabase_key = resolve_supabase_url_and_key(
                    repo_root=repo_root,
                    url_override=args.supabase_url,
                    project_id_override=args.supabase_project_id,
                    key_override=args.supabase_key,
                )
                if not supabase_url or not supabase_key:
                    msg = (
                        "Supabase upload skipped: missing credentials. Set SUPABASE_URL (or SUPABASE_PROJECT_ID) "
                        "and SUPABASE_SECRET_KEY in environment/app.env, or pass --supabase-url/--supabase-key."
                    )
                    source_notes.append(msg)
                    supabase_upload_summary["errors"].append(msg)
                else:
                    supabase_upload_summary["supabase_url"] = supabase_url
                    upload_started = time.monotonic()
                    uploaded_rows, upload_errors = upsert_topic_songs_to_supabase(
                        session=session,
                        supabase_url=supabase_url,
                        supabase_key=supabase_key,
                        table_name=table_name,
                        rows=prepared_rows,
                        on_conflict=args.supabase_on_conflict,
                        timeout_seconds=max(1.0, args.supabase_timeout),
                    )
                    supabase_upload_summary["uploaded_rows"] = uploaded_rows
                    supabase_upload_summary["failed_rows"] = max(0, len(prepared_rows) - uploaded_rows)
                    if upload_errors:
                        supabase_upload_summary["errors"] = upload_errors
                        source_errors.append(
                            "Supabase topic song upload failures: "
                            + " | ".join(upload_errors[:3])
                            + (" | ..." if len(upload_errors) > 3 else "")
                        )
                    source_notes.append(
                        f"Supabase upload: {uploaded_rows}/{len(prepared_rows)} topic song rows upserted into "
                        f"'{table_name}'."
                    )
                    progress_log(
                        f"Supabase upload complete: {uploaded_rows}/{len(prepared_rows)} "
                        f"in {time.monotonic() - upload_started:.1f}s",
                        force=True,
                    )

    report_payload = {
        "generated_at": timestamp_iso,
        "public_data_sources": source_status,
        "errors": source_errors,
        "notes": source_notes,
        "runtime": {
            "headless_enforced": True,
            "proxy_enabled": bool(proxy_config),
            "cookies_file": str(cookie_path) if cookie_path.exists() else None,
            "save_local_files": save_local_files,
            "history_file": str(history_file) if save_local_files else None,
            "output_json": str(output_json) if save_local_files else None,
            "discover_dances_enabled": bool(
                topic_terms and not args.no_discover_dances and discover_source_urls
            ),
            "discover_dances_url": (discover_source_urls[0] if topic_terms and discover_source_urls else None),
            "discover_dances_urls": discover_source_urls if topic_terms else [],
            "discover_dances_videos": max(1, args.discover_dances_videos),
            "discover_scroll_rounds": max(0, args.discover_scroll_rounds),
            "discover_video_urls_collected": discover_video_url_count,
            "max_video_age_days": (None if max_video_age_hours is None else round(max_video_age_hours / 24.0, 2)),
            "topic_hashtag_pages": max(1, args.topic_hashtag_pages),
            "topic_hashtag_video_samples": max(0, args.topic_hashtag_video_samples),
            "topic_max_related_videos": max(1, args.topic_max_related_videos),
            "music_related_video_samples": max(0, args.music_related_video_samples),
            "allow_broad_song_fallback": bool(args.allow_broad_song_fallback),
            "api_blocked": bool(api_blocked),
            "force_topic_seed_when_api_blocked": bool(args.force_topic_seed_when_api_blocked),
        },
        "supabase_upload": supabase_upload_summary,
        "topic": {
            "query": args.topic or None,
            "terms": sorted(topic_terms) if topic_terms else [],
            "strict": bool(topic_terms and not args.topic_loose),
            "min_relevance": args.topic_min_relevance if topic_terms else None,
            "allow_generic_topic_tags": bool(args.allow_generic_topic_tags) if topic_terms else None,
            "max_topic_global_views": safe_int(args.max_topic_global_views) if topic_terms else None,
            "prefer_emerging": bool(topic_terms),
            "allow_stable_topic_fallback": bool(args.allow_stable_topic_fallback) if topic_terms else None,
            "candidate_count": len(topic_candidates) if topic_terms else 0,
        },
        "totals": {
            "hashtags_scored": len(results),
            "hashtags_printed": 0,
            "songs_scored": len(song_results),
            "songs_printed": 0,
        },
        "results": results,
        "songs": song_results,
    }
    progress_log(
        "Scoring complete: "
        f"hashtags={len(results)} songs={len(song_results)} "
        f"errors={len(source_errors)} notes={len(source_notes)}",
        force=True,
    )

    if topic_terms:
        print(
            f"Topic mode active: '{safe_console_text(args.topic)}' "
            f"({len(topic_terms)} terms, strict={str(not args.topic_loose).lower()})"
        )
    should_print_hashtag_report = (not topic_terms) or bool(args.show_hashtag_report)
    printed_count = 0
    if should_print_hashtag_report:
        printed_count = print_report(results=results, top_n=max(1, args.top), min_score=args.min_score)
    report_payload["totals"]["hashtags_printed"] = printed_count
    if topic_terms:
        songs_printed = print_song_report(
            song_rows=song_results,
            top_n=max(1, args.song_top),
            min_score=max(0.0, args.song_min_score),
        )
        report_payload["totals"]["songs_printed"] = songs_printed
    progress_log(
        "Console reporting complete: "
        f"hashtags_printed={report_payload['totals']['hashtags_printed']} "
        f"songs_printed={report_payload['totals']['songs_printed']}",
        force=True,
    )
    if save_local_files:
        save_json_file(output_json, report_payload)
        save_json_file(history_file, new_history)
    if source_notes:
        print()
        print("Notes:")
        for note in source_notes:
            print(f"- {safe_console_text(note)}")
    if source_errors:
        print()
        print("Source warnings:")
        for err in source_errors:
            print(f"- {safe_console_text(err)}")
    if save_local_files:
        print()
        print(f"Saved report JSON: {output_json}")
        print(f"Updated history:   {history_file}")


if __name__ == "__main__":
    main()
