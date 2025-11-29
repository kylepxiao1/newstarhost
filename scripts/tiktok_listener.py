import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive import events as ttevents

API_BASE = os.environ.get("BATTLE_API", "http://127.0.0.1:8000")
TIKTOK_USERNAME = os.environ.get("TIKTOK_USERNAME", "zerokomodo")
LOG_FILE = os.environ.get("LOG_FILE", "tiktok_events.log")
DEFAULT_SLOT_ONE = os.environ.get("SLOT_ONE_NAME", "Performer One")
DEFAULT_SLOT_TWO = os.environ.get("SLOT_TWO_NAME", "Performer Two")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger("tiktok-listener")


def looks_like_battle_start(comment: Optional[str], gift_name: Optional[str]) -> bool:
    text = (comment or "").lower()
    if "!battle" in text or "start battle" in text:
        return True
    if gift_name and "battle" in gift_name.lower():
        return True
    return False


def looks_like_battle_end(comment: Optional[str], gift_name: Optional[str]) -> bool:
    text = (comment or "").lower()
    if "!end" in text or "end battle" in text or "gg" == text.strip():
        return True
    if gift_name and "whistle" in gift_name.lower():
        return True
    return False


class TikTokBattleListener:
    def __init__(self, username: str, api_base: str) -> None:
        self.client = TikTokLiveClient(unique_id=username)
        self.api_base = api_base.rstrip("/")
        self.http = httpx.AsyncClient(timeout=10)
        self._last_start = datetime.min
        self._last_end = datetime.min
        self._cooldown = timedelta(seconds=30)
        self._last_scores = {"slot_one": 0, "slot_two": 0}
        self._wire_events()

    def _wire_events(self) -> None:
        @self.client.on(ttevents.ConnectEvent)
        async def on_connect(_: ttevents.ConnectEvent) -> None:
            logger.info("Connected to TikTok LIVE as %s", self.client.room_info.host.display_id)

        @self.client.on(ttevents.DisconnectEvent)
        async def on_disconnect(_: ttevents.DisconnectEvent) -> None:
            logger.warning("Disconnected from TikTok LIVE. Reconnecting...")

        @self.client.on(ttevents.LinkMicBattleEvent)
        async def on_battle(event: ttevents.LinkMicBattleEvent) -> None:
            logger.info("LinkMicBattleEvent: %s", json.dumps(event.as_dict()))
            self._last_scores = {"slot_one": 0, "slot_two": 0}
            await self.trigger_start("linkmic_battle_event")

        @self.client.on(ttevents.LinkMicArmiesEvent)
        async def on_armies(event: ttevents.LinkMicArmiesEvent) -> None:
            data = event.as_dict()
            armies = data.get("armies") or data.get("army_list") or data.get("battle_armies") or []
            if isinstance(armies, dict):
                armies = armies.get("armies") or armies.get("army_list") or list(armies.values())
            slot_one_score = self._last_scores["slot_one"]
            slot_two_score = self._last_scores["slot_two"]
            if isinstance(armies, list):
                if len(armies) > 0:
                    slot_one_score = armies[0].get("points") or armies[0].get("score") or slot_one_score
                if len(armies) > 1:
                    slot_two_score = armies[1].get("points") or armies[1].get("score") or slot_two_score
            await self._update_scores(slot_one_score, slot_two_score)

        @self.client.on(ttevents.GiftEvent)
        async def on_gift(event: ttevents.GiftEvent) -> None:
            payload = event.as_dict()
            logger.info("Gift event: %s", json.dumps(payload))
            name = event.gift.name if hasattr(event, "gift") else None
            await self._maybe_trigger(comment=None, gift_name=name)

        @self.client.on(ttevents.CommentEvent)
        async def on_comment(event: ttevents.CommentEvent) -> None:
            payload = event.as_dict()
            logger.info("Comment event: %s", json.dumps(payload))
            text = event.comment
            if text.startswith("!battle"):
                await self.trigger_start("command")
            elif text.startswith("!end"):
                await self.trigger_end("command")
            elif text.startswith("!slots"):
                parts = text.replace("!slots", "", 1).strip().split("|")
                first = parts[0].strip() if parts and parts[0].strip() else DEFAULT_SLOT_ONE
                second = parts[1].strip() if len(parts) > 1 and parts[1].strip() else DEFAULT_SLOT_TWO
                await self.import_slots(first, second)
            else:
                await self._maybe_trigger(comment=text, gift_name=None)

        @self.client.on(ttevents.LikeEvent)
        async def on_like(event: ttevents.LikeEvent) -> None:
            payload = event.as_dict()
            logger.info("Like event: %s", json.dumps(payload))

    async def _maybe_trigger(self, comment: Optional[str], gift_name: Optional[str]) -> None:
        now = datetime.now(timezone.utc)
        if looks_like_battle_start(comment, gift_name) and now - self._last_start > self._cooldown:
            await self.trigger_start("heuristic")
        elif looks_like_battle_end(comment, gift_name) and now - self._last_end > self._cooldown:
            await self.trigger_end("heuristic")

    async def trigger_start(self, reason: str) -> None:
        self._last_start = datetime.now(timezone.utc)
        self._last_scores = {"slot_one": 0, "slot_two": 0}
        logger.info("Triggering battle start (%s)", reason)
        try:
            await self.http.post(f"{self.api_base}/battle/start", json={})
            await self.import_slots(DEFAULT_SLOT_ONE, DEFAULT_SLOT_TWO)
        except Exception as exc:
            logger.error("Failed to call battle/start: %s", exc)

    async def trigger_end(self, reason: str) -> None:
        self._last_end = datetime.now(timezone.utc)
        logger.info("Triggering battle end (%s)", reason)
        try:
            await self.http.post(f"{self.api_base}/battle/end")
        except Exception as exc:
            logger.error("Failed to call battle/end: %s", exc)

    async def import_slots(self, slot_one: Optional[str], slot_two: Optional[str]) -> None:
        try:
            await self.http.post(f"{self.api_base}/battle/slots/import", json={"slot_one": slot_one, "slot_two": slot_two})
            logger.info("Imported slots: %s vs %s", slot_one, slot_two)
        except Exception as exc:
            logger.error("Failed to import slots: %s", exc)

    async def _update_scores(self, slot_one_score: int, slot_two_score: int) -> None:
        # Compute deltas and push increments to backend state
        delta_one = max(0, int(slot_one_score) - self._last_scores.get("slot_one", 0))
        delta_two = max(0, int(slot_two_score) - self._last_scores.get("slot_two", 0))
        self._last_scores["slot_one"] = int(slot_one_score)
        self._last_scores["slot_two"] = int(slot_two_score)
        try:
            if delta_one:
                await self.http.post(f"{self.api_base}/score/slot_one/add", json={"amount": delta_one})
            if delta_two:
                await self.http.post(f"{self.api_base}/score/slot_two/add", json={"amount": delta_two})
        except Exception as exc:
            logger.error("Failed to update scores: %s", exc)

    async def run(self) -> None:
        while True:
            try:
                await self.client.start()
            except Exception as exc:
                logger.error("TikTok listener error: %s", exc)
            await asyncio.sleep(5)


async def main() -> None:
    listener = TikTokBattleListener(TIKTOK_USERNAME, API_BASE)
    await listener.run()


if __name__ == "__main__":
    asyncio.run(main())
