import os

# OBS connection details
OBS_HOST = os.environ.get("OBS_HOST", "localhost")
OBS_PORT = int(os.environ.get("OBS_PORT", 4455))
OBS_PASSWORD = os.environ.get("OBS_PASSWORD", "changeme")
CACHE_CAMERA_SOURCE = os.environ.get("CAMERA_SOURCE_NAME", "Video Capture Device")

# Scene/source naming conventions inside OBS
SCENE_MAIN = os.environ.get("SCENE_MAIN", "MainScene")
SCENE_BATTLE = os.environ.get("SCENE_BATTLE", "BattleScene")

TEXT_SOURCE_LEFT_NAME = os.environ.get("TEXT_SOURCE_LEFT_NAME", "LeftName")
TEXT_SOURCE_RIGHT_NAME = os.environ.get("TEXT_SOURCE_RIGHT_NAME", "RightName")
TEXT_SOURCE_LEFT_SCORE = os.environ.get("TEXT_SOURCE_LEFT_SCORE", "LeftScore")
TEXT_SOURCE_RIGHT_SCORE = os.environ.get("TEXT_SOURCE_RIGHT_SCORE", "RightScore")

# Overlay sources that can be toggled on/off
OVERLAY_SOURCES = os.environ.get(
    "OVERLAY_SOURCES",
    "BattleScore,BurstOverlay,CenterDottedLine",
).split(",")

# API / Websocket config
WEBSOCKET_PATH = "/ws/state"
API_PREFIX = ""

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

