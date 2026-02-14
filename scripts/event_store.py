import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import betterproto
import httpx
from TikTokLive.events.proto_events import WebcastCompetitionMessage

from utils import (
    _env,
    _extract_handle,
    _extract_recipient_from_describe,
    _get_any,
    _get_attr_any,
    _is_valid_value,
    _normalize_db_value,
    _safe_json,
    _safe_event_user
)

logger = logging.getLogger("tiktok-listener")


class SupabaseEventStore:
    def __init__(self, url: str, key: str, debug: bool = False) -> None:
        self._lock = asyncio.Lock()
        self._debug = debug
        self._client: Optional[httpx.AsyncClient] = None
        self._rest_url = ""
        self._raw_id_seq = int(time.time() * 1000) % 1000
        self._write_tiktok_events_raw = _env("SUPABASE_WRITE_TIKTOK_EVENTS_RAW", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        if not url or not key:
            logger.error(
                "Supabase credentials missing. Set SUPABASE_URL and SUPABASE_SECRET_KEY (or SERVICE_ROLE_KEY)."
            )
            return
        self._rest_url = url.rstrip("/") + "/rest/v1"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(base_url=self._rest_url, headers=headers, timeout=30.0)
        logger.info("Supabase REST client initialized for %s", self._rest_url)
        if self._write_tiktok_events_raw:
            logger.info("Supabase raw event writes enabled for tiktok_events_raw")
        else:
            logger.info(
                "Supabase raw event writes disabled for tiktok_events_raw (set SUPABASE_WRITE_TIKTOK_EVENTS_RAW=1 to enable)"
            )

    async def _post_row(
        self,
        table: str,
        row_data: dict,
        on_conflict: Optional[str] = None,
        prefer: Optional[str] = None,
    ) -> Optional[httpx.Response]:
        if not self._client:
            return None
        params = {}
        prefer_header = "return=minimal"
        if on_conflict:
            params["on_conflict"] = on_conflict
            prefer_header = "resolution=merge-duplicates,return=minimal"
        if prefer:
            prefer_header = prefer
        if self._debug:
            logger.debug(
                "Supabase POST %s on_conflict=%s id=%s message_id=%s",
                table,
                on_conflict or "none",
                row_data.get("id"),
                row_data.get("message_id"),
            )
        return await self._client.post(
            f"/{table}",
            params=params,
            json=row_data,
            headers={"Prefer": prefer_header},
        )

    def _log_result(self, table: str, action: str, resp: Optional[httpx.Response]) -> None:
        if resp is None:
            logger.debug("Supabase %s %s skipped (client unavailable)", table, action)
            return
        if resp.is_success:
            logger.debug("Supabase %s %s ok (status=%s)", table, action, resp.status_code)
            return
        try:
            payload = resp.json()
        except Exception:
            payload = (resp.text or "").strip()
        logger.error("Supabase %s %s failed (status=%s): %s", table, action, resp.status_code, payload)

    def _response_missing_unique(self, resp: Optional[httpx.Response]) -> bool:
        if resp is None:
            return False
        msg = ""
        code = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                msg = str(payload.get("message", "")).lower()
                code = str(payload.get("code", "")).lower()
            else:
                msg = str(payload).lower()
        except Exception:
            msg = (resp.text or "").lower()
        return "no unique or exclusion constraint" in msg or "42p10" in msg or code == "42p10"

    def _next_raw_id(self) -> int:
        # millisecond timestamp * 1000 + sequence to avoid collisions within the same ms
        base = int(time.time() * 1000) * 1000
        self._raw_id_seq = (self._raw_id_seq + 1) % 1000
        return base + self._raw_id_seq

    def _next_row_id(self) -> int:
        return self._next_raw_id()

    def _to_int(self, value) -> Optional[int]:
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

    def _camel_from_snake(self, value: str) -> str:
        parts = value.split("_")
        if not parts:
            return value
        return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])

    async def log_competition_event(
        self,
        payload: dict,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> None:
        payload_b64 = _get_any(payload, "payload")
        if not payload_b64:
            return

        raw_bytes = b""
        if isinstance(payload_b64, (bytes, bytearray)):
            raw_bytes = bytes(payload_b64)
            payload_b64 = base64.b64encode(raw_bytes).decode("ascii")
        elif isinstance(payload_b64, str):
            try:
                raw_bytes = base64.b64decode(payload_b64 + "===")
            except Exception:
                logger.warning("Failed to base64 decode competition payload")
                return
        else:
            return

        try:
            msg = WebcastCompetitionMessage().parse(raw_bytes)
            decoded = msg.to_dict()
        except Exception:
            logger.exception("Failed to decode WebcastCompetitionMessage")
            return

        active_stage = None
        for field_name in (
            "initiate",
            "reply",
            "start",
            "settle_start",
            "settle_end",
            "score_change",
            "finish",
            "switch_turn",
        ):
            try:
                if betterproto.serialized_on_wire(getattr(msg, field_name)):
                    active_stage = field_name
                    break
            except Exception:
                continue

        active_payload = None
        if active_stage:
            active_payload = decoded.get(self._camel_from_snake(active_stage))

        competition_message_type = None
        if isinstance(decoded, dict):
            raw_type = decoded.get("type")
            if isinstance(raw_type, str) and raw_type.strip():
                competition_message_type = raw_type.strip()
            elif raw_type is not None:
                enum_name = getattr(raw_type, "name", None)
                if isinstance(enum_name, str) and enum_name.strip():
                    competition_message_type = enum_name.strip()
        if not competition_message_type and active_stage:
            stage_to_type = {
                "initiate": "COMPETITION_MESSAGE_TYPE_INITIATE",
                "reply": "COMPETITION_MESSAGE_TYPE_REPLY",
                "start": "COMPETITION_MESSAGE_TYPE_START",
                "score_change": "COMPETITION_MESSAGE_TYPE_SCORE_CHANGE",
                "finish": "COMPETITION_MESSAGE_TYPE_FINISH",
                "settle_start": "COMPETITION_MESSAGE_TYPE_SETTLE_START",
                "settle_end": "COMPETITION_MESSAGE_TYPE_SETTLE_END",
                "switch_turn": "COMPETITION_MESSAGE_TYPE_SWITCH_TURN",
            }
            competition_message_type = stage_to_type.get(active_stage)

        base_message = decoded.get("baseMessage") if isinstance(decoded, dict) else {}
        if not isinstance(base_message, dict):
            base_message = {}
        biz_common = decoded.get("bizCommon") if isinstance(decoded, dict) else {}
        if not isinstance(biz_common, dict):
            biz_common = {}

        team_infos = []
        if isinstance(active_payload, dict):
            candidate = active_payload.get("teamInfos")
            if isinstance(candidate, list):
                team_infos = candidate
            if not team_infos:
                direct_teams = active_payload.get("teams")
                if isinstance(direct_teams, list):
                    team_infos = direct_teams
            if not team_infos:
                initiate_info = active_payload.get("initiateInfo")
                if isinstance(initiate_info, dict):
                    start_teams = initiate_info.get("teams")
                    if isinstance(start_teams, list):
                        team_infos = start_teams

        def _to_id_str(value) -> Optional[str]:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:
                return None
            if not text:
                return None
            if text.endswith(".0"):
                text = text[:-2]
            return text

        team_scores = []
        team_member_ids = []
        for team in team_infos:
            if not isinstance(team, dict):
                continue
            score_val = self._to_int(team.get("score"))
            if score_val is not None:
                team_scores.append(score_val)
            members = team.get("members")
            if not isinstance(members, list):
                members = team.get("users")
            member_ids = []
            if isinstance(members, list):
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    user_obj = member.get("user")
                    user_id = None
                    if isinstance(user_obj, dict):
                        user_id = _to_id_str(user_obj.get("userId"))
                    if user_id is None:
                        user_id = _to_id_str(member.get("userId"))
                    if user_id is not None:
                        member_ids.append(user_id)
            team_member_ids.append(member_ids)

        end_timestamp = None
        end_timestamp_actual = None
        if isinstance(active_payload, dict):
            end_timestamp = self._to_int(
                _get_any(
                    active_payload,
                    "endTimestamp",
                    "end_timestamp",
                )
            )
            end_timestamp_actual = self._to_int(
                _get_any(
                    active_payload,
                    "actualEndTimestamp",
                    "endTimestampActual",
                    "end_timestamp_actual",
                )
            )

        now_dt = datetime.now(timezone.utc)
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": now_dt.isoformat(),
            "unix_ts": int(now_dt.timestamp()),
            "message_id": self._to_int(base_message.get("messageId"))
            or self._to_int(_get_any(payload, "msgId", "msg_id", "messageId", "message_id")),
            "room_id": self._to_int(base_message.get("roomId")) or self._to_int(biz_common.get("roomId")),
            "create_time_ms": self._to_int(base_message.get("createTime")),
            "competition_id": self._to_int(biz_common.get("competitionId")),
            "competition_room_id": self._to_int(biz_common.get("roomId")),
            "competition_type": biz_common.get("type"),
            "competition_message_type": competition_message_type,
            "active_stage": active_stage,
            "team_count": len(team_infos) if team_infos else 0,
            "team_1_score": team_scores[0] if len(team_scores) > 0 else None,
            "team_2_score": team_scores[1] if len(team_scores) > 1 else None,
            "team_1_member_ids": team_member_ids[0] if len(team_member_ids) > 0 else [],
            "team_2_member_ids": team_member_ids[1] if len(team_member_ids) > 1 else [],
            "end_timestamp": end_timestamp,
            "end_timestamp_actual": end_timestamp_actual,
            "total_score": sum(team_scores) if team_scores else 0,
            "team_infos_json": _safe_json(team_infos),
            "tiktok_username": tiktok_username,
        }

        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping competition_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                # Keep Postgres text[] fields as JSON arrays for PostgREST (not JSON-encoded strings).
                row_data["team_1_member_ids"] = row.get("team_1_member_ids") or []
                row_data["team_2_member_ids"] = row.get("team_2_member_ids") or []
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("competition_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("competition_events", row_data)
                    action = "insert-fallback"
                self._log_result("competition_events", action, resp)
            except Exception:
                logger.exception("Failed to log competition event")

    async def log_gift(
        self,
        payload: dict,
        event,
        tiktok_username: str,
        gift_value_raw: Optional[int],
        gift_value_delta: Optional[int],
        raw_id: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        base_message = _get_attr_any(event, "base_message", "baseMessage")
        gift_monitor_payload = None
        if isinstance(payload, dict):
            gift_monitor_payload = payload.get("gift_monitor_info") or payload.get("giftMonitorInfo")
        gift_monitor_event = _get_attr_any(event, "gift_monitor_info", "giftMonitorInfo")
        user_identity_payload = None
        if isinstance(payload, dict):
            user_identity_payload = payload.get("user_identity") or payload.get("userIdentity")
        user_identity_event = _get_attr_any(event, "user_identity", "userIdentity")
        gift = payload.get("gift") if isinstance(payload, dict) else {}
        if not isinstance(gift, dict):
            gift = {}
        gift_obj = _get_attr_any(event, "gift")
        gift_id = _get_any(gift, "id", "gift_id", "giftId") or _get_attr_any(gift_obj, "id", "gift_id", "giftId")
        gift_name = _get_any(gift, "name", "describe") or _get_attr_any(gift_obj, "name", "describe")
        diamond_count = _get_any(payload, "diamond_count", default=_get_any(gift, "diamond_count", "diamond_cost"))
        if diamond_count is None:
            diamond_count = _get_attr_any(gift_obj, "diamond_count", "diamondCost", "diamond_cost")
        repeat_count = _get_any(payload, "repeat_count", "repeatCount", default=_get_any(gift, "repeat_count"))
        if repeat_count is None:
            repeat_count = _get_attr_any(gift_obj, "repeat_count", "repeatCount")

        def _gift_monitor_val(*names):
            if gift_monitor_payload is not None:
                if isinstance(gift_monitor_payload, dict):
                    val = _get_any(gift_monitor_payload, *names)
                else:
                    val = _get_attr_any(gift_monitor_payload, *names)
                if _is_valid_value(val):
                    return val
            val = _get_attr_any(gift_monitor_event, *names)
            if _is_valid_value(val):
                return val
            return None

        def _user_identity_val(*names):
            if user_identity_payload is not None:
                if isinstance(user_identity_payload, dict):
                    val = _get_any(user_identity_payload, *names)
                else:
                    val = _get_attr_any(user_identity_payload, *names)
                if val is not None:
                    return val
            return _get_attr_any(user_identity_event, *names)

        anchor_id = _get_any(payload, "anchor_id", "anchorId") or _gift_monitor_val("anchor_id", "anchorId")
        describe_val = _get_any(payload, "describe") or _get_attr_any(base_message, "describe")
        room_heat_val = _get_any(payload, "room_message_heat_level") or _get_attr_any(
            base_message, "room_message_heat_level", "roomMessageHeatLevel"
        )
        gift_monitor_from_platform = _get_any(payload, "gift_monitor_from_platform") or _gift_monitor_val(
            "from_platform", "fromPlatform"
        )
        gift_monitor_from_version = _get_any(payload, "gift_monitor_from_version") or _gift_monitor_val(
            "from_version", "fromVersion"
        )
        gift_monitor_send_message_success_ms = _gift_monitor_val(
            "send_gift_send_message_success_ms", "sendGiftSendMessageSuccessMs"
        )
        gift_monitor_to_user_id = _gift_monitor_val("to_user_id", "toUserId")
        send_type = _get_any(payload, "send_type", "sendType")
        if not _is_valid_value(send_type):
            send_type = _get_attr_any(event, "send_type", "sendType")
        if not _is_valid_value(send_type):
            send_type = _get_any(gift, "send_type", "sendType") or _get_attr_any(gift_obj, "send_type", "sendType")
        order_id = _get_any(payload, "order_id", "orderId")
        if not _is_valid_value(order_id):
            order_id = _get_attr_any(event, "order_id", "orderId")
        fan_ticket_count = _get_any(payload, "fan_ticket_count")
        if not _is_valid_value(fan_ticket_count):
            if _is_valid_value(gift_value_raw):
                fan_ticket_count = gift_value_raw
            elif _is_valid_value(gift_value_delta):
                fan_ticket_count = gift_value_delta
            elif _is_valid_value(diamond_count):
                fan_ticket_count = diamond_count
        if not _is_valid_value(fan_ticket_count):
            fan_ticket_count = None
        room_fan_ticket_count = _get_any(payload, "room_fan_ticket_count")
        if not _is_valid_value(room_fan_ticket_count):
            room_fan_ticket_count = fan_ticket_count
        if not _is_valid_value(room_fan_ticket_count):
            room_fan_ticket_count = None
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "gift",
            "room_id": _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId"),
            "create_time_ms": _get_any(payload, "create_time", "create_time_ms", "timestamp") or _get_attr_any(
                base_message, "create_time", "createTime"
            ),
            "message_id": _get_any(payload, "msg_id", "message_id", "event_id") or _get_attr_any(
                base_message, "message_id", "messageId"
            ),
            "gift_id": gift_id,
            "gift_name": gift_name,
            "diamond_count": diamond_count,
            "repeat_count": repeat_count,
            "combo_count": _get_any(payload, "combo_count", "comboCount"),
            "amount_value": gift_value_raw if gift_value_raw is not None else _get_any(payload, "amount", "total_value"),
            "fan_ticket_count": fan_ticket_count,
            "room_fan_ticket_count": room_fan_ticket_count,
            "group_count": _get_any(payload, "group_count"),
            "repeat_end": _get_any(payload, "repeat_end", "repeatEnd"),
            "from_user_id": _get_any(payload, "user_id", "userId"),
            "from_username": _get_any(payload, "user_name", "userName"),
            "from_nickname": _get_any(payload, "user_nickname", "nickname"),
            "to_user_id": _get_any(payload, "to_user_id", "toUserId"),
            "to_username": _get_any(payload, "to_user_name", "toUserName"),
            "to_nickname": _get_any(payload, "to_user_nickname", "toNickname"),
            "to_member_id_int": _get_any(payload, "to_member_id_int", "toMemberIdInt"),
            "to_member_nickname": _get_any(payload, "to_member_nickname", "toMemberNickname"),
            "anchor_id": anchor_id,
            "send_type": send_type,
            "order_id": order_id,
            "group_id": _get_any(payload, "group_id", "groupId"),
            "describe": describe_val,
            "is_gift_giver_of_anchor": _get_any(payload, "is_gift_giver_of_anchor")
            or _user_identity_val("is_gift_giver_of_anchor", "isGiftGiverOfAnchor"),
            "is_subscriber_of_anchor": _get_any(payload, "is_subscriber_of_anchor")
            or _user_identity_val("is_subscriber_of_anchor", "isSubscriberOfAnchor"),
            "is_mutual_following_with_anchor": _get_any(
                payload, "is_mutual_following_with_anchor", "is_mutual_following_of_anchor"
            )
            or _user_identity_val("is_mutual_following_with_anchor", "is_mutual_following_of_anchor"),
            "is_follower_of_anchor": _get_any(payload, "is_follower_of_anchor")
            or _user_identity_val("is_follower_of_anchor", "isFollowerOfAnchor"),
            "gift_monitor_from_platform": gift_monitor_from_platform,
            "gift_monitor_from_version": gift_monitor_from_version,
            "gift_monitor_send_msg_ms": _get_any(payload, "gift_monitor_send_msg_ms")
            or gift_monitor_send_message_success_ms,
            "priority": _get_any(payload, "priority"),
            "room_message_heat_level": room_heat_val,
            "gift_monitor_anchor_id": _gift_monitor_val("anchor_id", "anchorId"),
            "gift_monitor_to_user_id": gift_monitor_to_user_id,
            "gift_monitor_send_message_success_ms": gift_monitor_send_message_success_ms,
            "tiktok_username": tiktok_username,
        }
        if row.get("to_user_id") is not None and not _is_valid_value(row.get("to_user_id")):
            row["to_user_id"] = None
        if not row.get("from_user_id") or not row.get("from_username") or not row.get("from_nickname"):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            event_user = _get_attr_any(event, "user", "user_info", "userInfo")
            if not row.get("from_user_id"):
                row["from_user_id"] = (
                    _get_any(user_payload, "id", "user_id", "userId")
                    or _get_attr_any(event_user, "id", "user_id", "userId")
                )
            if not row.get("from_username"):
                row["from_username"] = (
                    _get_any(user_payload, "unique_id", "display_id", "username")
                    or _extract_handle(event_user)
                )
            if not row.get("from_nickname"):
                row["from_nickname"] = (
                    _get_any(user_payload, "nickname", "nick_name", "nickName")
                    or _get_attr_any(event_user, "nickname", "nick_name", "nickName")
                )
        if not row.get("to_user_id") or not row.get("to_username") or not row.get("to_nickname"):
            receiver_payload = payload.get("receiver") if isinstance(payload.get("receiver"), dict) else {}
            to_user_payload = payload.get("to_user") if isinstance(payload.get("to_user"), dict) else {}
            event_to_user = _get_attr_any(event, "to_user", "receiver", "toUser")
            to_member_id = _get_any(payload, "to_member_id", "toMemberId")
            to_member_id_int = _get_any(payload, "to_member_id_int", "toMemberIdInt")
            to_member_nickname = _get_any(payload, "to_member_nickname", "toMemberNickname")
            describe = str(row.get("describe") or "")
            parsed_recipient = _extract_recipient_from_describe(describe)

            def pick_value(*values):
                for value in values:
                    if _is_valid_value(value):
                        return value
                return None

            if not row.get("to_user_id"):
                row["to_user_id"] = pick_value(
                    _get_any(payload, "to_user_id", "toUserId"),
                    _get_any(receiver_payload, "id", "user_id"),
                    _get_any(to_user_payload, "id", "user_id"),
                    _get_attr_any(event_to_user, "id", "user_id"),
                    gift_monitor_to_user_id,
                    row.get("anchor_id"),
                    to_member_id_int,
                    to_member_id,
                )
            if not row.get("to_username"):
                row["to_username"] = pick_value(
                    _get_any(payload, "to_user_name", "toUserName"),
                    _get_any(receiver_payload, "unique_id", "display_id", "username"),
                    _get_any(to_user_payload, "unique_id", "display_id", "username"),
                    _extract_handle(event_to_user),
                )
                if not row.get("to_username") and "gifted the host" in describe.lower():
                    row["to_username"] = tiktok_username
                if not row.get("to_username") and parsed_recipient:
                    candidate = parsed_recipient.lstrip("@")
                    if re.fullmatch(r"[A-Za-z0-9._]{2,}", candidate):
                        row["to_username"] = candidate
            if not row.get("to_nickname"):
                row["to_nickname"] = pick_value(
                    _get_any(payload, "to_user_nickname", "toNickname"),
                    _get_any(receiver_payload, "nickname", "nick_name", "nickName"),
                    _get_any(to_user_payload, "nickname", "nick_name", "nickName"),
                    _get_attr_any(event_to_user, "nickname", "nick_name", "nickName"),
                    to_member_nickname,
                )
                if not row.get("to_nickname") and parsed_recipient:
                    row["to_nickname"] = parsed_recipient
            if not row.get("to_user_id") and row.get("to_username") and tiktok_username:
                if row["to_username"].lstrip("@").lower() == tiktok_username.lstrip("@").lower():
                    anchor_candidate = row.get("anchor_id")
                    if _is_valid_value(anchor_candidate):
                        row["to_user_id"] = anchor_candidate
            if tiktok_username:
                host_name = tiktok_username.lstrip("@")
                if not row.get("to_username"):
                    row["to_username"] = host_name
                if not row.get("to_nickname"):
                    row["to_nickname"] = host_name
            if not row.get("to_user_id") and tiktok_username and row.get("to_username"):
                if row["to_username"].lstrip("@").lower() == tiktok_username.lstrip("@").lower():
                    anchor_candidate = row.get("anchor_id")
                    if _is_valid_value(anchor_candidate):
                        row["to_user_id"] = anchor_candidate
        to_member_id_int = row.get("to_member_id_int")
        if not _is_valid_value(to_member_id_int):
            to_member_id_int = _get_any(payload, "to_member_id", "toMemberId")
        if not _is_valid_value(to_member_id_int):
            to_member_id_int = row.get("to_user_id") or gift_monitor_to_user_id or row.get("anchor_id")
        row["to_member_id_int"] = to_member_id_int if _is_valid_value(to_member_id_int) else None
        to_member_nickname = row.get("to_member_nickname")
        if not _is_valid_value(to_member_nickname):
            to_member_nickname = row.get("to_nickname") or row.get("to_username")
        if not _is_valid_value(to_member_nickname) and describe_val:
            parsed_recipient = _extract_recipient_from_describe(str(describe_val))
            if parsed_recipient:
                to_member_nickname = parsed_recipient
        if not _is_valid_value(to_member_nickname) and describe_val:
            if "gifted the host" in str(describe_val).lower():
                to_member_nickname = tiktok_username
        row["to_member_nickname"] = to_member_nickname if _is_valid_value(to_member_nickname) else None
        if not (row.get("to_user_id") or row.get("to_username") or row.get("to_nickname")):
            debug = {}
            try:
                if isinstance(payload, dict):
                    debug["payload_to_user_id"] = _get_any(payload, "to_user_id", "toUserId")
                    debug["payload_to_user_name"] = _get_any(payload, "to_user_name", "toUserName")
                    debug["payload_to_nickname"] = _get_any(payload, "to_user_nickname", "toNickname")
                    debug["payload_to_member_id"] = _get_any(payload, "to_member_id", "toMemberId")
                    debug["payload_to_member_id_int"] = _get_any(payload, "to_member_id_int", "toMemberIdInt")
                    debug["payload_to_member_nickname"] = _get_any(payload, "to_member_nickname", "toMemberNickname")
                if event_to_user:
                    debug["event_to_user"] = {
                        "id": _get_attr_any(event_to_user, "id", "user_id"),
                        "unique_id": _extract_handle(event_to_user),
                        "nickname": _get_attr_any(event_to_user, "nickname", "nick_name", "nickName"),
                    }
                if base_message:
                    debug["base_message_id"] = _get_attr_any(base_message, "message_id", "messageId")
                    debug["base_message_room_id"] = _get_attr_any(base_message, "room_id", "roomId")
            except Exception:
                pass
            logger.info(
                "Gift still missing to_user fields after fallback: to_user_id=%s to_username=%s to_nickname=%s debug=%s",
                row.get("to_user_id"),
                row.get("to_username"),
                row.get("to_nickname"),
                _safe_json(debug),
            )
        elif not row.get("to_user_id"):
            logger.debug(
                "Gift missing to_user_id after fallback: to_username=%s to_nickname=%s",
                row.get("to_username"),
                row.get("to_nickname"),
            )
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping gift_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("gift_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("gift_events", row_data)
                    action = "insert-fallback"
                self._log_result("gift_events", action, resp)
            except Exception:
                logger.exception("Failed to log gift event")

    async def log_join(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())

        base_message = _get_attr_any(event, "base_message", "baseMessage")
        user_obj = _safe_event_user(event)

        user_payload = payload.get("user") if isinstance(payload, dict) and isinstance(payload.get("user"), dict) else {}
        follow_info = (
            user_payload.get("followInfo")
            if isinstance(user_payload, dict) and isinstance(user_payload.get("followInfo"), dict)
            else None
        )
        if follow_info is None and isinstance(user_payload, dict) and isinstance(user_payload.get("follow_info"), dict):
            follow_info = user_payload.get("follow_info")

        joined_user_id = (
            _get_attr_any(user_obj, "id", "user_id", "userId")
            or _get_any(user_payload, "id", "user_id", "userId")
            or _get_any(payload, "joined_user_id", "joinedUserId")
        )
        joined_user_nickname = (
            _get_attr_any(user_obj, "nickname", "nick_name", "nickName")
            or _get_any(user_payload, "nickName", "nick_name", "nickname")
            or _get_any(payload, "joined_user_nickname", "joinedUserNickname")
        )
        joined_username = (
            _extract_handle(user_obj)
            or _get_any(user_payload, "unique_id", "display_id", "username")
            or _get_any(payload, "joined_username", "joinedUsername")
        )
        joined_user_sec_uid = (
            _get_attr_any(user_obj, "sec_uid", "secUid", "user_sec_uid")
            or _get_any(user_payload, "secUid", "sec_uid", "user_sec_uid")
            or _get_any(payload, "joined_user_sec_uid", "joinedUserSecUid")
        )

        action = _get_any(payload, "action") or _get_attr_any(event, "action")
        count = _get_any(payload, "count") or _get_attr_any(event, "count")
        client_enter_source = _get_any(payload, "clientEnterSource", "client_enter_source")
        client_enter_type = _get_any(payload, "clientEnterType", "client_enter_type")

        public_area = payload.get("publicAreaMessageCommon") if isinstance(payload, dict) else None
        if public_area is None and isinstance(payload, dict):
            public_area = payload.get("public_area_message_common")
        portrait = public_area.get("portraitInfo") if isinstance(public_area, dict) else None
        if portrait is None and isinstance(public_area, dict):
            portrait = public_area.get("portrait_info")
        user_metrics = portrait.get("userMetrics") if isinstance(portrait, dict) else None
        if user_metrics is None and isinstance(portrait, dict):
            user_metrics = portrait.get("user_metrics")
        if not isinstance(user_metrics, list):
            user_metrics = []

        grade_metric = None
        subscribe_metric = None
        follow_metric = None
        fans_club_metric = None
        top_viewer_metric = None

        for metric in user_metrics:
            if not isinstance(metric, dict):
                continue
            metric_type = str(metric.get("type") or "").upper()
            metric_val = metric.get("metricsValue")
            if metric_val is None:
                metric_val = metric.get("metrics_value")
            metric_val_int = self._to_int(metric_val)
            metric_val_norm = metric_val_int if metric_val_int is not None else metric_val
            if "GRADE" in metric_type:
                grade_metric = metric_val_norm
            elif "SUBSCRIBE" in metric_type:
                subscribe_metric = metric_val_norm
            elif "FOLLOW" in metric_type:
                follow_metric = metric_val_norm
            elif "FANS_CLUB" in metric_type or "FANSCLUB" in metric_type:
                fans_club_metric = metric_val_norm
            elif "TOP_VIEWER" in metric_type:
                top_viewer_metric = metric_val_norm

        follow_role = _get_any(payload, "follow_role", "followRole")
        if not _is_valid_value(follow_role) and isinstance(follow_info, dict):
            follow_role = _get_any(
                follow_info,
                "follow_role",
                "followRole",
                "follow_status",
                "followStatus",
                "follow_state",
                "followState",
            )
        if not _is_valid_value(follow_role):
            follow_role = follow_metric
        if not _is_valid_value(follow_role):
            follow_role = None

        following_count = _get_any(follow_info, "followingCount", "following_count") if isinstance(follow_info, dict) else None
        follower_count = _get_any(follow_info, "followerCount", "follower_count") if isinstance(follow_info, dict) else None

        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "method": _get_any(payload, "method") or _get_attr_any(base_message, "method") or "WebcastMemberMessage",
            "room_id": _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId"),
            "create_time_ms": _get_any(payload, "create_time", "create_time_ms", "createTime", "timestamp")
            or _get_attr_any(base_message, "create_time", "createTime"),
            "message_id": _get_any(payload, "msg_id", "msgId", "message_id", "messageId", "event_id")
            or _get_attr_any(base_message, "message_id", "messageId"),
            "joined_user_id": joined_user_id,
            "joined_user_nickname": joined_user_nickname,
            "joined_username": joined_username,
            "joined_user_sec_uid": joined_user_sec_uid,
            "action": action,
            "count": count,
            "client_enter_source": client_enter_source,
            "client_enter_type": client_enter_type,
            "grade_metric": grade_metric,
            "subscribe_metric": subscribe_metric,
            "follow_metric": follow_metric,
            "fans_club_metric": fans_club_metric,
            "top_viewer_metric": top_viewer_metric,
            "follow_role": follow_role,
            "following_count": following_count,
            "follower_count": follower_count,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping join_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                # Keep json/jsonb payload as native JSON list for PostgREST.
                row_data["user_metrics"] = user_metrics
                on_conflict = "message_id,tiktok_username" if row_data.get("message_id") else None
                action_name = "upsert" if on_conflict else "insert"
                resp = await self._post_row("join_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("join_events", row_data)
                    action_name = "insert-fallback"
                self._log_result("join_events", action_name, resp)
            except Exception:
                logger.exception("Failed to log join event")

    async def log_comment(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        base_message = _get_attr_any(event, "base_message", "baseMessage")
        user_info = _get_attr_any(event, "user_info", "userInfo")
        comment_text = ""
        try:
            comment_text = getattr(event, "comment", "") or getattr(event, "content", "") or ""
        except Exception:
            comment_text = ""
        if not comment_text and isinstance(payload, dict):
            comment_text = _get_any(payload, "comment", "content", default="")
        user_obj = _safe_event_user(event)
        username = _extract_handle(user_obj)
        nickname = getattr(user_obj, "nickname", "") if user_obj else ""
        user_id = getattr(user_obj, "unique_id", "") if user_obj else ""
        user_sec_uid = ""
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            username = username or _get_any(user_payload, "unique_id", "display_id", "username")
            nickname = nickname or _get_any(user_payload, "nickname", "nick_name")
            user_id = user_id or _get_any(user_payload, "id", "user_id")
            user_sec_uid = _get_any(user_payload, "sec_uid", "user_sec_uid")
        follow_role = _get_any(payload, "follow_role", "followRole")
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            follow_info = user_payload.get("follow_info") if isinstance(user_payload.get("follow_info"), dict) else None
            if follow_info is None and isinstance(user_payload.get("followInfo"), dict):
                follow_info = user_payload.get("followInfo")
            follow_role = follow_role or _get_any(
                user_payload, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
            )
            if follow_info:
                follow_role = follow_role or _get_any(
                    follow_info, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
                )
        if user_info:
            username = username or _extract_handle(user_info)
            nickname = nickname or _get_attr_any(user_info, "nickname", "nick_name", "nickName")
            user_id = user_id or _get_attr_any(user_info, "id", "user_id")
            user_sec_uid = user_sec_uid or _get_attr_any(user_info, "sec_uid", "user_sec_uid")
            follow_role = follow_role or _get_attr_any(user_info, "follow_role", "followRole", "follow_status", "followStatus")
            follow_info_obj = _get_attr_any(user_info, "follow_info", "followInfo")
            follow_role = follow_role or _get_attr_any(
                follow_info_obj, "follow_role", "followRole", "follow_status", "followStatus"
            )
        if not _is_valid_value(follow_role):
            follow_role = None
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "comment",
            "room_id": _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId"),
            "create_time_ms": _get_any(payload, "create_time", "create_time_ms", "timestamp") or _get_attr_any(
                base_message, "create_time", "createTime"
            ),
            "message_id": _get_any(payload, "msg_id", "message_id", "event_id") or _get_attr_any(
                base_message, "message_id", "messageId"
            ),
            "comment_text": comment_text,
            "user_id": user_id,
            "username": username,
            "nickname": nickname,
            "user_sec_uid": user_sec_uid or _get_any(payload, "user_sec_uid", "sec_uid"),
            "follow_role": follow_role,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping comment_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("comment_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("comment_events", row_data)
                    action = "insert-fallback"
                self._log_result("comment_events", action, resp)
            except Exception:
                logger.exception("Failed to log comment event")

    async def log_like(self, payload: dict, event, tiktok_username: str, raw_id: Optional[int] = None) -> None:
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        user_obj = _safe_event_user(event)
        username = _extract_handle(user_obj)
        nickname = getattr(user_obj, "nickname", "") if user_obj else ""
        user_id = getattr(user_obj, "unique_id", "") if user_obj else ""
        user_sec_uid = _get_attr_any(user_obj, "sec_uid", "user_sec_uid")
        follow_role = _get_attr_any(user_obj, "follow_role", "followRole")
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            username = username or _get_any(user_payload, "unique_id", "display_id", "username")
            nickname = nickname or _get_any(user_payload, "nickname", "nick_name")
            user_id = user_id or _get_any(user_payload, "id", "user_id")
            user_sec_uid = user_sec_uid or _get_any(user_payload, "sec_uid", "user_sec_uid")
        if not username or not user_id:
            user_info = _get_attr_any(event, "user_info", "userInfo")
            if user_info:
                username = username or _extract_handle(user_info)
                nickname = nickname or _get_attr_any(user_info, "nickname", "nick_name", "nickName")
                user_id = user_id or _get_attr_any(user_info, "id", "user_id")
                user_sec_uid = user_sec_uid or _get_attr_any(user_info, "sec_uid", "user_sec_uid")
        base_message = _get_attr_any(event, "base_message", "baseMessage")
        room_id = _get_any(payload, "room_id", "roomId") or _get_attr_any(base_message, "room_id", "roomId")
        create_time_ms = _get_any(payload, "create_time", "create_time_ms", "timestamp") or _get_attr_any(
            base_message, "create_time", "createTime"
        )
        message_id = _get_any(payload, "msg_id", "message_id", "event_id") or _get_attr_any(
            base_message, "message_id", "messageId"
        )
        like_count = _get_any(payload, "like_count", "count", "likeCount") or _get_attr_any(
            event, "count", "like_count", "likeCount"
        )
        total_like_count = _get_any(payload, "total_like_count", "totalCount", "total_like") or _get_attr_any(
            event, "total", "total_like_count", "totalCount", "total_like"
        )
        user_sec_uid = user_sec_uid or _get_any(payload, "user_sec_uid", "sec_uid")
        follow_role = follow_role or _get_any(payload, "follow_role", "followRole")
        if isinstance(payload, dict):
            user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
            user_sec_uid = user_sec_uid or _get_any(user_payload, "sec_uid", "user_sec_uid")
            follow_info = user_payload.get("follow_info") if isinstance(user_payload.get("follow_info"), dict) else None
            if follow_info is None and isinstance(user_payload.get("followInfo"), dict):
                follow_info = user_payload.get("followInfo")
            follow_role = follow_role or _get_any(
                user_payload, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
            )
            if follow_info:
                follow_role = follow_role or _get_any(
                    follow_info, "follow_role", "followRole", "follow_status", "followStatus", "follow_state", "followState"
                )
        if not user_sec_uid or not follow_role:
            user_info = _get_attr_any(event, "user_info", "userInfo")
            if user_info:
                user_sec_uid = user_sec_uid or _get_attr_any(user_info, "sec_uid", "user_sec_uid")
                follow_role = follow_role or _get_attr_any(user_info, "follow_role", "followRole", "follow_status", "followStatus")
                follow_info_obj = _get_attr_any(user_info, "follow_info", "followInfo")
                follow_role = follow_role or _get_attr_any(
                    follow_info_obj, "follow_role", "followRole", "follow_status", "followStatus"
                )
        if not follow_role:
            public_area = None
            if isinstance(payload, dict):
                public_area = payload.get("public_area_message_common") or payload.get("publicAreaMessageCommon")
            if public_area is None:
                public_area = _get_attr_any(event, "public_area_message_common", "publicAreaMessageCommon")
            if public_area is None and base_message:
                public_area = _get_attr_any(base_message, "public_area_message_common", "publicAreaMessageCommon")

            def _extract_follow_from_metrics(pa_obj):
                if not pa_obj:
                    return None
                portrait = pa_obj.get("portrait_info") if isinstance(pa_obj, dict) else _get_attr_any(pa_obj, "portrait_info", "portraitInfo")
                metrics = None
                if isinstance(portrait, dict):
                    metrics = portrait.get("user_metrics") or portrait.get("userMetrics")
                else:
                    metrics = _get_attr_any(portrait, "user_metrics", "userMetrics")
                if not metrics:
                    return None
                for metric in metrics:
                    m_type = metric.get("type") if isinstance(metric, dict) else _get_attr_any(metric, "type")
                    m_value = metric.get("metrics_value") if isinstance(metric, dict) else _get_attr_any(metric, "metrics_value", "metricsValue")
                    if m_type is None:
                        continue
                    if "FOLLOW" in str(m_type).upper():
                        return m_value
                return None

            follow_role = follow_role or _extract_follow_from_metrics(public_area)
            if not follow_role:
                portrait = public_area.get("portrait_info") if isinstance(public_area, dict) else _get_attr_any(public_area, "portrait_info", "portraitInfo")
                tags = portrait.get("portrait_tag") if isinstance(portrait, dict) else _get_attr_any(portrait, "portrait_tag", "portraitTag")
                if tags:
                    for tag in tags:
                        show_val = tag.get("show_value") if isinstance(tag, dict) else _get_attr_any(tag, "show_value", "showValue")
                        if not show_val:
                            continue
                        lower_val = str(show_val).lower()
                        if "notfollower" in lower_val or "not_follower" in lower_val:
                            follow_role = 0
                            break
                        if "follower" in lower_val:
                            follow_role = 1
                            break
        if not _is_valid_value(follow_role):
            follow_role = None
        row = {
            "id": raw_id if raw_id is not None else self._next_row_id(),
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "event_type": "like",
            "room_id": room_id,
            "create_time_ms": create_time_ms,
            "message_id": message_id,
            "like_count": like_count,
            "total_like_count": total_like_count,
            "user_id": user_id,
            "username": username,
            "nickname": nickname,
            "user_sec_uid": user_sec_uid,
            "follow_role": follow_role,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping like_events insert")
                    return
                row_data = dict(zip(row.keys(), values))
                on_conflict = "message_id" if row_data.get("message_id") else None
                action = "upsert" if on_conflict else "insert"
                resp = await self._post_row("like_events", row_data, on_conflict=on_conflict)
                if on_conflict and self._response_missing_unique(resp):
                    resp = await self._post_row("like_events", row_data)
                    action = "insert-fallback"
                self._log_result("like_events", action, resp)
            except Exception:
                logger.exception("Failed to log like event")

    async def close(self) -> None:
        if not self._client:
            return
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = None

    async def log_event(
        self,
        event_type: str,
        payload: dict,
        tiktok_username: str,
        raw_id: Optional[int] = None,
    ) -> Optional[int]:
        if not self._write_tiktok_events_raw:
            if self._debug:
                logger.debug("Skipping tiktok_events_raw insert (disabled): event_type=%s", event_type)
            return None
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        unix_ts = int(now_dt.timestamp())
        event_id = raw_id if raw_id is not None else self._next_raw_id()
        payload_json = _safe_json(payload)
        payload_value = None
        msg_id_value = None
        try:
            parsed = json.loads(payload_json)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payload_value = parsed.get("payload")
            msg_id_value = _get_any(parsed, "msgId", "msg_id", "messageId", "message_id")
            if msg_id_value is None:
                base_message = parsed.get("baseMessage") or parsed.get("base_message")
                if isinstance(base_message, dict):
                    msg_id_value = _get_any(base_message, "msgId", "msg_id", "messageId", "message_id")
        if isinstance(msg_id_value, str):
            try:
                msg_id_value = int(msg_id_value)
            except Exception:
                pass
        if isinstance(payload_value, (bytes, bytearray)):
            try:
                payload_value = base64.b64encode(bytes(payload_value)).decode("ascii")
            except Exception:
                payload_value = None
        elif payload_value is not None and not isinstance(payload_value, str):
            payload_value = str(payload_value)
        row = {
            "id": event_id,
            "event_type": event_type,
            "iso_ts": ts,
            "unix_ts": unix_ts,
            "payload": payload_value,
            "msgId": msg_id_value,
            "tiktok_username": tiktok_username,
        }
        values = [_normalize_db_value(v) for v in row.values()]
        async with self._lock:
            try:
                if not self._client:
                    logger.debug("Supabase client unavailable; skipping tiktok_events_raw insert")
                    return None
                row_data = dict(zip(row.keys(), values))
                resp = await self._post_row("tiktok_events_raw", row_data)
                self._log_result("tiktok_events_raw", "insert", resp)
                if resp is None or not resp.is_success:
                    return None
                return event_id
            except Exception:
                logger.exception("Failed to log tiktok_events_raw")
                return None
