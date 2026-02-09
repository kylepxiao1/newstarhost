import json
import re


def _safe_json(payload) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps(str(payload))
        except Exception:
            return "{}"


def _get_any(payload: dict, *keys, default=None):
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


def _get_attr_any(obj, *names, default=None):
    for name in names:
        try:
            if hasattr(obj, name):
                val = getattr(obj, name, None)
                if val is not None:
                    return val
        except Exception:
            continue
    return default


def _normalize_db_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if _is_placeholder_obj(value):
            return None
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    if isinstance(value, (dict, list, tuple)):
        return _safe_json(value)
    try:
        text = str(value)
    except Exception:
        return None
    if _is_placeholder_obj(text):
        return None
    return text


def _is_placeholder_obj(val: str) -> bool:
    return val.startswith("<object object") and "object at" in val


def _is_valid_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)) and value == 0:
        return False
    try:
        text = str(value).strip()
    except Exception:
        return False
    if not text:
        return False
    if text in {"0", "0.0", "0.00"}:
        return False
    return not _is_placeholder_obj(text)


def _safe_event_user(event):
    try:
        return getattr(event, "user", None)
    except Exception:
        return None


def _extract_handle(user_obj) -> str:
    """Return the best-effort handle/unique_id/display_id from various TikTok user representations."""
    if not user_obj:
        return ""
    try:
        # ExtendedUser or similar object
        for attr in ("unique_id", "display_id", "username", "nick_name", "nickname"):
            if hasattr(user_obj, attr):
                val = getattr(user_obj, attr, None)
                if val:
                    return str(val)
        # Dict representation
        if isinstance(user_obj, dict):
            for key in ("unique_id", "display_id", "username", "nick_name", "nickname"):
                val = user_obj.get(key)
                if val:
                    return str(val)
        # String repr like "User(... username='fffernxndo' ...)"
        if isinstance(user_obj, str):
            m = re.search(r"username='([^']+)'", user_obj)
            if m:
                return m.group(1)
            m = re.search(r"unique_id='([^']+)'", user_obj)
            if m:
                return m.group(1)
            m = re.search(r"display_id='([^']+)'", user_obj)
            if m:
                return m.group(1)
    except Exception:
        return ""
    return ""


def _extract_recipient_from_describe(describe: str) -> str:
    """
    Roughly parse the describe text like 'X: gifted Y 1 Swan' to get Y.
    """
    if not describe:
        return ""
    try:
        if "gifted" in describe:
            tail = describe.split("gifted", 1)[1].strip()
            toks = tail.split()
            if len(toks) > 2:
                return " ".join(toks[:-2])
            if toks:
                return toks[0]
    except Exception:
        return ""
    return ""
