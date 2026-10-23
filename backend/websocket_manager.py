import asyncio
import json
import logging
from typing import Dict, List

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebsocketManager:
    """Live-update sockets, grouped by dance group so each group only sees its own state."""

    def __init__(self) -> None:
        self.connections: Dict[str, List[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, group_id: str) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections.setdefault(group_id, []).append(websocket)
            count = len(self.connections[group_id])
        logger.debug("WebSocket connected (group=%s, %s total)", group_id, count)

    async def disconnect(self, websocket: WebSocket, group_id: str) -> None:
        async with self._lock:
            sockets = self.connections.get(group_id, [])
            if websocket in sockets:
                sockets.remove(websocket)
            if not sockets:
                self.connections.pop(group_id, None)

    async def broadcast(self, message: dict, group_id: str) -> None:
        sockets = list(self.connections.get(group_id, []))
        if not sockets:
            return
        payload = json.dumps(message)
        to_remove = []
        for ws in sockets:
            try:
                await ws.send_text(payload)
            except Exception as exc:
                logger.debug("WebSocket send failed: %s", exc)
                to_remove.append(ws)
        if to_remove:
            async with self._lock:
                remaining = self.connections.get(group_id, [])
                for ws in to_remove:
                    if ws in remaining:
                        remaining.remove(ws)
