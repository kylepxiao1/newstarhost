import logging
from typing import Optional

import backend.config as config

try:
    from obswebsocket import obsws, requests  # type: ignore
except ImportError:  # pragma: no cover - runtime guard
    obsws = None
    requests = None

logger = logging.getLogger(__name__)


class OBSController:
    def __init__(self) -> None:
        self._host = config.OBS_HOST
        self._port = config.OBS_PORT
        self._password = config.OBS_PASSWORD
        self._client: Optional["obsws"] = None
        self._failed = False

    def connect(self) -> None:
        if obsws is None:
            logger.warning("obs-websocket-py not installed; OBS control disabled.")
            return
        if self._client:
            return
        if self._failed:
            return
        self._client = obsws(self._host, self._port, self._password)
        try:
            self._client.connect()
            logger.info("Connected to OBS at %s:%s", self._host, self._port)
        except Exception as exc:  # pragma: no cover - network guard
            logger.error("Failed to connect to OBS: %s", exc)
            self._client = None
            self._failed = True

    def disconnect(self) -> None:
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                logger.debug("OBS disconnect suppressed")
        self._client = None

    def _ensure(self) -> bool:
        if self._client is None:
            self.connect()
        return self._client is not None

    def set_scene(self, scene_name: str) -> None:
        if not self._ensure():
            return
        try:
            self._client.call(requests.SetCurrentProgramScene(scene_name))
        except Exception as exc:
            logger.error("Failed to switch scene to %s: %s", scene_name, exc)

    def set_text(self, source_name: str, text: str) -> None:
        if not self._ensure():
            return
        try:
            self._client.call(requests.SetInputSettings(source_name, {"text": text}, True))
        except Exception as exc:
            logger.error("Failed to set text for %s: %s", source_name, exc)

    def set_visibility(self, scene_name: str, source_name: str, visible: bool) -> None:
        if not self._ensure():
            return
        try:
            # obs-websocket-py 1.0: use keyword args
            self._client.call(requests.SetSceneItemRender(item=source_name, render=visible))
        except Exception as exc:
            logger.error("Failed to set visibility for %s: %s", source_name, exc)

    def ensure_capture_source_visible(self, scene_name: str, source_name: str) -> None:
        """Enable the camera capture source if present."""
        self.set_visibility(scene_name, source_name, True)

    def refresh_scoreboard(self, left: str, right: str, left_score: int, right_score: int) -> None:
        # Update both names and scores for clarity
        self.set_text(config.TEXT_SOURCE_LEFT_NAME, left)
        self.set_text(config.TEXT_SOURCE_RIGHT_NAME, right)
        self.set_text(config.TEXT_SOURCE_LEFT_SCORE, str(left_score))
        self.set_text(config.TEXT_SOURCE_RIGHT_SCORE, str(right_score))

