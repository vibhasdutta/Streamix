from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
SHARED_DIR = SRC_DIR / "shared"
ASSETS_DIR = SHARED_DIR / "assets"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
PARTY_INFO_PATH = DATA_DIR / "party_info.json"
PARTY_CONFIG_PATH = DATA_DIR / "party_config.json"
BANNER_PATH = ASSETS_DIR / "banner.txt"
SOUND_ASSETS_DIR = PROJECT_ROOT / "sound_assets"


def ensure_data_directories():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)