# Anilix: The Ultimate Terminal Anime Interface ✨

[![Version](https://img.shields.io/badge/version-v1.0.0-blue.svg)](https://github.com/VibhasDutta/anilix)
[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Built with Rich](https://img.shields.io/badge/built%20with-Rich-blueviolet.svg)](https://github.com/Textualize/rich)
[![Managed by UV](https://img.shields.io/badge/managed%20by-uv-black.svg)](https://github.com/astral-sh/uv)
[![Required mpv](https://img.shields.io/badge/required-mpv-red.svg)](https://mpv.io/)

Anilix is a high-performance **Terminal Anime Interface** that allows you to browse, discover, and **watch anime in terminal** with a keyboard-driven, professional-grade experience.

## 🚀 Features

- **📺 Premium Terminal Anime Interface**: A keyboard-driven dashboard designed for terminal enthusiasts.
- **🔍 Intelligent Search**: Find series instantly using integrated search.
- **🔥 Trending & Discovery**: Stay updated with the latest seasonal hits.
- **🎥 Watch Anime in Terminal**:
  - **Sequential Auto-Play**: Binge-watch your favorite shows with a zero-interaction playback loop.
  - **Native mpv Integration**: Reliable, high-performance streaming for Windows, Linux, and macOS.
- **📚 Persistent History**: Automatically tracks your watch history and allows you to resume episodes exactly where you left off.
- **✨ Rich UI**: A beautiful, interactive terminal interface powered by the `rich` library.
- **🛠️ Zero Config Backend**: Automatically manages a local FastAPI backend server.

## 🛠️ Installation & Setup

### 1. Prerequisites
- **Python**: 3.13 or higher.
- **Video Player**: [mpv](https://mpv.io/) is **mandatory**.

### 2. Install & Run
Clone the repository and use `uv` for lightning-fast setup:

```bash
# Clone the repository
git clone https://github.com/VibhasDutta/anilix.git
cd anilix

# Install dependencies & run
uv sync
uv run anilix.py
```

### 🚀 Global Command (Linux/macOS)
```bash
chmod +x setup.sh
./setup.sh install
```

## 📁 Project Structure

- `anilix.py`: Main CLI (v1.0.0).
- `anilix_server.py`: FastAPI backend.
- `recent_watch.json`: Persistent history database.

## 🖥️ Runtime OS Detection Utility

Anilix includes a singleton-style OS detector module that resolves the OS once
at import time and exposes enum values, booleans, and raw platform metadata.

```python
from utils.os_detector import IS_MACOS, IS_WINDOWS, current_os, OS

if current_os is OS.WINDOWS:
  print("Windows-specific logic")
elif current_os is OS.MACOS:
  print("macOS-specific logic")
elif current_os is OS.LINUX:
  print("Linux-specific logic")
```

Boolean flags are available for quick guards:

```python
from utils.os_detector import IS_LINUX, IS_MACOS, IS_WINDOWS

if IS_WINDOWS:
  use_windows_pipe()
elif IS_MACOS:
  use_pbcopy_clipboard()
elif IS_LINUX:
  use_xclip_or_wlcopy()
```

Callable accessor alternative:

```python
from utils.os_detector import get_os, OS

if get_os() is OS.LINUX:
  print("Running on Linux")
```

Raw values for logging/debugging:

```python
from utils.os_detector import RAW_OS_NAME, RAW_OS_RELEASE, RAW_OS_VERSION

print(f"OS={RAW_OS_NAME} release={RAW_OS_RELEASE} version={RAW_OS_VERSION}")
```

Optional startup integration in an entry point:

```python
from utils.os_detector import RAW_OS_NAME, RAW_OS_RELEASE, RAW_OS_VERSION, current_os

def main() -> None:
  print(
    f"Detected OS: {current_os.value} | "
    f"raw={RAW_OS_NAME} {RAW_OS_RELEASE} | "
    f"version={RAW_OS_VERSION}"
  )
  # Continue startup...
```

## 📈 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=VibhasDutta/anilix&type=Date)](https://star-history.com/#VibhasDutta/anilix&Date)

## 🤝 Special Thanks

- [Miruro-API](https://github.com/walterwhite-69/Miruro-API)
- [ani-cli](https://github.com/pystardust/ani-cli)

## ⚖️ License
MIT License. Enjoy your anime! 🍿
