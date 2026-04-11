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

## 📈 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=VibhasDutta/anilix&type=Date)](https://star-history.com/#VibhasDutta/anilix&Date)

## 🤝 Special Thanks

- [Miruro-API](https://github.com/walterwhite-69/Miruro-API)
- [ani-cli](https://github.com/pystardust/ani-cli)

## ⚖️ License
MIT License. Enjoy your anime! 🍿
