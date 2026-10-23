"""Compute which TikTok accounts each listener shard follows, from Supabase.

    python scripts/listener_accounts.py          # prints "<set>\t<comma,separated,handles>" per line
    python scripts/listener_accounts.py --json

  Set 1: the primary account (LISTENER_PRIMARY_ACCOUNT, default wildcard_ns)
  Set 2: every enabled group's TikTok account
  Set 3: every dancer handle across enabled groups (minus sets 1-2)

TIKTOK_USERNAMES_SET_2 / _SET_3 in app.env are optional extras merged into those sets
(for accounts that aren't a group or a dancer). Sets 4+ stay manual in app.env.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.supabase_rest import SupabaseRest  # noqa: E402
from scripts.utils import _env  # noqa: E402

_HANDLE_RE = re.compile(r"^[a-z0-9._]{2,64}$")


def _clean(handle) -> str:
    value = str(handle or "").strip().lstrip("@").lower()
    return value if _HANDLE_RE.match(value) else ""


def _extras(var: str) -> list[str]:
    return [h for h in (_clean(x) for x in _env(var, "").split(",")) if h]


def _ordered(items) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item:
            seen.setdefault(item, None)
    return list(seen)


def compute_sets(rest: SupabaseRest | None = None) -> dict[int, list[str]]:
    rest = rest or SupabaseRest()
    groups = rest.select("groups", {"select": "id,tiktok_handle,disabled", "order": "created_at"})
    active = [g for g in groups if not g.get("disabled")]
    active_ids = {g["id"] for g in active}
    dancer_docs = rest.select("group_kv", {"select": "group_id,value", "key": "eq.dancers"})

    set1 = _ordered([_clean(_env("LISTENER_PRIMARY_ACCOUNT", "wildcard_ns") or "wildcard_ns")])
    set2 = _ordered(h for h in [_clean(g.get("tiktok_handle")) for g in active] + _extras("TIKTOK_USERNAMES_SET_2")
                    if h not in set1)
    dancers = []
    for doc in dancer_docs:
        if doc.get("group_id") not in active_ids or not isinstance(doc.get("value"), list):
            continue
        dancers.extend(_clean(d.get("handle")) for d in doc["value"] if isinstance(d, dict))
    taken = set(set1) | set(set2)
    set3 = _ordered(h for h in dancers + _extras("TIKTOK_USERNAMES_SET_3") if h not in taken)
    return {1: set1, 2: set2, 3: set3}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sets = compute_sets()
    if args.json:
        print(json.dumps(sets, indent=2))
    else:
        for idx, handles in sets.items():
            print(f"{idx}\t{','.join(handles)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
