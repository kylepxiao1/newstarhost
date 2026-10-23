"""Manage dance group logins (there is no sign-up page).

    python scripts/manage_groups.py list
    python scripts/manage_groups.py create <username> --name "Group Name" [--handle tiktok_handle] [--generate]
    python scripts/manage_groups.py set-password <username> [--generate | --no-password]  # logs out every device
    python scripts/manage_groups.py update <username> [--name ...] [--handle ...]
    python scripts/manage_groups.py rotate-overlay <username>              # new OBS overlay link
    python scripts/manage_groups.py logout-all <username>
    python scripts/manage_groups.py disable|enable <username>

Passwords are typed at a hidden prompt (or generated with --generate and printed once).
"""
from __future__ import annotations

import argparse
import getpass
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth import hash_password, new_overlay_key  # noqa: E402
from backend.supabase_rest import SupabaseRest  # noqa: E402

rest = SupabaseRest()
APP_URL = "https://newstarhost.fly.dev"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(username: str) -> dict:
    row = rest.select_one("groups", {"username": f"eq.{username.lower()}"})
    if not row:
        raise SystemExit(f"No group with username {username!r}")
    return row


def _password(generate: bool, none: bool = False) -> str:
    """Returns the hash to store ("" = no password: log in with just the username)."""
    if none:
        print("No password: anyone who knows the username can log in and edit this group.")
        return ""
    if generate:
        pw = secrets.token_urlsafe(12)
        print(f"Generated password: {pw}  (shown once; share it securely)")
        return hash_password(pw)
    pw = getpass.getpass("New password: ")
    if len(pw) < 8:
        raise SystemExit("Password must be at least 8 characters.")
    if getpass.getpass("Repeat password: ") != pw:
        raise SystemExit("Passwords don't match.")
    return hash_password(pw)


def cmd_list(_args) -> None:
    rows = rest.select("groups", {"select": "username,display_name,tiktok_handle,disabled,created_at", "order": "created_at"})
    for r in rows:
        flag = "  (disabled)" if r["disabled"] else ""
        print(f"{r['username']:20} {r['display_name']:25} @{r['tiktok_handle'] or '-':25}{flag}")


def cmd_create(args) -> None:
    username = args.username.lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
        raise SystemExit("Username: 3-40 characters, letters/numbers/_ . - only.")
    if rest.select_one("groups", {"username": f"eq.{username}"}):
        raise SystemExit(f"{username!r} already exists.")
    row = {
        "username": username,
        "display_name": args.name or username,
        "tiktok_handle": (args.handle or "").lstrip("@") or None,
        "password_hash": _password(args.generate, args.no_password),
        "overlay_key": new_overlay_key(),
    }
    created = rest.insert("groups", row, returning=True)[0]
    print(f"Created {username} ({created['id']}). Log in at {APP_URL}/login")
    print(f"OBS overlay link: {APP_URL}/overlay?key={created['overlay_key']}")


def cmd_set_password(args) -> None:
    row = _get(args.username)
    rest.update("groups", {"id": f"eq.{row['id']}"}, {
        "password_hash": _password(args.generate, args.no_password),
        "session_version": int(row["session_version"]) + 1,
        "updated_at": _now(),
    })
    print(f"Password updated for {row['username']}; all devices were logged out.")


def cmd_update(args) -> None:
    row = _get(args.username)
    values = {"updated_at": _now()}
    if args.name:
        values["display_name"] = args.name
    if args.handle is not None:
        values["tiktok_handle"] = args.handle.lstrip("@") or None
    rest.update("groups", {"id": f"eq.{row['id']}"}, values)
    print(f"Updated {row['username']}. (The running app picks this up within ~30s; the group name in "
          f"live state updates after the next restart.)")


def cmd_rotate_overlay(args) -> None:
    row = _get(args.username)
    key = new_overlay_key()
    rest.update("groups", {"id": f"eq.{row['id']}"}, {"overlay_key": key, "updated_at": _now()})
    print(f"New OBS overlay link: {APP_URL}/overlay?key={key}  (the old link stops working)")


def cmd_logout_all(args) -> None:
    row = _get(args.username)
    rest.update("groups", {"id": f"eq.{row['id']}"}, {"session_version": int(row["session_version"]) + 1, "updated_at": _now()})
    print(f"Logged out every device for {row['username']}.")


def cmd_disable(args, disabled: bool = True) -> None:
    row = _get(args.username)
    rest.update("groups", {"id": f"eq.{row['id']}"}, {"disabled": disabled, "updated_at": _now()})
    print(f"{row['username']} {'disabled' if disabled else 'enabled'}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    p = sub.add_parser("create")
    p.add_argument("username")
    p.add_argument("--name")
    p.add_argument("--handle")
    p.add_argument("--generate", action="store_true")
    p.add_argument("--no-password", action="store_true", help="Log in with just the username.")
    p.set_defaults(func=cmd_create)
    p = sub.add_parser("set-password")
    p.add_argument("username")
    p.add_argument("--generate", action="store_true")
    p.add_argument("--no-password", action="store_true", help="Remove the password (username-only login).")
    p.set_defaults(func=cmd_set_password)
    p = sub.add_parser("update")
    p.add_argument("username")
    p.add_argument("--name")
    p.add_argument("--handle")
    p.set_defaults(func=cmd_update)
    for name, func in [("rotate-overlay", cmd_rotate_overlay), ("logout-all", cmd_logout_all),
                       ("disable", lambda a: cmd_disable(a, True)), ("enable", lambda a: cmd_disable(a, False))]:
        p = sub.add_parser(name)
        p.add_argument("username")
        p.set_defaults(func=func)
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
