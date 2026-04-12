# Streamix: The Ultimate Terminal Watch Party Experience ✨

[![Version](https://img.shields.io/badge/version-v1.1.0-blue.svg)](https://github.com/VibhasDutta/anilix)
[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-blueviolet.svg)](https://github.com/Textualize/textual)
[![Managed by UV](https://img.shields.io/badge/managed%20by-uv-black.svg)](https://github.com/astral-sh/uv)
[![Required mpv](https://img.shields.io/badge/required-mpv-red.svg)](https://mpv.io/)

Streamix is a high-performance **Terminal Anime Interface & Watch Party System** that allows you to browse, discover, and **watch anime together** with a keyboard-driven, professional-grade experience.

## 🚀 Features

- **📺 Premium Terminal UI**: A keyboard-driven dashboard powered by `Rich` and `Questionary`.
- **🤝 Integrated Watch Parties**:
  - **Textual-based TUI**: A modern, interactive chat and sync interface built with `Textual`.
  - **Real-time Sync**: Host controls playback for all participants via `mpv` IPC.
  - **Built-in Chat**: Communicate seamlessly during playback.
- **🔍 Intelligent Search**: Find series instantly using integrated search.
- **🔥 Trending & Discovery**: Stay updated with the latest seasonal hits.
- **🎥 Pro-Grade Playback**:
  - **Sequential Auto-Play**: Binge-watch with zero-interaction playback loops.
  - **Native mpv Integration**: Reliable, high-performance streaming across all platforms.
- **📚 Persistent History**: Automatically tracks progress and allows you to resume exactly where you left off.
- **🛠️ Zero Config Backend**: Automatically manages a local FastAPI backend and WebSocket server.

## 🛠️ Installation & Setup

### 1. Prerequisites
- **Python**: 3.13 or higher.
- **Video Player**: [mpv](https://mpv.io/) is **mandatory**.

### 2. Install & Run
Clone the repository and use `uv` for setup:

```bash
# Clone the repository
git clone https://github.com/VibhasDutta/anilix.git
cd anilix

# Install dependencies & run
uv sync
uv run main.py
```

## 📁 Project Structure

- `main.py`: Main CLI entry point.
- `backend.py`: FastAPI backend server.
- `party.py`: WebSocket synchronization server.
- `host.py`: Admin/Host Textual TUI.
- `client.py`: Client/Member Textual TUI.
- `recent_watch.json`: Persistent history database.

## 📈 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=VibhasDutta/anilix&type=Date)](https://star-history.com/#VibhasDutta/anilix&Date)

## 🤝 Special Thanks

- [Miruro-API](https://github.com/walterwhite-69/Miruro-API)
- [ani-cli](https://github.com/pystardust/ani-cli)

## ⚖️ License
MIT License. Enjoy your anime! 🍿
