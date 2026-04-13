from shared.utils.logger import install_asyncio_exception_handler, setup_logger
import asyncio
import requests
import os
from shared.media import get_mpv_path, get_streaming_headers
import json
import time
import sys
import subprocess
import threading
import signal
import atexit
import shutil
import platform
import hashlib
import shlex
import getpass
from core.config import (
    load_config, 
    update_admin_config, 
    update_client_config,
    get_admin_config,
    get_client_config
)
from core.paths import BANNER_PATH, CACHE_DIR, DATA_DIR, LOGS_DIR, PARTY_INFO_PATH, ensure_data_directories
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt
from rich.align import Align
from rich.text import Text
from rich.rule import Rule
from rich.layout import Layout
from rich.live import Live
from rich.spinner import Spinner
from rich.columns import Columns
from rich import box
import questionary
from questionary import Style as QStyle
from contextlib import contextmanager
from shared.utils.os_detector import (
    IS_MACOS,
    IS_WINDOWS,
    OS,
    RAW_OS_NAME,
    RAW_OS_RELEASE,
    RAW_OS_VERSION,
    current_os,
)

# Initialize centralized lifecycle logger
logger = setup_logger("main_hub", "streamix_backend.log")

API_BASE = "http://localhost:8000"
ensure_data_directories()
CACHE_FILE = DATA_DIR / "recent_watch.json"

console = Console()
backend_process = None
party_proc = None
active_subprocesses = []
VERSION = "1.0.0"
PROJECT_NAME = "STREAMIX"
# Flags to tell monitor when we are actually in a party session
party_active_flag = threading.Event()

# ── Premium Questionary Theme ──
QSTYLE = QStyle([
    ("qmark",        "fg:#E040FB bold"),       # Purple question mark
    ("question",     "fg:#FFFFFF bold"),        # White question text  
    ("answer",       "fg:#00E5FF bold"),        # Cyan confirmed answer
    ("pointer",      "fg:#E040FB bold"),        # Purple arrow ❯
    ("highlighted",  "fg:#E040FB bold"),        # Highlighted hover
    ("selected",     "fg:#00E5FF"),             # Multi-select check
    ("separator",    "fg:#555555"),             # Dim separators
    ("instruction",  "fg:#777777 italic"),      # Dim instructions
    ("text",         "fg:#CCCCCC"),             # Default text
])

def _trunc(text, width=35):
    """Truncate text to width with ellipsis."""
    if not text:
        return ""
    return (text[:width-1] + "…") if len(text) > width else text

def fetch_json(url, params=None, ttl_hours=24):
    """Fetch JSON from API with persistent disk caching."""
    # Create unique key for this request
    key_str = f"{url}:{json.dumps(params, sort_keys=True)}"
    cache_key = hashlib.md5(key_str.encode()).hexdigest()
    cache_path = CACHE_DIR / f"{cache_key}.json"
    
    # Check if cache exists and is fresh
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                cached_data = json.load(f)
            
            timestamp = cached_data.get("_cache_ts", 0)
            if (time.time() - timestamp) < (ttl_hours * 3600):
                return cached_data.get("data")
        except:
            pass # Fallback to network on corrupt cache

    # Fetch from network
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # Save to cache
        with open(cache_path, "w") as f:
            json.dump({"_cache_ts": time.time(), "data": data}, f)
            
        return data
    except Exception as e:
        # If network fails but we have (even expired) cache, use it as fallback
        if cache_path.exists():
            try:
                with open(cache_path, "r") as f:
                    return json.load(f).get("data")
            except:
                pass
        raise e

@contextmanager
def status_after(text, center=False):
    """Simplified helper to show a spinner AFTER the status text."""
    spinner = Spinner("aesthetic", style="cyan")
    display = Columns([text, spinner], padding=1, expand=False)
    if center:
        display = Align.center(display)
    with Live(display, transient=True, refresh_per_second=10):
        yield

_is_cleaning_up = False
def stop_backend(stop_party=True):
    """Cleanup Streamix backend resources.

    If stop_party is False, active watch-party room processes (party server,
    ngrok tunnel, host/client chat terminals) are intentionally preserved.
    """
    global _is_cleaning_up
    if _is_cleaning_up:
        return
    _is_cleaning_up = True
    
    global backend_process
    if backend_process:
        try:
            if backend_process.poll() is None:
                console.print(f"[dim]Stopping {PROJECT_NAME} backend[/dim]")
                logger.info(f"[LIFECYCLE] Stopping backend process (PID: {backend_process.pid})")
                backend_process.terminate()
                backend_process.wait(timeout=3)
        except:
            if backend_process:
                try: 
                    logger.warning(f"[LIFECYCLE] Force-killing backend process (PID: {backend_process.pid})")
                    backend_process.kill()
                except: pass
        finally:
            backend_process = None
            # Explicitly flush logs before exit
            for handler in logger.handlers:
                handler.flush()
            
    global active_subprocesses
    for proc in active_subprocesses:
        try:
            if (not stop_party) and party_proc and proc is party_proc:
                # Keep detached party room alive when launcher exits.
                continue
            if proc.poll() is None:
                logger.info(f"[LIFECYCLE] Terminating subprocess (PID: {proc.pid})")
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                logger.warning(f"[LIFECYCLE] Force-killing subprocess (PID: {proc.pid})")
                proc.kill()
            except:
                pass
    active_subprocesses.clear()
    logger.info("[LIFECYCLE] System cleanup completed.")
            
    # Kill party Python processes and ngrok only when explicitly requested.
    if stop_party:
        try:
            import signal
            if IS_WINDOWS:
                # Use taskkill but catch any issues
                subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe", "/T"], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # Unix-like cleanup: terminate party server first so host/client
                # receive websocket closure and can show their session countdown.
                subprocess.run(["pkill", "-f", os.path.join(os.path.dirname(__file__), "features", "watch_party", "party.py")], stderr=subprocess.DEVNULL)
                # Allow client/host UIs to process close event and exit gracefully.
                time.sleep(3.5)
                # Fallback cleanup for stragglers only.
                subprocess.run(["pkill", "-f", os.path.join(os.path.dirname(__file__), "features", "watch_party", "host.py")], stderr=subprocess.DEVNULL)
                subprocess.run(["pkill", "-f", os.path.join(os.path.dirname(__file__), "features", "watch_party", "client.py")], stderr=subprocess.DEVNULL)
                subprocess.run(["pkill", "-9", "ngrok"], stderr=subprocess.DEVNULL)
        except Exception:
            pass
    
    # Cleanup party info file only when room teardown is requested.
    if stop_party:
        try:
            if PARTY_INFO_PATH.exists():
                PARTY_INFO_PATH.unlink()
        except Exception:
            pass
    
    # Cleanup any leftover IPC socket files (Unix only)
    if not IS_WINDOWS:
        try:
            import glob
            for sock in glob.glob("/tmp/streamix_*.sock"):
                try: os.remove(sock)
                except: pass
        except Exception:
            pass

def _cleanup_party():
    """Kill all party-related processes (server, ngrok, admin/client terminals) without touching the backend."""
    global active_subprocesses
    
    # 1. Kill tracked subprocesses first (Safest way)
    for proc in active_subprocesses:
        try:
            if proc.poll() is None:
                logger.info(f"[LIFECYCLE] Terminating session subprocess (PID: {proc.pid})")
                proc.terminate()
                try: proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired: proc.kill()
        except: pass
    active_subprocesses.clear()

    # 2. Force-kill known external dependencies (ngrok)
    try:
        if IS_WINDOWS:
            # Only kill ngrok on windows as a fallback
            subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe", "/T"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.system("pkill -9 ngrok >/dev/null 2>&1")
    except:
        pass
    
    # 3. Cleanup state files
    try:
        if PARTY_INFO_PATH.exists():
            PARTY_INFO_PATH.unlink()
    except:
        pass
    
    # Remove party_info.json so the next host doesn't read stale data
    try:
        if PARTY_INFO_PATH.exists():
            PARTY_INFO_PATH.unlink()
    except Exception:
        pass
    
    # Cleanup IPC sockets
    if not IS_WINDOWS:
        try:
            import glob
            for sock in glob.glob("/tmp/streamix_*.sock"):
                try: os.remove(sock)
                except: pass
        except Exception:
            pass
    
    # Remove dead party procs from active_subprocesses
    active_subprocesses = [p for p in active_subprocesses if p.poll() is None]

def _open_in_new_terminal(script_name, args, title="Streamix"):
    """Open a Python script in a new terminal window. Works on macOS, Windows, and Linux."""
    py = sys.executable
    script = os.path.abspath(script_name)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_argv = [py, script] + [str(a) for a in args]

    proc = None

    if current_os is OS.WINDOWS:
        # Windows: run script with the current interpreter in a dedicated console.
        # This avoids dependency on shell-specific tools in child terminals.
        command = subprocess.list2cmdline(child_argv)
        logger.info(f"[LIFECYCLE] Launching {script_name} in new Windows Terminal: {title}")
        create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

        proc = subprocess.Popen(
            ["cmd", "/k", command],
            creationflags=create_new_console,
            cwd=project_root,
        )

    elif current_os is OS.MACOS:
        # macOS: open a new Terminal.app window via AppleScript
        script_cmd = f"cd {shlex.quote(project_root)}; " + " ".join(
            shlex.quote(part) for part in child_argv
        )
        script_cmd += "; exit"
        
        # Proper escaping for AppleScript "do script"
        escaped_script = script_cmd.replace("\\", "\\\\").replace('"', '\\"')
        applescript = f'tell application "Terminal" to do script "{escaped_script}"'
        proc = subprocess.Popen(["osascript", "-e", applescript])
        
    else:
        # Linux: try common terminal emulators
        term = (shutil.which("x-terminal-emulator") or 
                shutil.which("gnome-terminal") or 
                shutil.which("konsole") or 
                shutil.which("alacritty") or 
                shutil.which("xterm"))
        if term:
            command = " ".join(shlex.quote(part) for part in child_argv)
            terminal_name = os.path.basename(term)

            if terminal_name == "gnome-terminal":
                cmd_args = [
                    term,
                    "--working-directory",
                    project_root,
                    "--",
                    "bash",
                    "-lc",
                    f"{command}; exec bash",
                ]
            elif terminal_name == "konsole":
                cmd_args = [
                    term,
                    "--workdir",
                    project_root,
                    "-e",
                    "bash",
                    "-lc",
                    f"{command}; exec bash",
                ]
            elif terminal_name == "alacritty":
                cmd_args = [
                    term,
                    "--working-directory",
                    project_root,
                    "-e",
                    "bash",
                    "-lc",
                    f"{command}; exec bash",
                ]
            elif terminal_name == "xterm":
                cmd_args = [
                    term,
                    "-e",
                    "bash",
                    "-lc",
                    f"cd {shlex.quote(project_root)} && {command}; exec bash",
                ]
            else:
                # Generic fallback for distro-provided terminal wrappers.
                cmd_args = [
                    term,
                    "-e",
                    "bash",
                    "-lc",
                    f"cd {shlex.quote(project_root)} && {command}; exec bash",
                ]

            proc = subprocess.Popen(cmd_args)
        else:
            console.print("[red]Could not find a suitable terminal emulator![/red]")
    
    if proc:
        active_subprocesses.append(proc)
    return proc

def start_backend():
    global backend_process

    # ── Display Header ──
    console.clear()
    console.print()  # Spacer

    if BANNER_PATH.exists():
        try:
            with open(BANNER_PATH, "r", encoding="utf-8") as f:
                banner_text = f.read()
                console.print(Align.center(Text(banner_text, style="bold cyan")))
        except Exception:
            console.print(Align.center(f"[bold cyan]✨ {PROJECT_NAME} ✨[/bold cyan]"))
    else:
        console.print(Align.center(f"[bold cyan]✨ {PROJECT_NAME} ✨[/bold cyan]"))

    console.print(Align.center(f"[bold magenta]v{VERSION}[/bold magenta] [dim]|[/dim] [dim]Made with 💖 by [bold cyan]Vibhas Dutta[/bold cyan][/dim]"))
    console.print(Align.center(Rule(style="dim cyan"), width=60))
    console.print()

    # Check if a server is already running on 8000
    try:
        requests.get(API_BASE, timeout=1)
        return
    except requests.RequestException:
        pass

    # ── Start Backend ──
    with status_after(f"[bold yellow]🚀 Initializing {PROJECT_NAME} backend server[/bold yellow]", center=True):
        # Ensure .logs directory exists
        log_dir = LOGS_DIR
        
        # Redirect stderr to a log file
        log_file_path = log_dir / "streamix_backend.log"
        log_file = open(log_file_path, "a")
        log_file.write(f"\n--- SESSION START: {time.ctime()} ---\n")
        log_file.flush()

        try:
            backend_process = subprocess.Popen(
                ["uv", "run", "uvicorn", "features.api_backend.backend:app", "--app-dir", os.path.dirname(__file__), "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"],
                stdout=log_file,
                stderr=log_file
            )
            logger.info(f"[LIFECYCLE] Backend server started successfully (PID: {backend_process.pid})")
            atexit.register(lambda: stop_backend(stop_party=False))
            
            # Wait for backend to be ready
            max_attempts = 15
            for i in range(max_attempts):
                try:
                    requests.get(f"{API_BASE}/", timeout=1)
                    return
                except requests.RequestException:
                    time.sleep(1)
            
            console.print(f"\n[bold red]❌ Timed out waiting for {PROJECT_NAME} backend to start.[/bold red]")
            sys.exit(1)
        except Exception as e:
            console.print(f"\n[bold red]❌ Failed to start backend: {e}[/bold red]")
            sys.exit(1)


def score_bar(score, max_score=100, width=20):
    if score is None or score == "?":
        return "[dim]N/A[/dim]"
    score = int(score)
    filled = int((score / max_score) * width)
    empty = width - filled
    if score >= 75:
        color = "green"
    elif score >= 50:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim] {score}/100"


def play_video(url, anime_title="Custom Playback", episode_num="", is_custom=False, is_live=False, quality=None, ipc_server=None, start_time=0, provider=None):
    """Platform-aware video playback using mpv exclusively.
    """
    mpv_path = get_mpv_path()

    # ── Log Playback ──
    try:
        with open(LOGS_DIR / "streamix_backend.log", "a", encoding='utf-8') as f:
            ep_string = f" - Ep {episode_num}" if episode_num else ""
            f.write(f"PLAYBACK: [{time.ctime()}] {anime_title}{ep_string} | {url}\n")
    except:
        pass

    # ── Try to use mpv ──
    if mpv_path:
        console.print(f"[bold magenta]▶️ Launching mpv[/bold magenta] [dim](Space: Pause/Play, 9/0: Volume, F: Fullscreen, M: Mute)[/dim]")
        
        title_arg = f"--title={anime_title}"
        if episode_num:
            title_arg += f" - Ep {episode_num}"
            
        args = [mpv_path, title_arg, "--fs"]
        
        if ipc_server:
            args.append(f"--input-ipc-server={ipc_server}")
        
        if is_live:
            args.append("--profile=low-latency")
        
        if quality and str(quality).isdigit():
            # Apply quality constraint for yt-dlp
            q = int(quality)
            args.append(f"--ytdl-format=bestvideo[height<={q}]+bestaudio/best[height<={q}]")
        
        # Add shared headers and optimizations
        args.extend(get_streaming_headers(url, provider))
        
        if is_custom:
            # Better buffering for multi-stream HLS/DASH
            args.extend([
                "--demuxer-max-bytes=150MiB",
                "--demuxer-max-back-bytes=75MiB",
                "--cache=yes",
            ])
            
        if start_time and float(start_time) > 0:
            args.append(f"--start={start_time}")
            
        # ── Native Stdout Tracking (No Lua/Files needed) ──
        # Force mpv to continuously stream precise playback position to stdout
        args.append("--term-status-msg=STREAMIX_POS=${=time-pos}|${=duration}")

        args.append(url)

        if ipc_server and not IS_WINDOWS and os.path.exists(ipc_server):
            try:
                os.remove(ipc_server)
            except Exception as e:
                logger.warning(f"[LIFECYCLE] Could not remove stale host IPC socket {ipc_server}: {e}")
        
        try:
            if ipc_server and party_proc:
                mpv_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info(f"[LIFECYCLE] Solo Player launched (PID: {mpv_process.pid}) for URL: {url}")
                import threading
                def _party_watchdog():
                    while mpv_process.poll() is None:
                        if party_proc.poll() is not None:
                            try: mpv_process.kill()
                            except: pass
                            return
                        time.sleep(0.5)
                
                watcher = threading.Thread(target=_party_watchdog, daemon=True)
                watcher.start()
                mpv_process.wait()
                return 0.0, 0.0  # Party mode doesn't store local resume progress natively yet
            else:
                # Solo mode: Capture stdout to precisely track the exit position
                mpv_process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
                logger.info(f"[LIFECYCLE] Party Player launched (PID: {mpv_process.pid}) for URL: {url}")
                
                last_pos = 0.0
                duration = 0.0
                for line in mpv_process.stdout:
                    if "STREAMIX_POS=" in line:
                        try:
                            # Parse out the float values. Format: STREAMIX_POS=pos|dur
                            val = line.split("STREAMIX_POS=")[1].strip()
                            parts = val.split("|")
                            if parts[0] and parts[0] != '(unavailable)':
                                last_pos = float(parts[0])
                            if len(parts) > 1 and parts[1] and parts[1] != '(unavailable)':
                                duration = float(parts[1])
                        except:
                            pass
                            
                mpv_process.wait()
                
            return last_pos, duration
            
        except KeyboardInterrupt:
            return 0.0, 0.0
        except Exception as e:
            console.print(f"[bold red]❌ mpv failed to launch:[/bold red] {e}")
            return 0.0, 0.0
    else:
        # MPV Missing - MANDATORY REQUIREMENT
        console.print("\n" + "="*50)
        console.print("[bold red]❌ ERROR: mpv Player Not Found![/bold red]")
        console.print(f"[white]{PROJECT_NAME} now requires [bold cyan]mpv[/bold cyan] for all playback and tracking.[/white]")
        
        if current_os == "Windows":
            console.print("[white]👉 Install: [cyan]https://mpv.io/installation/[/cyan] or [magenta]'choco install mpv'[/magenta][/white]")
        elif current_os == "Darwin":
            console.print("[white]👉 Install: [magenta]'brew install mpv'[/magenta][/white]")
        else:
            console.print("[white]👉 Install: [magenta]'sudo apt install mpv'[/magenta][/white]")
        console.print("="*50 + "\n")
        
        Prompt.ask("[bold yellow]Press Enter to return to menu[/bold yellow]")
        return False

def load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if "anilist_id" in data:
                    new_data = {str(data["anilist_id"]): data}
                    new_data[str(data["anilist_id"])]["timestamp"] = time.time()
                    return new_data
                return data
        except:
            return {}
    return {}

def save_cache(title, anilist_id, provider, category, episode, total_eps=0, status="Watching", mark_watched=False, resume_time=0):
    cache = load_cache()
    a_id = str(anilist_id)
    if a_id not in cache:
        cache[a_id] = {
            "title": title,
            "anilist_id": anilist_id,
            "watched": [],
            "status": "Watching",
            "total_eps": total_eps,
            "resume_times": {}
        }
    
    entry = cache[a_id]
    if total_eps > 0:
        entry["total_eps"] = total_eps
    entry["status"] = status
    entry["provider"] = provider
    entry["category"] = category
    entry["last_watched_ep"] = str(episode)
    entry["timestamp"] = time.time()
    
    # Detailed logic: 
    # 'watched' array is for the ✅ checkboxes (episodes fully completed).
    # 'last_watched_ep' is for the ▶️ indicator (where the user is currently or was last).
    if "watched" not in entry: entry["watched"] = []
    if mark_watched and str(episode) not in entry["watched"]:
        entry["watched"].append(str(episode))
        
    if "resume_times" not in entry: entry["resume_times"] = {}
    entry["resume_times"][str(episode)] = resume_time
        
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=4)
    except Exception as e:
        console.print(f"[red]Failed to save cache: {e}[/red]")

def show_anime_grid(results):
    if not results:
        console.print("[red]❌ No results available.[/red]")
        return None

    page = 0
    page_size = 10
    total_pages = (len(results) + page_size - 1) // page_size

    while True:
        console.clear()
        console.print()
        console.rule(f"[bold cyan]✨ {PROJECT_NAME} ✨[/bold cyan]  [dim]Page {page+1}/{total_pages}[/dim]", style="cyan")
        console.print()
        
        start = page * page_size
        end = start + page_size
        chunk = results[start:end]

        choices = []
        for idx, res in enumerate(chunk, 1):
            title_dict = res.get("title", {})
            title_str = title_dict.get("english") or title_dict.get("romaji") or "Unknown"
            year = str(res.get("seasonYear") or "—")
            status_raw = str(res.get("status", "Unknown")).title().replace("_", " ")
            avg_score = res.get("averageScore")
            score_str = f"{avg_score/10:.1f}★" if avg_score else "—"

            choices.append(questionary.Choice(
                title=f"  {_trunc(title_str, 40):<42} | {year:>4} | {status_raw:<12} | {score_str}",
                value=res
            ))
        
        choices.append(questionary.Separator())
        
        # Navigation controls
        if total_pages > 1:
            if page < total_pages - 1:
                choices.append(questionary.Choice("  ➡️  Next Page", value="next"))
            if page > 0:
                choices.append(questionary.Choice("  ⬅️  Previous Page", value="prev"))
        
        choices.append(questionary.Choice("  🔙  Go Back", value="back"))

        selected = questionary.select(
            "Select an anime:",
            choices=choices,
            style=QSTYLE,
            instruction="(↑/↓ navigate, Enter select)"
        ).ask()
        
        if selected is None:
            return None
            
        if selected == "back":
            return None
        elif selected == "next":
            page += 1
        elif selected == "prev":
            page -= 1
        else:
            return selected

def display_characters(anilist_id, anime_title):
    page = 1
    while True:
        console.clear()
        with status_after(f"[yellow]👥 Loading characters for [bold]{anime_title}[/bold] (Page {page})...[/yellow]"):
            data = fetch_json(f"{API_BASE}/anime/{anilist_id}/characters?page={page}&per_page=12")
            
        if not data or not data.get("characters"):
            console.print("[red]No characters found or failed to load.[/red]")
            input("\nPress Enter to go back...")
            return

        total_chars = data.get("characters", [])
        
        table = Table(title=f"Characters & Voice Actors - {anime_title}", box=None, padding=(0, 2))
        table.add_column("Character", style="bold cyan", no_wrap=False)
        table.add_column("Role", style="dim italic")
        table.add_column("Voice Actor (JP)", style="bold magenta")
        
        for edge in total_chars:
            node = edge.get("node", {})
            role = edge.get("role", "SUPPORTING")
            va_list = edge.get("voiceActors", [])
            va_name = va_list[0].get("name", {}).get("full", "N/A") if va_list else "N/A"
            char_name = node.get("name", {}).get("full", "Unknown")
            
            table.add_row(char_name, role.title(), va_name)
            
        console.print(table)
        console.print(Rule(style="dim cyan"))
        
        choices = []
        if data.get("hasNextPage"):
            choices.append(questionary.Choice("  ➡️  Next Page", value="next"))
        if page > 1:
            choices.append(questionary.Choice("  ⬅️  Previous Page", value="prev"))
        choices.append(questionary.Choice("  🔙  Back to Details", value="back"))
        
        ans = questionary.select("Navigation:", choices=choices, style=QSTYLE).ask()
        if ans == "back": break
        elif ans == "next": page += 1
        elif ans == "prev": page -= 1

def display_recommendations(anilist_id, anime_title):
    console.clear()
    with status_after(f"[yellow]💡 Loading recommendations for [bold]{anime_title}[/bold]...[/yellow]"):
        data = fetch_json(f"{API_BASE}/anime/{anilist_id}/recommendations?per_page=15")
        
    if not data or not data.get("recommendations"):
        console.print("[red]No recommendations found.[/red]")
        input("\nPress Enter to go back...")
        return

    nodes = data.get("recommendations", [])
    
    choices = []
    for entry in nodes:
        node = entry.get("mediaRecommendation", {})
        if not node: continue
        r_title = node.get("title", {}).get("english") or node.get("title", {}).get("romaji") or "Unknown"
        score = node.get("averageScore", "?")
        choices.append(questionary.Choice(f"  ✨  {_trunc(r_title, 50)} ([yellow]{score}%[/yellow])", value=node))
        
    choices.append(questionary.Separator())
    choices.append(questionary.Choice("  🔙  Back to Details", value="back"))
    
    selected = questionary.select("You might also like:", choices=choices, style=QSTYLE).ask()
    if selected == "back":
        return
    else:
        # Recursively view recommendations
        display_anime_details(selected)

def display_anime_details(selected_anime):
    console.clear()
    console.print()
    
    anilist_id = selected_anime.get("id")
    # Fetch full info (with caching)
    with status_after(f"[yellow]📖 Loading details for [bold]{selected_anime.get('title', {}).get('english', 'Anime')}[/bold][/yellow]"):
        try:
            full_info = fetch_json(f"{API_BASE}/info/{anilist_id}")
            if full_info:
                selected_anime = full_info
        except:
            pass # Use partial info

    title_dict = selected_anime.get("title", {})
    t_str = title_dict.get("english") or title_dict.get("romaji") or "Unknown"
    t_romaji = title_dict.get("romaji", "")
    t_native = title_dict.get("native", "")
    
    # ── Header ──
    console.print(Rule(style="dim cyan"))
    header = Text(justify="center")
    header.append(f"  {t_str}  \n", style="bold cyan")
    subtitles = []
    if t_romaji and t_romaji != t_str:
        subtitles.append(t_romaji)
    if t_native:
        subtitles.append(t_native)
    if subtitles:
        header.append("  •  ".join(subtitles), style="dim italic")
    console.print(Align.center(header))
    console.print(Rule(style="dim cyan"))
    console.print()

    # ── Info Table ──
    status = str(selected_anime.get("status", "Unknown")).title().replace("_", " ")
    episodes = selected_anime.get("episodes", "?")
    season = selected_anime.get("season", "")
    year = selected_anime.get("seasonYear", "?")
    fmt = selected_anime.get("format", "Unknown")
    avg_score = selected_anime.get("averageScore")
    popularity = selected_anime.get("popularity", "?")
    favourites = selected_anime.get("favourites", "?")
    genres = selected_anime.get("genres", [])
    studios = [s.get("name") for s in selected_anime.get("studios", {}).get("nodes", []) if s.get("isAnimationStudio")]
    
    # Status color
    status_colors = {"Releasing": "green", "Finished": "white", "Not Yet Released": "yellow", "Cancelled": "red", "Hiatus": "yellow"}
    s_color = status_colors.get(status, "white")
    
    # Score display
    if avg_score:
        score_val = avg_score / 10.0
        stars = "★" * int(score_val) + "☆" * (10 - int(score_val))
        score_fmt = f"[bold yellow]{score_val:.1f}[/bold yellow] [dim]{stars}[/dim]"
    else:
        score_fmt = "[dim]N/A[/dim]"

    info_grid = Table.grid(expand=True, padding=(0, 2))
    info_grid.add_column("L", ratio=1)
    info_grid.add_column("R", ratio=1)

    info_grid.add_row(
        f"[bold cyan]Status:[/bold cyan] [{s_color}]{status}[/{s_color}]",
        f"[bold magenta]Score:[/bold magenta] {score_fmt}"
    )
    info_grid.add_row(
        f"[bold cyan]Episodes:[/bold cyan] {episodes}",
        f"[bold magenta]Popularity:[/bold magenta] {popularity}"
    )
    info_grid.add_row(f"[bold cyan]Season:[/bold cyan] {season} {year}", f"[bold magenta]Favorites:[/bold magenta] {favourites}")
    site_url = selected_anime.get("siteUrl", f"https://anilist.co/anime/{anilist_id}")
    
    info_grid.add_row(
        f"[bold cyan]Format:[/bold cyan] {fmt}",
        f"[bold magenta]Studio:[/bold magenta] {', '.join(studios) if studios else 'N/A'}"
    )
    info_grid.add_row(f"[bold cyan]Link:[/bold cyan] [dim blue underline]{site_url}[/dim blue underline]", "")
    
    console.print(info_grid)
    console.print()
    
    # ── Genres ──
    if genres:
        genre_text = Text()
        for i, g in enumerate(genres):
            genre_text.append(f" {g} ", style="bold black on cyan")
            if i < len(genres) - 1: genre_text.append("  ")
        console.print(Align.center(genre_text))
        console.print()

    # ── Synopsis ──
    description = selected_anime.get("description", "No description available.")
    import re
    clean_desc = re.sub(r'<[^>]+>', '', description).strip()
    
    if clean_desc and clean_desc != "No description available.":
        console.print(Rule("[bold white]SYNOPSIS[/bold white]", style="dim"))
        console.print()
        # Limit to ~4 lines for readability
        lines = clean_desc.split(". ")
        short_desc = ". ".join(lines[:4])
        if len(lines) > 4:
            short_desc += "…"
        console.print(f"  [dim]{short_desc}[/dim]")
        console.print()

    # ── Airing Status ──
    next_ep = selected_anime.get("nextAiringEpisode")
    if next_ep:
        ep_num = next_ep.get("episode")
        time_left = next_ep.get("timeUntilAiring", 0)
        days = time_left // 86400
        hours = (time_left % 86400) // 3600
        console.print(Align.center(f"[bold yellow]⏰ Episode {ep_num} airs in {days}d {hours}h[/bold yellow]"))
        console.print()

    console.print(Rule(style="dim cyan"))
    console.print()

    # ── Action Menu ──
    relations_list = selected_anime.get("relations", {}).get("edges", [])
    anime_relations = []
    seen_ids = set()
    valid_relations = {"PREQUEL", "SEQUEL", "PARENT", "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "ADAPTATION", "FRANCHISE"}
    
    def _add_relation(rel_node, rel_str):
        n_id = rel_node.get("id")
        if not n_id or n_id in seen_ids:
            return
        if rel_node.get("type") == "ANIME":
            anime_relations.append({"relationType": rel_str, "node": rel_node})
            seen_ids.add(n_id)

    # First pass
    for rel in (relations_list or []):
        node = rel.get("node", {})
        rel_type = rel.get("relationType", "")
        if rel_type in valid_relations:
            _add_relation(node, rel_type)
            
            # Second pass (franchise expansion)
            deep_edges = node.get("relations", {}).get("edges", [])
            for deep_rel in deep_edges:
                if deep_rel.get("relationType", "") in valid_relations:
                    _add_relation(deep_rel.get("node", {}), "FRANCHISE")

    opt_choices = [
        questionary.Choice("  ▶️  Watch Episodes", value="watch"),
        questionary.Choice("  👥  Characters & Staff", value="characters"),
        questionary.Choice("  💡  Recommendations", value="recommendations")
    ]
    
    if anime_relations:
        opt_choices.append(questionary.Separator("--- Related / Seasons ---"))
        for r in anime_relations[:20]:
            rel_type = str(r.get("relationType", "RELATED")).replace("_", " ").title()
            node = r.get("node", {})
            r_title = node.get("title", {}).get("english") or node.get("title", {}).get("romaji") or "Unknown"
            opt_choices.append(questionary.Choice(f"  🔗  {rel_type}: {_trunc(r_title, 40)}", value=("relation", node)))
            
    opt_choices.append(questionary.Separator())
    opt_choices.append(questionary.Choice("  🔙  Back", value="back"))
    
    action_val = questionary.select(
        "What would you like to do?",
        choices=opt_choices,
        style=QSTYLE
    ).ask()
    
    if action_val == "watch":
        return "watch", (t_str, anilist_id)
    elif action_val == "characters":
        display_characters(anilist_id, t_str)
        return display_anime_details(selected_anime) # Loop back
    elif action_val == "recommendations":
        display_recommendations(anilist_id, t_str)
        return display_anime_details(selected_anime) # Loop back
    elif isinstance(action_val, tuple) and action_val[0] == "relation":
        return "relation", action_val[1]
    
    return "back", None

def handle_audio_peripherals():
    """Menu to select and test microphone and headphones."""
    from features.voice_chat.voice_manager import VoiceManager
    import sounddevice as sd
    from core.config import update_admin_config, update_client_config, load_config
    
    while True:
        cfg = load_config()
        # Use admin config as source of truth for setup (it syncs to client anyway)
        mic_idx = cfg["admin"].get("mic_device_index")
        
        # Get actual names if possible
        devices = sd.query_devices()
        mic_name = "Default"
        if mic_idx is not None and mic_idx < len(devices):
            mic_name = devices[mic_idx]['name']

        console.clear()
        console.rule("[bold magenta]🎙️ AUDIO PERIPHERALS[/bold magenta]", style="dim magenta")
        console.print(f"\n[cyan]Microphone:[/cyan]  {mic_name}")
        console.print("\n[dim]Anime and Voice Chat will follow your Windows default output.[/dim]")
        console.print()
        
        choice = questionary.select(
            "Peripherals Menu:",
            choices=[
                "Change Microphone",
                "Test Microphone (Live Bar)",
                "Hear a Test Sound (Headphones)",
                questionary.Separator(),
                "Save & Back"
            ],
            style=QSTYLE
        ).ask()
        
        if choice == "Save & Back" or choice is None:
            break
            
        if choice == "Change Microphone":
            mics = VoiceManager.get_devices(kind='input')
            mic_choices = [questionary.Choice(f" {m['index']}: {m['name']}", value=m['index']) for m in mics]
            mic_choices.insert(0, questionary.Choice("  🔙  Back", value="back"))
            mic_choices.insert(1, questionary.Choice("Default System Device", value=None))
            
            new_mic = questionary.select("Select Microphone:", choices=mic_choices, style=QSTYLE).ask()
            if new_mic != "back":
                update_admin_config(mic_device_index=new_mic)
                update_client_config(mic_device_index=new_mic)
            
        elif choice == "Test Microphone (Live Bar)":
            console.print("[yellow]Mic Test active for 10 seconds. Speak now![/yellow]")
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                install_asyncio_exception_handler(loop, logger)
            vm = VoiceManager(loop, input_device=mic_idx)
            vm.mic_muted = False
            vm.start()
            
            from rich.live import Live
            from rich.panel import Panel
            
            def make_bar():
                vol = vm.current_volume
                bar_len = int(vol * 40)
                bar = "█" * bar_len + " " * (40 - bar_len)
                color = "green" if vol < 0.6 else ("yellow" if vol < 0.8 else "red")
                return Panel(f"[{color}]{bar}[/{color}]", title="Mic Input Level", width=50)

            with Live(make_bar(), refresh_per_second=20) as live:
                for _ in range(200): # ~10 seconds
                    live.update(make_bar())
                    time.sleep(0.05)
            vm.stop()

        elif choice == "Hear a Test Sound (Headphones)":
            console.print("[yellow]Playing test tone...[/yellow]")
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            # Use output_device=None to play through Windows Default
            vm = VoiceManager(loop, output_device=None)
            vm.play_test_sound()

def handle_episode_flow(anilist_id, t_str, pre_provider=None, pre_category=None, ipc_server_path=None):
    with status_after(f"[yellow]🔍 Fetching providers for {t_str}[/yellow]"):
        try:
            # Episodes should have low TTL (1h) so airing anime get new episodes quickly
            providers = fetch_json(f"{API_BASE}/episodes/{anilist_id}", ttl_hours=1)
        except Exception as e:
            console.print(f"[red]❌ Connection Error: {e}[/red]")
            console.print("[dim]Check 'data/logs/streamix_backend.log' for details.[/dim]")
            return

    providers = providers.get("providers", {})
    if not providers:
        console.print("[red]❌ No providers found for this anime.[/red]")
        return
        
    session_provider = pre_provider
    session_category = pre_category
    auto_play = True # Default to ON as requested
    
    while True:
        auto_label = "[green]ON[/green]" if auto_play else "[red]OFF[/red]"
        auto_text = "ON" if auto_play else "OFF"
        if not session_provider:
            console.clear()
            console.print()
            console.rule(f"[bold magenta]{t_str}[/bold magenta]", style="magenta")
            console.print()
            
            p_names = list(providers.keys())
            p_choices = [questionary.Choice(f"  📡  {name.upper()}", value=name) for name in p_names]
            p_choices.append(questionary.Separator())
            p_choices.append(questionary.Choice("  🔙  Back to Main Menu", value="back"))
            
            session_provider = questionary.select(
                "Select Provider:",
                choices=p_choices,
                style=QSTYLE,
                instruction="(↑/↓ navigate)"
            ).ask()
            
            if session_provider is None:
                return
            
            if session_provider == "back":
                return

        if not session_category:
            console.clear()
            console.print()
            header = f"[bold magenta]{t_str}[/bold magenta]  [dim]|[/dim]  [bold cyan]{session_provider.upper()}[/bold cyan]  [dim]|[/dim]  Auto-Play: {auto_label}"
            console.rule(header, style="magenta")
            console.print()
            
            categories = list(providers[session_provider].get("episodes", {}).keys())
            cat_choices = [questionary.Choice(f"  🔄  Toggle Auto-Play ({auto_text})", value="toggle_auto")]
            cat_choices.extend([questionary.Choice(f"  🎬  {c.upper()}", value=c) for c in categories])
            cat_choices.append(questionary.Separator())
            cat_choices.append(questionary.Choice("  🔙  Change Provider", value="back"))
            
            session_category = questionary.select(
                "Select Category:",
                choices=cat_choices,
                style=QSTYLE,
            ).ask()
            
            if session_category is None:
                session_provider = None
                continue
            
            if session_category == "toggle_auto":
                auto_play = not auto_play
                session_category = None
                continue
            
            if session_category == "back":
                session_provider = None
                continue

        ep_list = providers.get(session_provider, {}).get("episodes", {}).get(session_category, [])
        # Ensure episodes are sorted ascending by number to avoid "Index Issue"
        try:
            ep_list = sorted(ep_list, key=lambda x: float(x.get('number', 0)))
        except:
            pass # Fallback to original order if sorting fails

        if not ep_list:
            console.print("[red]❌ No episodes found![/red]")
            session_category = None
            continue
            
        # Get watched episodes for this anime (O(1) lookup hashmap)
        cache = load_cache()
        watched_map = set(cache.get(str(anilist_id), {}).get("watched", []))
        last_watched = cache.get(str(anilist_id), {}).get("last_watched_ep")

        resume_times = cache.get(str(anilist_id), {}).get("resume_times", {})

        # ── Episode List ──
        console.clear()
        console.print()
        watched_count = len(watched_map)
        progress_str = f"{watched_count}/{len(ep_list)}" if len(ep_list) > 0 else ""
        console.rule(f"[bold magenta]{_trunc(t_str, 30)}[/bold magenta]  [dim]|[/dim]  [bold cyan]{session_provider.upper()}[/bold cyan]  [dim]|[/dim]  [dim]{progress_str} episodes[/dim]", style="magenta")
        console.print()

        ep_choices = []
        for ep in ep_list:
            e_num = str(ep.get('number'))
            ep_title = _trunc(ep.get('title', ''), 40)
            
            if e_num == last_watched:
                status_icon = "▶️ Now"
                prefix = "▶️"
            elif e_num in watched_map:
                status_icon = "✅ Done"
                prefix = "✅"
            elif e_num in resume_times and float(resume_times[e_num]) > 0:
                saved_pos = int(resume_times[e_num])
                status_icon = f"⏳ Resume ({saved_pos//60}:{saved_pos%60:02d})"
                prefix = "⏳"
            else:
                status_icon = "—"
                prefix = "  "
            
            ep_choices.append(questionary.Choice(
                title=f" {prefix} Ep {e_num:<4} | {_trunc(ep_title, 40):<42} | {status_icon}", 
                value=ep
            ))
        
        ep_choices.append(questionary.Separator())
        ep_choices.append(questionary.Choice("  🔙  Back to Categories", value="back"))
        
        selected_ep = questionary.select(
            f"Select Episode:",
            choices=ep_choices,
            style=QSTYLE,
            instruction="(↑/↓ navigate)"
        ).ask()
        
        if selected_ep is None:
            session_category = None
            continue
            
        if selected_ep == "back":
            session_category = None
            continue
            
        ep_num = str(selected_ep["number"])

        with status_after(f"[yellow]▶️ Fetching streaming links for Episode {ep_num}[/yellow]"):
            try:
                # Streaming links stay for 6 hours (expirable links)
                watch_res = fetch_json(f"{API_BASE}/{selected_ep['id']}", ttl_hours=6)
            except Exception as e:
                console.print(f"[red]❌ Error fetching streams: {e}[/red]")
                continue
            
        streams = watch_res.get("streams", [])
        if not streams:
            console.print("[red]❌ No playable video URLs found for this episode![/red]")
            continue
            
        # Select Video Link with Questionary
        quality_icons = {"1080p": "💎", "720p": "🎬", "480p": "📺", "360p": "📟"}
        stream_choices = [
            questionary.Choice(
                title=f"  {quality_icons.get(str(s.get('quality', '')).lower(), '🚀')}  {str(s.get('quality', 'Auto')).upper():<7} | {s.get('url', '')}", 
                value=s
            )
            for s in streams
        ]
        stream_choices.append(questionary.Separator())
        stream_choices.append(questionary.Choice("  🔙  Back to Episodes", value="back"))
        
        selected_stream = questionary.select(
            f"Select Quality (Ep {ep_num}):",
            choices=stream_choices,
            style=QSTYLE,
        ).ask()
        
        if selected_stream is None:
            continue
            
        if selected_stream == "back":
            continue
            
        while True:
            selected_url = selected_stream.get("url")
            if selected_url:
                cache = load_cache()
                resume_time = cache.get(str(anilist_id), {}).get("resume_times", {}).get(str(ep_num), 0)
                
                # 1. Instant update to mark as 'Currently Watching' (▶️ indicator)
                # We save with the current resume_time
                save_cache(t_str, anilist_id, session_provider, session_category, ep_num, total_eps=len(ep_list), status="Watching", mark_watched=False, resume_time=resume_time)
                
                final_time, dur = play_video(
                    selected_url, 
                    anime_title=t_str, 
                    episode_num=ep_num, 
                    quality=selected_stream.get('quality'), 
                    ipc_server=ipc_server_path, 
                    start_time=resume_time,
                    provider=session_provider
                )
                
                # Check if this was the last episode
                current_idx = next((i for i, e in enumerate(ep_list) if str(e.get('number')) == ep_num), -1)
                is_last = (current_idx != -1 and current_idx + 1 == len(ep_list))

                # 2. Final update based on duration threshold
                if dur > 0:
                    if (final_time / dur) > 0.90:
                        completed_ep = True
                        saved_resume = 0
                    else:
                        completed_ep = False
                        saved_resume = final_time
                else:
                    if final_time > 5.0:
                        # Duration unknown but watched somewhat
                        completed_ep = False
                        saved_resume = final_time
                    else:
                        # Failed to load entirely or instant quit -> abort without marking completed
                        completed_ep = False
                        saved_resume = resume_time
                    
                if is_last and completed_ep:
                    ep_status = "Completed"
                else:
                    ep_status = "Watching"
                    
                save_cache(t_str, anilist_id, session_provider, session_category, ep_num, total_eps=len(ep_list), status=ep_status, mark_watched=completed_ep, resume_time=saved_resume)
            
                # --- Auto Next Logic ---
                if not is_last:
                    next_ep = ep_list[current_idx + 1]
                    next_num = str(next_ep.get('number'))
                    
                    if auto_play:
                        # 3. Pre-update the 'Currently Watching' indicator for the NEXT episode
                        # This ensures the ▶️ moves to the next ep even during the 10s countdown
                        save_cache(t_str, anilist_id, session_provider, session_category, next_num, total_eps=len(ep_list), status="Watching", mark_watched=False)
                        
                        console.print(f"\n[bold green]✅ Episode Finished[/bold green]")

                        is_windows = IS_WINDOWS
                        should_play = False
                        
                        if is_windows:
                            import msvcrt
                            time_left = 10.0
                            start_time = time.time()
                            
                            while time_left > 0 and auto_play:
                                display = int(time_left) + 1
                                auto_text = "ON" if auto_play else "OFF"
                                console.print(f"[bold yellow]⏭️  Next Episode {next_num} in {display}s ({auto_text})[/bold yellow] ([red]'b' to Select Ep[/red] | [yellow]'s' to Toggle Auto-Play[/yellow])        ", end="\r")
                                
                                if msvcrt.kbhit():
                                    char = msvcrt.getch().decode("utf-8", "ignore").lower()
                                    if char == 'b':
                                        should_play = False
                                        break
                                    elif char == 's':
                                        auto_play = not auto_play
                                    elif char in ['\r', '\n', ' ']:
                                        should_play = True
                                        break
                                
                                time.sleep(0.05)
                                time_left = 10.0 - (time.time() - start_time)
                            
                            if time_left <= 0:
                                should_play = True
                                
                            console.print()
                            
                            if not should_play:
                                console.print(f"[bold red]🛑 Auto-Play Cancelled[/bold red]")
                                break
                        else:
                            import select, tty, termios
                            fd = sys.stdin.fileno()
                            old_settings = termios.tcgetattr(fd)
                            try:
                                tty.setcbreak(fd)
                                time_left = 10.0
                                start_time = time.time()
                                
                                while time_left > 0 and auto_play:
                                    display = int(time_left) + 1
                                    auto_text = "ON" if auto_play else "OFF"
                                    console.print(f"[bold yellow]⏭️  Next Episode {next_num} in {display}s ({auto_text})[/bold yellow] ([red]'b' to Select Ep[/red] | [yellow]'s' to Toggle Auto-Play[/yellow])        ", end="\r")
                                    
                                    dr, _, _ = select.select([sys.stdin], [], [], 0.05)
                                    if dr:
                                        char = sys.stdin.read(1).lower()
                                        if char == 'b':
                                            should_play = False
                                            break
                                        elif char == 's':
                                            auto_play = not auto_play
                                        elif char in ['\r', '\n', ' ']:
                                            should_play = True
                                            break
                                    
                                    time_left = 10.0 - (time.time() - start_time)
                                
                                if time_left <= 0:
                                    should_play = True
                                
                                console.print()
                                
                                if not should_play:
                                    break
                            finally:
                                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    else:
                        # Pre-update the indicator for solo mode too
                        save_cache(t_str, anilist_id, session_provider, session_category, next_num, total_eps=len(ep_list), status="Watching", mark_watched=False)
                        
                        console.print(f"\n[bold green]✅ Episode Finished![/bold green]")
                        should_play = questionary.confirm(f"Play Episode {next_num} next?", default=True, style=QSTYLE).ask()
                        
                    if should_play:
                        ep_num = next_num
                        selected_ep = next_ep
                        
                        # Re-fetch streams for next ep
                        with status_after(f"[yellow]▶️ Fetching streams for Episode {ep_num}[/yellow]"):
                            try:
                                watch_res = fetch_json(f"{API_BASE}/{selected_ep['id']}", ttl_hours=6)
                                streams = watch_res.get("streams", [])
                                if not streams:
                                    console.print("[red]❌ No streams found for next episode.[/red]")
                                    break
                                
                                # Auto-select best quality (match previous or first)
                                prev_q = selected_stream.get("quality", "")
                                selected_stream = next((s for s in streams if s.get("quality") == prev_q), streams[0])
                                continue # Re-run inner playback loop
                            except:
                                console.print("[red]❌ Error fetching next episode.[/red]")
                                break
                    else:
                        break
                else:
                    # End of series
                    console.print()
                    console.rule("[bold green]🎉 SERIES COMPLETED! 🎉[/bold green]", style="bold green")
                    console.print()
                    console.print(Align.center("[dim]You have watched all available episodes of this series.[/dim]"))
                    time.sleep(2.5)
                    break

def main():
    # Persistent state across views
    search_history = []
    view = "home" # "home", "party", "dashboard"
    
    # Session state
    global party_proc
    party_active = False
    is_host = True
    
    party_proc = None
    ipc_server_path = None

    while True:
        # ─── VIEW: HOME ───
        if view == "home":
            console.clear()
            console.rule(f"[bold cyan]✨ {PROJECT_NAME} ✨[/bold cyan]", style="cyan")
            console.print(Align.center(f"[dim]v{VERSION}  |  Choose Your Playback Mode[/dim]"))
            console.print()

            mode = questionary.select(
                "How do you want to watch?",
                choices=[
                    questionary.Choice("  🎬  Solo (Watch Alone)", value="solo"),
                    questionary.Choice("  🎉  Party (Watch Together)", value="party"),
                    questionary.Separator(),
                    questionary.Choice("  🚪  Exit", value="exit"),
                ],
                style=QSTYLE,
                instruction="(↑/↓ navigate)"
            ).ask()
            
            if mode is None or mode == "exit":
                console.print(Align.center("[bold magenta]さようなら! 👋✨[/bold magenta]"))
                return
            
            if mode == "solo":
                party_active = False
                view = "dashboard"
            elif mode == "party":
                view = "party"

        # ─── VIEW: PARTY SETUP ───
        elif view == "party":
            console.clear()
            console.rule("[bold cyan]🎉 WATCH PARTY MODE[/bold cyan]", style="dim cyan")
            console.print()
            
            party_choice = questionary.select(
                "Watch Party Menu:",
                choices=[
                    questionary.Choice("  📡  Host a Party", value="host"),
                    questionary.Choice("  🔗  Join a Party", value="join"),
                    questionary.Choice("  🎙️  Audio Settings", value="audio"),
                    questionary.Separator(),
                    questionary.Choice("  🔙  Back", value="back"),
                ],
                style=QSTYLE,
            ).ask()
            
            if party_choice is None or party_choice == "back":
                view = "home"
                continue
            
            if party_choice == "audio":
                handle_audio_peripherals()
                continue

            if party_choice == "join":
                console.clear()
                console.rule("[bold cyan]🔗 JOIN WATCH PARTY[/bold cyan]", style="dim cyan")
                console.print()
                party_url = questionary.text("Enter Party Link (wss://...):", style=QSTYLE).ask()
                if not party_url: continue
                
                import re
                party_url = re.sub(r"\s+", "", party_url)
                if party_url.startswith("https://"): party_url = party_url.replace("https://", "wss://", 1)
                elif party_url.startswith("http://"): party_url = party_url.replace("http://", "ws://", 1)
                elif not party_url.startswith("ws://") and not party_url.startswith("wss://"): party_url = "wss://" + party_url
                
                client_cfg = get_client_config()
                username = questionary.text("Enter Your Name:", default=client_cfg.get("default_username", ""), style=QSTYLE).ask()
                if not username: username = f"Guest_{int(time.time())%1000}"
                else: update_client_config(default_username=username)
                
                console.print("[yellow]Connecting... a new window will open for chat.[/yellow]")
                _open_in_new_terminal(os.path.join(os.path.dirname(__file__), "features", "watch_party", "client.py"), [party_url, username], title="Streamix Client")
                console.print("[bold green]✅ Client window launched![/bold green]")
                input("Press Enter to return to menu...")
                view = "home"
                continue

            if party_choice == "host":
                import uuid
                uid = str(uuid.uuid4())[:8]
                ipc_server_path = fr"\\.\pipe\streamix_host_{uid}" if IS_WINDOWS else f"/tmp/streamix_host_{uid}.sock"
                
                admin_cfg = get_admin_config()
                room_name = questionary.text("Room Name:", default=admin_cfg.get("default_room_name") or f"{getpass.getuser()}'s Party", style=QSTYLE).ask()
                host_name = questionary.text("Your Host Name:", default=admin_cfg.get("default_host_name") or "Host", style=QSTYLE).ask()
                max_users = questionary.text("Max Users:", default="10", style=QSTYLE).ask()
                
                if not room_name or not host_name or not max_users: continue
                update_admin_config(default_room_name=room_name, default_host_name=host_name)
                
                console.print("[yellow]Starting party server and ngrok tunnel...[/yellow]")
                
                _cleanup_party()
                time.sleep(0.5)
                party_log = open(LOGS_DIR / "streamix_backend.log", "a")
                party_log.write(f"\n--- WATCH PARTY SERVER START: {time.ctime()} ---\n")
                party_log.flush()
                party_proc = subprocess.Popen(["uv", "run", os.path.join(os.path.dirname(__file__), "features", "watch_party", "party.py"), room_name, host_name, max_users], stdout=party_log, stderr=party_log)
                active_subprocesses.append(party_proc)
                
                with status_after("[yellow]📡 Starting Party Server...[/yellow]", center=True):
                    for _ in range(25):
                        if PARTY_INFO_PATH.exists(): break
                        time.sleep(0.5)
                
                if PARTY_INFO_PATH.exists():
                    with open(PARTY_INFO_PATH, "r") as f:
                        info = json.load(f)
                        console.print(f"\n[bold green]✅ Party ready! Share this link with friends:[/bold green]")
                        console.print(Align.center(f"[bold yellow]{info['url']}[/bold yellow]"))
                        
                        try:
                            if IS_WINDOWS:
                                subprocess.run(['clip'], input=info['url'].encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            elif IS_MACOS:
                                subprocess.run(['pbcopy'], input=info['url'].encode())
                            else:
                                if shutil.which('xclip'):
                                    subprocess.run(['xclip', '-selection', 'clipboard'], input=info['url'].encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            console.print(Align.center("[dim italic](Link automatically copied to your clipboard!)[/dim italic]"))
                        except: pass
                    
                    _open_in_new_terminal(os.path.join(os.path.dirname(__file__), "features", "watch_party", "host.py"), [host_name, ipc_server_path or "", info['url']], title="Streamix Host")
                    
                    console.print("[dim]Admin console opened in a new window.[/dim]")
                    party_active = True
                    is_host = True
                else:
                    console.print("[red]❌ Failed to start party server. Check logs.[/red]")
                    if party_proc: party_proc.terminate()
                    time.sleep(2)
                    continue
                
                console.print("\n[bold cyan]Now pick what to watch! 🍿[/bold cyan]")
                time.sleep(1.5)
                view = "dashboard"

        # ─── VIEW: DASHBOARD (Main browsing loop) ───
        elif view == "dashboard":
            if party_active and party_proc and party_proc.poll() is not None:
                console.print("[yellow]Party session ended. Returning to home menu...[/yellow]")
                _cleanup_party()
                party_active = False
                party_active_flag.clear()
                time.sleep(1)
                view = "home"
                continue

            if party_active:
                party_active_flag.set()
            else:
                party_active_flag.clear()

            console.clear()
            console.print()
            
            mode_label = "[green]🎉 Party[/green]" if party_active else "[cyan]🎬 Solo[/cyan]"
            mpv_ok = "[green]✓[/green]" if get_mpv_path() else "[red]✗[/red]"
            console.rule(f"[bold cyan]✨ {PROJECT_NAME} ✨[/bold cyan]", style="cyan")
            console.print(Align.center(f"{mode_label}  [dim]|[/dim]  [dim]v{VERSION}[/dim]  [dim]|[/dim]  mpv {mpv_ok}"))
            console.print(Rule(style="dim cyan"))
            console.print()

            cache = load_cache()
            
            dashboard_choices = [
                questionary.Choice("  🔍  Search Anime", value="search"),
                questionary.Choice("  🆕  New Releases", value="recent"),
                questionary.Choice("  ⏰  Upcoming Anime", value="upcoming"),
                questionary.Choice("  🔥  Discover Trending", value="trending"),
                questionary.Choice("  📈  Top Popular", value="popular")
            ]
            if cache:
                dashboard_choices.append(questionary.Choice("  📜  Watch History", value="history"))
            dashboard_choices.append(questionary.Choice("  ▶️  Custom Video", value="custom_play"))
            
            dashboard_choices.append(questionary.Separator())
            dashboard_choices.append(questionary.Choice("  🔙  Back", value="back"))
            dashboard_choices.append(questionary.Choice("  🚪  Exit", value="exit"))
            
            choice = questionary.select(
                "What would you like to do?",
                choices=dashboard_choices,
                style=QSTYLE,
                instruction="(↑/↓ navigate)"
            ).ask()
            
            if not choice or choice == "exit":
                console.print(Align.center("[bold magenta]さようなら! 👋✨[/bold magenta]"))
                return
            
            if choice == "back":
                _cleanup_party()
                party_active = False
                party_active_flag.clear()
                view = "home"
                continue

            if choice == 'custom_play':
                console.clear()
                console.rule("[bold cyan]▶️ CUSTOM VIDEO PLAYBACK[/bold cyan]", style="dim cyan")
                console.print()
                sub_choice = questionary.select(
                    "Source Type:",
                    choices=[
                        questionary.Choice("  📁  Local File", value="local"),
                        questionary.Choice("  🔗  Video Link / URL", value="link"),
                        questionary.Choice("  📡  Live Stream (Low Latency)", value="live"),
                        questionary.Separator(),
                        questionary.Choice("  🔙  Back", value="back")
                    ], style=QSTYLE
                ).ask()
                
                if not sub_choice or sub_choice == "back": continue
                    
                is_live = (sub_choice == "live")
                if sub_choice == "local":
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk(); root.attributes("-topmost", True); root.withdraw()
                    c_path = filedialog.askopenfilename(title="Select Video File")
                    root.destroy()
                    if not c_path: continue
                else:
                    c_path = questionary.text("Enter URL:", style=QSTYLE).ask()
                    if not c_path: continue
                
                clean_path = c_path.strip("\"'")
                if sub_choice in ["link", "live"] and not clean_path.startswith("http"): clean_path = "https://" + clean_path
                
                with status_after("[yellow]▶️ Preparing playback...[/yellow]", center=True): time.sleep(0.5)
                play_video(clean_path, anime_title="Custom Playback", is_custom=True, is_live=is_live, ipc_server=ipc_server_path)

            elif choice == 'search':
                sq_choices = [questionary.Choice(f"  🕒  {search_history[i]}", value=search_history[i]) for i in range(min(5, len(search_history)))]
                if sq_choices: sq_choices.append(questionary.Separator())
                sq_choices.append(questionary.Choice("  🔍  New Search", value="new"))
                sq_choices.append(questionary.Choice("  🔙  Back", value="back"))
                
                q_choice = questionary.select("Search:", choices=sq_choices, style=QSTYLE).ask()
                if q_choice == "back" or q_choice is None: continue
                
                if q_choice == "new":
                    query = questionary.text("Enter anime title:", style=QSTYLE).ask()
                else:
                    query = q_choice
                    
                if not query: continue
                if query not in search_history: search_history.insert(0, query)
                
                with status_after(f"[yellow]🔍 Searching for '{query}'[/yellow]", center=True):
                    try:
                        data = fetch_json(f"{API_BASE}/search", params={"query": query, "per_page": 50}, ttl_hours=1)
                        results = data.get("results", [])
                    except: results = []
                
                if not results:
                    console.print(Align.center("[red]❌ No results found.[/red]")); time.sleep(1.5); continue
                    
                selected_anime = show_anime_grid(results)
                while selected_anime:
                    action, payload = display_anime_details(selected_anime)
                    if action == "watch":
                        handle_episode_flow(payload[1], payload[0], ipc_server_path=ipc_server_path)
                        break
                    elif action == "relation":
                        selected_anime = payload
                    else:
                        break

            elif choice in ['recent', 'trending', 'popular', 'upcoming']:
                labels = {'recent': 'New Releases', 'trending': 'Trending', 'popular': 'Top Popular', 'upcoming': 'Upcoming'}
                with status_after(f"[yellow]📡 Fetching {labels[choice]}[/yellow]", center=True):
                    try:
                        data = fetch_json(f"{API_BASE}/{choice}", ttl_hours=2 if choice == 'recent' else 6)
                        results = data.get("results", [])
                    except: results = []
                
                if results:
                    selected_anime = show_anime_grid(results)
                    while selected_anime:
                        action, payload = display_anime_details(selected_anime)
                        if action == "watch":
                            handle_episode_flow(payload[1], payload[0], ipc_server_path=ipc_server_path)
                            break
                        elif action == "relation":
                            selected_anime = payload
                        else:
                            break
                else:
                    console.print(Align.center("[red]❌ Connection failed.[/red]")); time.sleep(1.5)

            elif choice == 'history' and cache:
                console.clear(); console.rule("[bold cyan]📜 WATCH HISTORY[/bold cyan]", style="cyan"); console.print()
                sorted_h = sorted(cache.values(), key=lambda x: x.get("timestamp", 0), reverse=True)
                h_opts = []
                for idx, item in enumerate(sorted_h, 1):
                    title = item.get("title", "Unknown")
                    ep = item.get("last_watched_ep", "?")
                    h_opts.append(questionary.Choice(f" {idx:>2} | {_trunc(title, 35):<37} | Ep {ep}", value=item))
                h_opts.append(questionary.Separator()); h_opts.append(questionary.Choice("  🔙  Back", value="back"))
                
                sel_item = questionary.select("Resume watching:", choices=h_opts, style=QSTYLE).ask()
                if sel_item == "back" or sel_item is None: continue
                
                mock_a = {"id": sel_item["anilist_id"], "title": {"english": sel_item["title"]}}
                while mock_a:
                    action, payload = display_anime_details(mock_a)
                    if action == "watch":
                        handle_episode_flow(payload[1], payload[0], 
                                            pre_provider=sel_item.get("provider") if mock_a["id"] == sel_item["anilist_id"] else None,
                                            ipc_server_path=ipc_server_path)
                        break
                    elif action == "relation": mock_a = payload
                    else: break
        else:
            console.print("[red]❌ View error.[/red]")
            view = "home"

if __name__ == "__main__":
    import signal
    
    def _signal_cleanup(signum, frame):
        """Handle SIGTERM/SIGINT for clean shutdown."""
        raise KeyboardInterrupt
    
    signal.signal(signal.SIGTERM, _signal_cleanup)
    signal.signal(signal.SIGINT, _signal_cleanup)
    
    try:
        start_backend()
        main()
    except KeyboardInterrupt:
        console.print(f"\n\n[bold magenta]さようなら! 👋✨ Logging out of {PROJECT_NAME}[/bold magenta]")
    except Exception as e:
        console.print(f"\n\n[bold red]❌ CRITICAL ERROR:[/bold red] {e}")
        console.print("[dim]Backend has been safely shut down.[/dim]")
    finally:
        # If the launcher exits while a hosted party session is active,
        # tear down party processes as well.
        stop_backend(stop_party=party_active_flag.is_set())
