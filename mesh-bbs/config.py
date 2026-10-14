"""
config.py

Loads config.yaml (in the same directory, or wherever MESHBBS_CONFIG
points) into a single CONFIG dict, merged over built-in defaults — so a
missing file, or a config.yaml that only overrides a few values, still
works fine rather than crashing on a missing key.

Every other module imports CONFIG from here instead of hardcoding values,
e.g.:  from config import CONFIG
       MODEM_PORT = CONFIG["modem"]["port"]

Requires:  pip install pyyaml
"""

import os

import yaml

_CONFIG_PATH = os.environ.get(
    "MESHBBS_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
)

_DEFAULTS = {
    "modem": {
        "port": "/dev/modem",
        "idle_baud": 19200,
        "connect_baud": 300,
        "init_strings": [
            "ATZ", "ATE0", "ATS0=1", "AT&C1&D2",
            "AT&N1", "AT&U0", "ATB1", "AT&B0", "AT&K0",
        ],
    },
    "radio": {
        "backend": "meshtastic",  # "meshtastic" or "meshcore"
        "port": "/dev/meshradio",
        "baud": 115200,           # only used by the meshcore backend
        "channel_idx": 0,         # only used by the meshcore backend
        "db_path": "/root/meshbbs/messages.db",
        "send_timeout": 15,
    },
    "bbs": {
        "term_width": 40,
        "banner_title": "=== MeshBBS - phillymesh.net ===",
        "banner_subtitle": "Messages traveling over LoRa mesh radio.",
    },
    "logging": {
        "log_file": "/root/meshbbs/bbs.log",
    },
}


def _deep_merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load():
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH) as f:
            user_config = yaml.safe_load(f) or {}
    else:
        user_config = {}
    return _deep_merge(_DEFAULTS, user_config)


CONFIG = _load()
