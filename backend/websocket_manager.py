import asyncio
import json
import logging
from typing import List

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebsocketManager:
    def __init__(self) -> None:
        self.connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections.append(websocket)
        logger.info("WebSocket connected (%s total)", len(self.connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self.connections:
                self.connections.remove(websocket)
        logger.info("WebSocket disconnected (%s total)", len(self.connections))

    async def broadcast(self, message: dict) -> None:
        if not self.connections:
            return
        payload = json.dumps(message)
        to_remove = []
        for ws in list(self.connections):
            try:
                await ws.send_text(payload)
            except Exception as exc:
                logger.debug("WebSocket send failed: %s", exc)
                to_remove.append(ws)
        if to_remove:
            async with self._lock:
                for ws in to_remove:
                    if ws in self.connections:
                        self.connections.remove(ws)

