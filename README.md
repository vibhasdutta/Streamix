# 🎬 Streamix: The Ultimate Terminal Watch Party Experience ✨

[![Version](https://img.shields.io/badge/version-v1.0.0-blue.svg)](https://github.com/VibhasDutta/streamix)
[![Python](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-blueviolet.svg)](https://github.com/Textualize/textual)
[![Managed by UV](https://img.shields.io/badge/managed%20by-uv-black.svg)](https://github.com/astral-sh/uv)

Streamix is a high-performance **Terminal Anime Interface & Watch Party System** designed for enthusiasts who want a professional-grade, keyboard-driven experience. Browse, stream, and sync your favorite series with friends—all from the comfort of your terminal.

---

## 🚀 Features

- **📺 Premium Terminal UI**: A sleek, keyboard-driven dashboard powered by `Rich` and `Questionary`.
- **🤝 Advanced Watch Parties**:
  - **Voice & Chat**: Integrated real-time voice chat and text messaging.
  - **Surgical Moderation**: Hash-ID based moderation (Mute/Deafen/Ban) that tracks unique IDs instead of names to prevent evasion.
  - **Global & Local Controls**: Administrators can manage the whole room, while participants can tune their own experience.
- **🔄 Real-time Sync**: Precise `mpv` synchronization using IPC—when the host pauses, everyone pauses.
- **🔍 Intelligent Search & Discovery**: Find any series instantly or explore seasonal hits.
- **🎥 Pro-Grade Playback**:
  - **Sequential Auto-Play**: Seamlessly watch following episodes without touching the keyboard.
  - **Hardenened Connection**: Aggressive URL sanitization and diagnostic logic for 99.9% uptime on ngrok tunnels.
- **📚 Persistent History**: Automatically tracks progress and resume-points across devices.

---

## 🛠️ Installation & Setup

### 1. Install Dependencies
We recommend using [uv](https://github.com/astral-sh/uv) for lightning-fast package management.

```bash
# Clone the repository
git clone https://github.com/VibhasDutta/streamix.git
cd streamix

# Install all pip packages automatically
uv sync
```

### 2. Install MPV (Required)
Streamix uses `mpv` for high-performance video streaming.

- **Windows**: `choco install mpv`
- **macOS**: `brew install mpv`
- **Linux**: `sudo apt install mpv`

### 3. Setup Networking (For Watch Parties)
To host a party, you need an [ngrok](https://ngrok.com/) account for tunneling.

1.  [Download & Install ngrok](https://dashboard.ngrok.com/get-started/setup).
2.  Get your **Authtoken** from the dashboard.
3.  Run: `ngrok config add-authtoken YOUR_TOKEN_HERE`

---

## 📦 Coming Soon
> [!NOTE]
> We are working on providing standalone **executables/packages** for:
> - 🪟 Windows (.exe)
> - 🍎 macOS (.app / Homebrew)
> - 🐧 Linux (.deb / AppImage)
> 
> *Stay tuned for the v1.1.0 release!*

---

## 🎮 Running Streamix

Simply launch the main dashboard:
```bash
uv run src/main.py
```

---

## 📈 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=VibhasDutta/streamix&type=Date)](https://star-history.com/#VibhasDutta/streamix&Date)

---

## 🤝 Special Thanks

- [Miruro-API](https://github.com/walterwhite-69/Miruro-API) - High-speed anime metadata.
- [ani-cli](https://github.com/pystardust/ani-cli) - Inspirational terminal workflow.
- [Textual](https://github.com/Textualize/textual) - Beautiful TUI development.

---

## ⚖️ License
MIT License. **Built with 💖 by Vibhas Dutta.** Enjoy your anime! 🍿✨
