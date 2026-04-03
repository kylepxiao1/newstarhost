#!/usr/bin/env python3
"""
One-time backfill: forward all topic_trends rows with shared=false and cluster_id IS NOT NULL
to Discord, then mark them shared=true.

Usage:
    .venv/bin/python scripts/backfill_trendbot_shares.py [--dry-run] [--limit N]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


def load_env_file_values(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
    except FileNotFoundError:
        pass
    return result


def first_non_empty(values: List[str]) -> str:
    for v in values:
        if v and v.strip():
            return v.strip()
    return ""


def resolve_credentials(repo_root: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    app_env = load_env_file_values(repo_root / "app.env")

    url = first_non_empty([
        os.environ.get("SUPABASE_URL", ""),
        app_env.get("SUPABASE_URL", ""),
    ])
    project_id = first_non_empty([
        os.environ.get("SUPABASE_PROJECT_ID", ""),
        app_env.get("SUPABASE_PROJECT_ID", ""),
    ])
    if not url and project_id:
        url = f"https://{project_id}.supabase.co"

    key = first_non_empty([
        os.environ.get("SUPABASE_SECRET_KEY", ""),
        app_env.get("SUPABASE_SECRET_KEY", ""),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        app_env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        os.environ.get("SUPABASE_SERVICE_ROLE", ""),
        app_env.get("SUPABASE_SERVICE_ROLE", ""),
        os.environ.get("SUPABASE_API_KEY", ""),
        app_env.get("SUPABASE_API_KEY", ""),
    ])

    discord_webhook = first_non_empty([
        os.environ.get("DISCORD_WEBHOOK_URL", ""),
        app_env.get("DISCORD_WEBHOOK_URL", ""),
    ])

    return (url or None), (key or None), (discord_webhook or None)


def fetch_unshared_rows(supabase_url: str, key: str, table: str, limit: int) -> List[Dict[str, Any]]:
    endpoint = f"{supabase_url}/rest/v1/{table}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    params = {
        "shared": "eq.false",
        "cluster_id": "not.is.null",
        "order": "generated_at.desc",
        "limit": str(limit),
    }
    resp = requests.get(endpoint, headers=headers, params=params, timeout=30)
    if not resp.ok:
        print(f"  ERROR body: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def mark_shared(supabase_url: str, key: str, table: str, row_id: Any) -> bool:
    endpoint = f"{supabase_url}/rest/v1/{table}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    params = {
        "id": f"eq.{row_id}",
        "shared": "is.false",
        "cluster_id": "not.is.null",
    }
    resp = requests.patch(endpoint, headers=headers, params=params, json={"shared": True}, timeout=30)
    if not resp.ok:
        return False
    rows = resp.json()
    return isinstance(rows, list) and len(rows) > 0


def format_number(value: Any) -> str:
    try:
        n = float(str(value).replace(",", ""))
        if abs(n) >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if abs(n) >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(int(n))
    except (ValueError, TypeError):
        return "n/a"


def build_discord_message(row: Dict[str, Any]) -> str:
    song = str(row.get("song") or "unknown song").strip()
    artist = str(row.get("artist") or "unknown artist").strip()
    link = str(row.get("song_link") or "(missing link)").strip()
    topic = str(row.get("topic") or "n/a").strip()
    plays = format_number(row.get("recent_play_count"))
    likes_val = row.get("likes") or row.get("recent_digg_count")
    likes = format_number(likes_val)
    vel = row.get("velocity_views_per_hour")
    velocity = f"{format_number(vel)}/hour" if vel is not None else "n/a"

    hashtags_raw = row.get("hashtags")
    if isinstance(hashtags_raw, str):
        try:
            hashtags_raw = json.loads(hashtags_raw)
        except Exception:
            hashtags_raw = [h.strip() for h in hashtags_raw.split(",") if h.strip()]
    if isinstance(hashtags_raw, list):
        tags = [f"#{str(t).lstrip('#').strip()}" for t in hashtags_raw if str(t).strip()][:8]
        hashtags_text = " ".join(tags) if tags else "n/a"
    else:
        hashtags_text = "n/a"

    lines = [
        "**Viral Dance Detected** *(backfill)*",
        f"Song: **{song}**",
        f"Artist: **{artist}**",
        f"Recent plays: `{plays}`",
        f"Likes: `{likes}`",
        f"Velocity: `{velocity}`",
        f"Hashtags: {hashtags_text}",
        f"Topic: `{topic}`",
        f"Link: {link}",
    ]
    msg = "\n".join(lines)
    return msg[:1900]


def send_discord(webhook_url: str, content: str) -> bool:
    resp = requests.post(
        webhook_url,
        json={"content": content, "allowed_mentions": {"parse": []}},
        timeout=15,
    )
    return resp.ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill unshared trendbot rows to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without sending or marking.")
    parser.add_argument("--limit", type=int, default=50, help="Max rows to process (default 50).")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between Discord sends (default 1.5).")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    supabase_url, key, discord_webhook = resolve_credentials(repo_root)

    if not supabase_url or not key:
        print("ERROR: Missing Supabase credentials (SUPABASE_URL + SUPABASE_API_KEY/SUPABASE_SECRET_KEY).", file=sys.stderr)
        sys.exit(1)
    if not discord_webhook and not args.dry_run:
        print("ERROR: Missing DISCORD_WEBHOOK_URL.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching up to {args.limit} unshared rows with cluster_id set...")
    rows = fetch_unshared_rows(supabase_url, key, "topic_trends", args.limit)
    print(f"Found {len(rows)} rows to process.")

    if not rows:
        print("Nothing to do.")
        return

    forwarded = 0
    skipped = 0
    for i, row in enumerate(rows, 1):
        row_id = row.get("id")
        song = row.get("song", "?")
        cluster_id = row.get("cluster_id")
        print(f"[{i}/{len(rows)}] id={row_id} song={song!r} cluster_id={cluster_id}")

        if args.dry_run:
            print("  [dry-run] would send to Discord and mark shared=true")
            continue

        # Atomically claim the row first
        claimed = mark_shared(supabase_url, key, "topic_trends", row_id)
        if not claimed:
            print("  skipped (already claimed or condition not met)")
            skipped += 1
            continue

        # Send to Discord
        content = build_discord_message(row)
        ok = send_discord(discord_webhook, content)
        if ok:
            print("  forwarded to Discord ✓")
            forwarded += 1
        else:
            print("  WARNING: Discord send failed (row already marked shared=true)")

        if i < len(rows):
            time.sleep(args.delay)

    print(f"\nDone. forwarded={forwarded} skipped={skipped} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
