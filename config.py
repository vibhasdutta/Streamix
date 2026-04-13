"""
Party Config — Persistent settings for Watch Party admin and client TUIs.
Stored in party_config.json in the project root.
"""
import json
import os

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(CONFIG_DIR, "party_config.json")

DEFAULT_CONFIG = {
    "admin": {
        "volume": 100,
        "notifications": True,
        "default_room_name": "",
        "default_host_name": "",
        "chat_history_limit": 50,
        "sync_interval_seconds": 1,
        "mic_device_index": None
    },
    "client": {
        "volume": 100,
        "notifications": True,
        "default_username": "",
        "chat_history_limit": 50,
        "mic_device_index": None
    }
}

def load_config():
    """Load config from disk, merging with defaults for any missing keys."""
    config = _deep_copy(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge saved values into defaults
            for section in ["admin", "client"]:
                if section in saved:
                    for key, val in saved[section].items():
                        if key in config[section]:
                            config[section][key] = val
    except Exception:
        pass
    return config

def save_config(config):
    """Save config to disk."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass

def get_admin_config():
    """Get admin section of config."""
    return load_config()["admin"]

def get_client_config():
    """Get client section of config."""
    return load_config()["client"]

def update_admin_config(**kwargs):
    """Update specific admin config keys and save."""
    config = load_config()
    for key, val in kwargs.items():
        if key in config["admin"]:
            config["admin"][key] = val
    save_config(config)
    return config["admin"]

def update_client_config(**kwargs):
    """Update specific client config keys and save."""
    config = load_config()
    for key, val in kwargs.items():
        if key in config["client"]:
            config["client"][key] = val
    save_config(config)
    return config["client"]

def _deep_copy(d):
    """Simple deep copy for nested dicts."""
    return json.loads(json.dumps(d))
