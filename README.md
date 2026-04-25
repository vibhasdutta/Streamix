<p align="center">
  <img src="src/shared/assets/streamix.jpeg" alt="Streamix Banner" width="700" />
</p>

<h1 align="center">STREAMIX</h1>

<p align="center">
  <strong>Stream Together. Right From Your Terminal.</strong>
</p>

<p align="center">
  <a href="https://github.com/VibhasDutta/streamix"><img src="https://img.shields.io/badge/version-v1.0.0-blue.svg" alt="Version" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.13+-blue.svg" alt="Python" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" /></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/badge/managed%20by-uv-black.svg" alt="Managed by UV" /></a>
</p>

---

Streamix is a **terminal-first streaming and watch party platform**. Host synchronized watch sessions with friends complete with **real-time voice chat**, **text messaging**, and **admin moderation** — or go solo and binge on your own. Everything runs from your terminal across Windows, macOS, and Linux.

## Why Streamix?

| | Solo | Watch Party |
|---|---|---|
| **Stream content** | Browse, search, and play instantly | Host syncs playback for everyone |
| **Voice chat** | — | Built-in voice with noise gate |
| **Text chat** | — | Full chat with notifications |
| **Moderation** | — | Mute, deafen, kick, ban by Hash-ID |
| **Auto-play** | Sequential episode playback | Synced auto-play across all clients |
| **Discord RPC** | Shows what you're watching | Shows party info and member count |

---

## Features

### Watch Party
- **Real-Time Voice Chat** — 24kHz audio with noise gate, loopback testing, and per-user volume controls
- **Synchronized Playback** — When the host plays, pauses, or seeks, every client follows via mpv IPC
- **Moderation Tools** — Hash-ID based muting, deafening, kicking, and banning that survives name changes
- **Global & Local Controls** — Admins manage the room; participants tune their own experience
- **Secure Tunneling** — Sessions are hosted via ngrok with automatic link generation and clipboard copy

### Solo Mode
- **Instant Search** — Find any series by title or browse trending/seasonal content
- **Sequential Auto-Play** — Episodes play back-to-back without touching the keyboard
- **Watch History** — Tracks progress and resume-points automatically
- **Character & Recommendation Browser** — Explore cast info and discover similar series

### Platform
- **Cross-Platform** — Windows, macOS, and Linux with OS-specific dependency detection
- **Startup Health Check** — Animated dependency checker validates uv, mpv, and ngrok on launch
- **Discord Rich Presence** — Shows real-time status with smart rate-limited updates
- **Premium TUI** — Clean, keyboard-driven interface powered by Rich and Questionary

---

## Installation

### Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| [uv](https://github.com/astral-sh/uv) | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [mpv](https://mpv.io/) | Video playback | `scoop install mpv` / `brew install mpv` / `sudo apt install mpv` |
| [ngrok](https://ngrok.com/) | Watch Party tunneling | `scoop install ngrok` / `brew install ngrok` / `snap install ngrok` |

### Setup

```bash
# Clone the repository
git clone https://github.com/VibhasDutta/streamix.git
cd streamix

# Install all Python dependencies
uv sync
```

### Ngrok Auth (Required for hosting Watch Parties)

```bash
ngrok config add-authtoken YOUR_TOKEN_HERE
```

Get your authtoken from the [ngrok dashboard](https://dashboard.ngrok.com/get-started/setup).

---

## Usage

```bash
uv run src/main.py
```

Choose **Solo** to watch alone or **Party** to host/join a watch session.

### Watch Party Shortcuts (Host & Client)

| Key | Action |
|---|---|
| `Ctrl+K` | Toggle microphone mute |
| `Ctrl+T` | Toggle speaker deafen |
| `Ctrl+N` | Toggle notification sounds |
| `Page Up / Down` | Scroll chat history |

---

## Coming Soon

> [!NOTE]
> Standalone executables are in progress for:
> - Windows (.exe)
> - macOS (.app / Homebrew)
> - Linux (.deb / AppImage)
>
> *Stay tuned for v1.1.0!*

---

## Star History

<a href="https://www.star-history.com/?repos=VibhasDutta%2Fanilix&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=vibhasdutta/anilix&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=vibhasdutta/anilix&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=vibhasdutta/anilix&type=date&legend=top-left" />
 </picture>
</a>

---

## Acknowledgements

- [Miruro-API](https://github.com/walterwhite-69/Miruro-API) — Anime metadata backend
- [ani-cli](https://github.com/pystardust/ani-cli) — Terminal workflow inspiration
---

## License

MIT License. **Built with love by Vibhas Dutta & Abhinav.**
