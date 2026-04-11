import requests
import os
import json
import time
import sys
import subprocess
import atexit
import shutil
import platform
import hashlib
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
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
from contextlib import contextmanager
from utils.os_detector import (
    IS_MACOS,
    IS_WINDOWS,
    OS,
    RAW_OS_NAME,
    RAW_OS_RELEASE,
    RAW_OS_VERSION,
    current_os,
)

API_BASE = "http://localhost:8000"
JSON_DIR = Path(".json")
JSON_DIR.mkdir(exist_ok=True)
CACHE_FILE = str(JSON_DIR / "recent_watch.json")
VERSION = "1.0.0"
CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
console = Console()
backend_process = None
party_proc = None
active_subprocesses = []

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

def stop_backend():
    """Full cleanup of ALL Anilix processes: backend, party server, ngrok, Terminal windows, IPC sockets."""
    global backend_process
    if backend_process:
        try:
            if backend_process.poll() is None:
                console.print("[dim]Stopping Anilix backend[/dim]")
                backend_process.terminate()
                backend_process.wait(timeout=3)
        except:
            if backend_process:
                try: backend_process.kill()
                except: pass
        finally:
            backend_process = None
            
    global active_subprocesses
    for proc in active_subprocesses:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except:
                pass
    active_subprocesses.clear()
            
    # Kill anilix_party Python processes and ngrok
    try:
        import signal
        if IS_WINDOWS:
            # Use taskkill but catch any issues
            subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe", "/T"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Unix-like cleanup
            # pkill returns non-zero if no process found, so ignore errors
            subprocess.run(["pkill", "-f", "anilix_party_admin.py"], stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-f", "anilix_party_client.py"], stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-f", "anilix_party.py"], stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "ngrok"], stderr=subprocess.DEVNULL)
    except Exception:
        pass
    
    # Give Terminal windows a moment to close via '; exit', then force-close stragglers
    if IS_MACOS:
        try:
            time.sleep(0.5)
            # Close any Terminal windows where the shell has already exited
            applescript = '''
            tell application "Terminal"
                repeat with w in windows
                    try
                        repeat with t in tabs of w
                            if busy of t is false then
                                close w saving no
                                exit repeat
                            end if
                        end repeat
                    end try
                end repeat
            end tell
            '''
            subprocess.run(["osascript", "-e", applescript],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
    
    # Cleanup party info file
    try:
        party_info = os.path.join(os.path.dirname(__file__), ".json", "party_info.json")
        if os.path.exists(party_info):
            os.remove(party_info)
    except Exception:
        pass
    
    # Cleanup any leftover IPC socket files (Unix only)
    if not IS_WINDOWS:
        try:
            import glob
            for sock in glob.glob("/tmp/anilix_*.sock"):
                try: os.remove(sock)
                except: pass
        except Exception:
            pass

def _cleanup_party():
    """Kill all party-related processes (server, ngrok, admin/client terminals) without touching the backend."""
    # Kill party scripts
    try:
        if IS_WINDOWS:
            os.system("taskkill /F /IM ngrok.exe /T >nul 2>&1")
        else:
            os.system("pkill -f anilix_party_admin.py >/dev/null 2>&1")
            os.system("pkill -f anilix_party_client.py >/dev/null 2>&1")
            os.system("pkill -f anilix_party.py >/dev/null 2>&1")
            os.system("pkill -9 ngrok >/dev/null 2>&1")
    except Exception:
        pass
    
    # Close leftover Terminal windows on macOS
    if IS_MACOS:
        try:
            time.sleep(0.3)
            applescript = '''
            tell application "Terminal"
                repeat with w in windows
                    try
                        repeat with t in tabs of w
                            if busy of t is false then
                                close w saving no
                                exit repeat
                            end if
                        end repeat
                    end try
                end repeat
            end tell
            '''
            subprocess.run(["osascript", "-e", applescript],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:
            pass
    
    # Remove party_info.json so the next host doesn't read stale data
    try:
        party_info = os.path.join(os.path.dirname(__file__), ".json", "party_info.json")
        if os.path.exists(party_info):
            os.remove(party_info)
    except Exception:
        pass
    
    # Cleanup IPC sockets
    if not IS_WINDOWS:
        try:
            import glob
            for sock in glob.glob("/tmp/anilix_*.sock"):
                try: os.remove(sock)
                except: pass
        except Exception:
            pass
    
    # Remove dead party procs from active_subprocesses
    global active_subprocesses
    active_subprocesses = [p for p in active_subprocesses if p.poll() is None]

def _open_in_new_terminal(script_name, args, title="Anilix"):
    """Open a Python script in a new terminal window. Works on macOS, Windows, and Linux."""
    py = sys.executable
    script = os.path.abspath(script_name)

    proc = None

    if current_os is OS.WINDOWS:
        # Windows: open a new cmd window
        args_str = ' '.join(f'\"{a}\"' for a in args)
        cmd = f'start "{title}" cmd /c "\"{py}\" \"{script}\" {args_str}"'
        proc = subprocess.Popen(cmd, shell=True)

    elif current_os is OS.MACOS:
        # macOS: open a new Terminal.app window via AppleScript
        script_cmd = f'"{py}" "{script}"'
        if args:
            script_cmd += " " + " ".join(f'"{a}"' for a in args)
        script_cmd += "; exit"
        
        # Proper escaping for AppleScript "do script"
        escaped_script = script_cmd.replace('"', '\\"')
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
            cmd_args = [term, "-e", py, script] + list(args)
            proc = subprocess.Popen(cmd_args)
        else:
            console.print("[red]Could not find a suitable terminal emulator![/red]")
    
    if proc:
        active_subprocesses.append(proc)
    return proc

def start_backend():
    global backend_process
    # Check if a server is already running on 8000
    try:
        requests.get(API_BASE, timeout=1)
        # console.print("[dim]Backend already running.[/dim]")
        return
    except requests.RequestException:
        pass

    console.print(Align.center(Panel.fit(f"[bold cyan]✨ ANILIX v{VERSION} ✨[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
    console.print(Align.center("[bold yellow]🚀 Initializing backend server[/bold yellow]"))
    server_path = os.path.join(os.path.dirname(__file__), "anilix_server.py")
    
    # Start the server using uvicorn
    # Ensure .logs directory exists
    log_dir = os.path.join(os.path.dirname(__file__), ".logs")
    os.makedirs(log_dir, exist_ok=True)
    
    # Redirect stderr to a log file for easier debugging
    log_file_path = os.path.join(log_dir, "anilix_backend.log")
    log_file = open(log_file_path, "a")
    log_file.write(f"\n--- SESSION START: {time.ctime()} ---\n")
    log_file.flush()

    try:
        backend_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "anilix_server:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"],
            stdout=log_file,
            stderr=log_file
        )
        atexit.register(stop_backend)
        
        # Wait for backend to be ready
        max_attempts = 15
        for i in range(max_attempts):
            try:
                # Use a specific endpoint to verify health
                requests.get(f"{API_BASE}/", timeout=1)
                # console.print("[green]Backend is ready![/green]")
                return
            except requests.RequestException:
                time.sleep(1)
                
        console.print("[red]❌ Timed out waiting for Anilix backend to start.[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]❌ Failed to start backend: {e}[/red]")
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


def get_mpv_path():
    """Find mpv on the system (Linux/macOS)."""
    cmd = shutil.which("mpv")
    if cmd:
        return cmd
    possible_paths = [
        # Local relative path
        "./mpv.exe",
        "bin/mpv.exe",
        # Linux/macOS
        "/usr/bin/mpv",
        "/usr/local/bin/mpv",
        "/snap/bin/mpv",
        "/opt/homebrew/bin/mpv",
        # Windows common paths
        "C:\\Program Files\\mpv\\mpv.exe",
        "C:\\Program Files\\MPV Player\\mpv.exe",
        "C:\\mpv\\mpv.exe",
        os.path.expanduser("~\\AppData\\Local\\Microsoft\\WindowsApps\\mpv.exe")
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None

def play_video(url, anime_title="Custom Playback", episode_num="", is_custom=False, is_live=False, quality=None, ipc_server=None):
    """Platform-aware video playback using mpv exclusively.
    """
    mpv_path = get_mpv_path()

    # ── Log Playback ──
    try:
        log_dir = os.path.join(os.path.dirname(__file__), ".logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "anilix_backend.log"), "a", encoding='utf-8') as f:
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
            
        args = [mpv_path, title_arg]
        
        if ipc_server:
            args.append(f"--input-ipc-server={ipc_server}")
        
        if is_live:
            args.append("--profile=low-latency")
        
        if quality and str(quality).isdigit():
            # Apply quality constraint for yt-dlp
            q = int(quality)
            args.append(f"--ytdl-format=bestvideo[height<={q}]+bestaudio/best[height<={q}]")
        
        if not is_custom:
            args.extend([
                "--referrer=https://kwik.cx/",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ])
            
        args.append(url)
        
        try:
            mpv_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            while mpv_process.poll() is None:
                time.sleep(0.3)
                # If party server unexpectedly dies (e.g. host clicked X), abort playback immediately
                if ipc_server and party_proc and party_proc.poll() is not None:
                    mpv_process.terminate()
                    break
            
            return True
        except KeyboardInterrupt:
            try: mpv_process.terminate()
            except: pass
            return True
        except Exception as e:
            console.print(f"[bold red]❌ mpv failed to launch:[/bold red] {e}")
            return False
    else:
        # MPV Missing - MANDATORY REQUIREMENT
        console.print("\n" + "="*50)
        console.print("[bold red]❌ ERROR: mpv Player Not Found![/bold red]")
        console.print("[white]Anilix now requires [bold cyan]mpv[/bold cyan] for all playback and tracking.[/white]")
        
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
    if os.path.exists(CACHE_FILE):
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

def save_cache(title, anilist_id, provider, category, episode):
    cache = load_cache()
    a_id = str(anilist_id)
    if a_id not in cache:
        cache[a_id] = {
            "title": title,
            "anilist_id": anilist_id,
            "watched": []
        }
    
    entry = cache[a_id]
    entry["provider"] = provider
    entry["category"] = category
    entry["last_watched_ep"] = str(episode)
    entry["timestamp"] = time.time()
    
    # Store all watched episodes (as a set-like list)
    if "watched" not in entry: entry["watched"] = []
    if str(episode) not in entry["watched"]:
        entry["watched"].append(str(episode))
        
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
        console.print(Align.center(Panel.fit(f"[bold cyan]✨ ANILIX ✨[/bold cyan] | Page {page+1}/{total_pages}", border_style="cyan", box=box.ROUNDED)))
        
        start = page * page_size
        end = start + page_size
        chunk = results[start:end]

        choices = []
        for res in chunk:
            title_dict = res.get("title", {})
            title_str = title_dict.get("english") or title_dict.get("romaji") or "Unknown"
            year = str(res.get("seasonYear") or "?")
            status = res.get("status", "Unknown")
            choices.append(questionary.Choice(
                title=f"{title_str} ({year}) | {status}",
                value=res
            ))
        
        choices.append(questionary.Separator())
        
        # Navigation controls
        if total_pages > 1:
            if page < total_pages - 1:
                choices.append(questionary.Choice("➡️ Next Page", value="next"))
            if page > 0:
                choices.append(questionary.Choice("⬅️ Previous Page", value="prev"))
        
        choices.append(questionary.Choice("🔙 Go Back", value="back"))

        selected = questionary.select(
            "Select an anime:",
            choices=choices,
            instruction="(Use arrows to navigate, Enter to select)"
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

def display_anime_details(selected_anime):
    console.clear()
    console.print(Align.center(Panel.fit("[bold cyan]✨ ANILIX ✨[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
    
    anilist_id = selected_anime.get("id")
    # Fetch full info (with caching)
    with status_after("[yellow]📖 Loading details[/yellow]"):
        try:
            full_info = fetch_json(f"{API_BASE}/info/{anilist_id}")
            if full_info:
                selected_anime = full_info
        except:
            pass # Use partial info from search results if details fetch fails

    title_dict = selected_anime.get("title", {})
    t_str = title_dict.get("english") or title_dict.get("romaji") or "Unknown"
    t_romaji = title_dict.get("romaji", "")
    t_native = title_dict.get("native", "")
    
    console.clear()
    
    # ── Big Title ──
    console.print()
    console.print(Align.center(Text(f"  {t_str}  ", style="bold white on dark_blue")))
    if t_romaji and t_romaji != t_str:
        console.print(Align.center(f"[dim italic]{t_romaji}[/dim italic]"))
    if t_native:
        console.print(Align.center(f"[dim]{t_native}[/dim]"))
    console.print()
    

    # ── Score + Quick Stats Line ──
    status = selected_anime.get("status", "Unknown")
    episodes = selected_anime.get("episodes", "?")
    season = selected_anime.get("season", "")
    year = selected_anime.get("seasonYear", "?")
    fmt = selected_anime.get("format", "Unknown")
    avg_score = selected_anime.get("averageScore")
    popularity = selected_anime.get("popularity", "?")
    favourites = selected_anime.get("favourites", "?")
    genres = selected_anime.get("genres", [])
    
    stats_table = Table(show_header=False, box=box.SIMPLE_HEAVY, expand=False, padding=(0, 3))
    stats_table.add_column("Label", style="bold cyan")
    stats_table.add_column("Value", style="white")
    
    stats_table.add_row("⭐ Score", score_bar(avg_score))
    stats_table.add_row("📺 Status", f"[bold yellow]{status}[/bold yellow]")
    stats_table.add_row("🎬 Episodes", f"[bold green]{episodes}[/bold green]")
    stats_table.add_row("📅 Season", f"[bold blue]{season} {year}[/bold blue]")
    stats_table.add_row("🎥 Format", f"{fmt}")
    stats_table.add_row("❤️  Favourites", f"{favourites}")
    stats_table.add_row("📈 Popularity", f"{popularity}")
    
    # Studios
    studios_data = selected_anime.get("studios", {}).get("nodes", [])
    studio_names = [s.get("name", "") for s in studios_data if s.get("isAnimationStudio")]
    if studio_names:
        stats_table.add_row("🏢 Studio", f"[bold magenta]{', '.join(studio_names)}[/bold magenta]")
    
    console.print(Align.center(stats_table))
    # ── Genres ──
    if genres:
        genre_tags = "  ".join([f"[bold white on dark_green] {g} [/bold white on dark_green]" for g in genres])
        console.print()
        console.print(Align.center(genre_tags))
    
    # ── Description ──
    description = selected_anime.get("description", "")
    if description:
        # Clean up HTML tags from description
        import re
        clean_desc = re.sub(r'<[^>]+>', '', description).strip()
        # No truncation for synopsis
        console.print()
        console.print(Align.center(Rule("Synopsis", style="cyan")))
        console.print()
        # Keep descriptions clean without excessive framing
        console.print(clean_desc, width=min(90, console.width - 4), justify="center")
    
    # ── Next Airing ──
    next_ep = selected_anime.get("nextAiringEpisode")
    if next_ep:
        ep_num = next_ep.get("episode", "?")
        time_left = next_ep.get("timeUntilAiring", 0)
        days = time_left // 86400
        hours = (time_left % 86400) // 3600
        console.print()
        console.print(Align.center(f"[bold yellow]⏰ Next Episode: {ep_num} airing in {days}d {hours}h[/bold yellow]"))
    
    console.print()
    return t_str, anilist_id

def handle_episode_flow(anilist_id, t_str, pre_provider=None, pre_category=None, ipc_server_path=None):
    with status_after(f"[yellow]🔍 Fetching providers for {t_str}[/yellow]"):
        try:
            # Metadata stays for 24h
            providers = fetch_json(f"{API_BASE}/episodes/{anilist_id}", ttl_hours=24)
        except Exception as e:
            console.print(f"[red]❌ Connection Error: {e}[/red]")
            console.print("[dim]Check '.logs/anilix_backend.log' for details.[/dim]")
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
            console.print(Align.center(Panel.fit(f"[bold magenta]{t_str}[/bold magenta]", border_style="magenta", box=box.ROUNDED)))
            
            p_names = list(providers.keys())
            p_choices = [questionary.Choice(name.upper(), value=name) for name in p_names]
            p_choices.append(questionary.Separator())
            p_choices.append(questionary.Choice("🔙 Back to Main Menu", value="back"))
            
            session_provider = questionary.select(
                "Select Provider:",
                choices=p_choices,
                instruction="(Arrows to navigate)"
            ).ask()
            
            if session_provider is None:
                return
            
            if session_provider == "back":
                return

        if not session_category:
            console.clear()
            header = f"[bold magenta]{t_str}[/bold magenta] | Provider: [bold cyan]{session_provider.upper()}[/bold cyan] | Auto-Play: {auto_label}"
            console.print(Align.center(Panel.fit(header, border_style="magenta", box=box.ROUNDED)))
            
            categories = list(providers[session_provider].get("episodes", {}).keys())
            cat_choices = [questionary.Choice(f"🔄 Toggle Auto-Play ({auto_text})", value="toggle_auto")]
            cat_choices.extend([questionary.Choice(c.upper(), value=c) for c in categories])
            cat_choices.append(questionary.Separator())
            cat_choices.append(questionary.Choice("🔙 Change Provider", value="back"))
            
            session_category = questionary.select(
                "Select Category:",
                choices=cat_choices
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

        # Select Episode with Questionary
        ep_choices = []
        for ep in ep_list:
            e_num = str(ep.get('number'))
            prefix = "✅ " if e_num in watched_map else "   "
            if e_num == last_watched:
                prefix = "▶️ " # Indicator for current/last watching
            
            ep_choices.append(questionary.Choice(
                title=f"{prefix}Episode {e_num} {ep.get('title', '')}", 
                value=ep
            ))
            
        ep_choices.append(questionary.Separator())
        ep_choices.append(questionary.Choice("🔙 Back to Categories", value="back"))
        
        selected_ep = questionary.select(
            f"Select Episode for {t_str}:",
            choices=ep_choices
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
        stream_choices = [
            questionary.Choice(
                title=f"🚀 Play: {str(s.get('quality', 'Unknown')).upper()} | {s.get('url', '')}", 
                value=s
            )
            for s in streams
        ]
        stream_choices.append(questionary.Separator())
        stream_choices.append(questionary.Choice("🔙 Back to Episodes", value="back"))
        
        selected_stream = questionary.select(
            f"Select Quality (Ep {ep_num}):",
            choices=stream_choices
        ).ask()
        
        if selected_stream is None:
            continue
            
        if selected_stream == "back":
            continue
            
        while True:
            selected_url = selected_stream.get("url")
            if selected_url:
                play_video(selected_url, t_str, ep_num, ipc_server=ipc_server_path)
                
                save_cache(t_str, anilist_id, session_provider, session_category, ep_num)
            
                # --- Auto Next Logic ---
                current_idx = next((i for i, e in enumerate(ep_list) if str(e.get('number')) == ep_num), -1)
                if current_idx != -1 and current_idx + 1 < len(ep_list):
                    next_ep = ep_list[current_idx + 1]
                    next_num = str(next_ep.get('number'))
                    
                    if auto_play:
                        console.print(f"\n[bold green]✅ Episode Finished[/bold green]")

                        is_windows = IS_WINDOWS
                        should_play = False
                        
                        if is_windows:
                            import msvcrt
                            paused = False
                            time_left = 3.0
                            start_time = time.time()
                            
                            while time_left > 0:
                                display = int(time_left) + 1
                                if paused:
                                    console.print(f"[bold yellow]⏭️  Auto-Play PAUSED[/bold yellow] ([cyan]'p' to Resume[/cyan] | [red]'c' to Cancel[/red] | [green]Enter to Play[/green])        ", end="\r")
                                else:
                                    console.print(f"[bold yellow]⏭️  Launching Episode {next_num} in {display}s[/bold yellow] ([cyan]'p' to Pause[/cyan] | [red]'c' to Cancel[/red] | [green]Enter to Play[/green])        ", end="\r")
                                
                                if msvcrt.kbhit():
                                    char = msvcrt.getch().decode("utf-8", "ignore").lower()
                                    if char == 'c':
                                        should_play = False
                                        break
                                    elif char == 'p':
                                        paused = not paused
                                        if not paused:
                                            start_time = time.time() - (3.0 - time_left)
                                    elif char in ['\r', '\n', ' ']:
                                        should_play = True
                                        break
                                
                                time.sleep(0.05)
                                if not paused:
                                    time_left = 3.0 - (time.time() - start_time)
                            
                            if time_left <= 0:
                                should_play = True
                                
                            console.print()
                            
                            if not should_play:
                                console.print(f"[bold red]🛑 Auto-Play Cancelled[/bold red]")
                                break
                        else:
                            try:
                                for i in range(3, 0, -1):
                                    console.print(f"[bold yellow]⏭️  Launching Episode {next_num} in {i} (Press Ctrl+C to cancel)[/bold yellow]  ", end="\r")
                                    time.sleep(1)
                                console.print() # Move to next line
                                should_play = True
                            except KeyboardInterrupt:
                                console.print(f"\n[bold red]🛑 Auto-Play Cancelled[/bold red]")
                                should_play = False
                                break # Exit the playback loop to return to episode list
                    else:
                        console.print(f"\n[bold green]✅ Episode Finished![/bold green]")
                        should_play = questionary.confirm(f"Play Episode {next_num} next?", default=True).ask()
                        
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
                    break

def main():
    start_backend()
    console.print(
        f"[dim]Detected OS: {current_os.value} | raw={RAW_OS_NAME} {RAW_OS_RELEASE} | version={RAW_OS_VERSION}[/dim]"
    )
    search_history = []
    
    # ── Mode Selection ──
    console.clear()
    console.print(Align.center(Panel.fit(f"[bold cyan]✨ ANILIX v{VERSION} | TERMINAL ANIME INTERFACE ✨[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
    
    mode = questionary.select(
        "How do you want to watch?",
        choices=[
            questionary.Choice("🎬 Solo (Watch Alone)", value="solo"),
            questionary.Choice("🎉 Party (Watch Together)", value="party"),
            questionary.Separator(),
            questionary.Choice("🚪 Exit", value="exit"),
        ],
        instruction="(Select with arrows)"
    ).ask()
    
    if mode is None or mode == "exit":
        console.print(Align.center("[bold magenta]Goodbye! 🎉[/bold magenta]"))
        return
    
    # ── Party sub-menu ──
    global party_proc
    party_active = False
    party_proc = None
    ipc_server_path = None
    
    if mode == "party":
        console.clear()
        console.print(Align.center(Panel.fit("[bold cyan]🎉 WATCH PARTY MODE[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
        
        party_choice = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice("📡 Host a Party", value="host"),
                questionary.Choice("🔗 Join a Party", value="join"),
                questionary.Choice("🔙 Back", value="back"),
            ]
        ).ask()
        
        if party_choice is None or party_choice == "back":
            return main()  # Restart
        
        if party_choice == "join":
            console.clear()
            console.print(Align.center(Panel.fit("[bold cyan]🔗 JOIN WATCH PARTY[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
            party_url = questionary.text("Enter Party Link (wss://... or ws://...):").ask()
            if not party_url:
                return
            # Normalize URL: accept any format the user pastes
            party_url = party_url.strip()
            if party_url.startswith("https://"):
                party_url = party_url.replace("https://", "wss://", 1)
            elif party_url.startswith("http://"):
                party_url = party_url.replace("http://", "ws://", 1)
            elif not party_url.startswith("ws://") and not party_url.startswith("wss://"):
                party_url = "wss://" + party_url
            
            username = questionary.text("Enter Your Name:").ask()
            if not username:
                username = f"Guest_{int(time.time())%1000}"
            
            console.print("[yellow]Connecting... a new window will open for chat.[/yellow]")
            _open_in_new_terminal("anilix_party_client.py", [party_url, username], title="Anilix Client")
            console.print("[bold green]✅ Client window launched![/bold green]")
            input("Press Enter to return to menu...")
            return main()
        
        if party_choice == "host":
            import uuid
            uid = str(uuid.uuid4())[:8]
            ipc_server_path = fr"\\.\pipe\anilix_host_{uid}" if IS_WINDOWS else f"/tmp/anilix_host_{uid}.sock"
            
            room_name = questionary.text("Room Name:", default=f"{os.getlogin()}'s Party").ask()
            host_name = questionary.text("Your Host Name:", default="Host").ask()
            
            if not room_name or not host_name:
                return
            
            console.print("[yellow]Starting party server and ngrok tunnel...[/yellow]")
            
            # Clean up any leftover party processes from a previous session
            _cleanup_party()
            time.sleep(0.5)  # Give port 9000 time to be released
            party_log_file = open(os.path.join(os.path.dirname(__file__), ".logs", "anilix_backend.log"), "a")
            party_log_file.write(f"\n--- WATCH PARTY SERVER START: {time.ctime()} ---\n")
            party_log_file.flush()
            party_proc = subprocess.Popen(
                [sys.executable, "anilix_party.py", room_name, host_name],
                stdout=party_log_file,
                stderr=party_log_file
            )
            active_subprocesses.append(party_proc)
            
            party_info_path = os.path.join(os.path.dirname(__file__), ".json", "party_info.json")
            for _ in range(20):
                if os.path.exists(party_info_path):
                    break
                time.sleep(0.5)
                
            if os.path.exists(party_info_path):
                with open(party_info_path, "r") as f:
                    info = json.load(f)
                    console.print(f"\n[bold green]✅ Party ready! Share this link with friends:[/bold green]")
                    console.print(Align.center(Panel.fit(f"[bold yellow]{info['url']}[/bold yellow]", border_style="green", box=box.ROUNDED)))
                    
                    # Auto-copy URL to clipboard based on OS natively
                    try:
                        if IS_WINDOWS:
                            subprocess.run(['clip'], input=info['url'].encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        elif IS_MACOS:
                            subprocess.run(['pbcopy'], input=info['url'].encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        else:
                            import shutil
                            if shutil.which('xclip'):
                                subprocess.run(['xclip', '-selection', 'clipboard'], input=info['url'].encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            elif shutil.which('Wayland'):
                                subprocess.run(['wl-copy'], input=info['url'].encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        console.print(Align.center("[dim italic](Link automatically copied to your clipboard!)[/dim italic]"))
                    except BaseException:
                        pass
                
                _open_in_new_terminal("anilix_party_admin.py", [host_name, ipc_server_path or ""], title="Anilix Admin")
                
                console.print("[dim]Admin console opened in a new window.[/dim]")
                party_active = True
            else:
                console.print("[red]❌ Failed to start party server. Continuing in solo mode.[/red]")
                if party_proc:
                    party_proc.terminate()
                    party_proc = None
            
            console.print("\n[bold cyan]Now pick what to watch! 🍿[/bold cyan]")
            time.sleep(1.5)
    
    # ── Main Loop (shared by Solo and Party Host) ──
    while True:
        if party_active and party_proc and party_proc.poll() is not None:
            console.print("[yellow]Party session ended. Returning to home menu...[/yellow]")
            time.sleep(1)
            return main()

        console.print()
        console.clear()
        
        mode_label = "[bold green]PARTY HOST[/bold green] 🎉" if party_active else "[bold blue]SOLO[/bold blue] 🎬"
        console.print(Align.center(Panel.fit(f"[bold cyan]✨ ANILIX v{VERSION}[/bold cyan] | {mode_label}", border_style="cyan", box=box.ROUNDED)))
        
        cache = load_cache()
        
        menu_choices = [
            questionary.Choice("🔍 Search Anime", value="search"),
            questionary.Choice("🔥 Discover Trending", value="trending")
        ]
        if cache:
            menu_choices.append(questionary.Choice("📚 View Watch History", value="history"))
            
        menu_choices.append(questionary.Choice("▶️ Play Custom Video (Local/URL)", value="custom_play"))
        
        menu_choices.append(questionary.Separator())
        menu_choices.append(questionary.Choice("🔙 Back", value="back"))
        menu_choices.append(questionary.Choice("🚪 Exit", value="exit"))
        
        choice = questionary.select(
            "What would you like to do?",
            choices=menu_choices,
            instruction="(Select with arrows)"
        ).ask()
        
        if choice is None or choice == "exit":
            console.print(Align.center("[bold magenta]Goodbye! 🎉[/bold magenta]"))
            break
        
        if choice == "back":
            _cleanup_party()
            return main()
            
        elif choice == 'custom_play':
            console.clear()
            console.print(Align.center(Panel.fit("[bold cyan]▶️ CUSTOM VIDEO PLAYBACK[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
            console.print("[dim]Supported: Local files, HTTP Links, YouTube URLs (yt-dlp is installed for mpv)[/dim]\n")
            
            sub_choice = questionary.select(
                "Choose playback source:",
                choices=[
                    questionary.Choice("📁 Select Local File", value="local"),
                    questionary.Choice("🔗 Paste Link", value="link"),
                    questionary.Choice("📡 Live Stream Link (Low Latency)", value="live"),
                    questionary.Choice("🔙 Back", value="back")
                ]
            ).ask()
            
            if not sub_choice or sub_choice == "back":
                continue
                
            is_live_stream = (sub_choice == "live")
                
            if sub_choice == "local":
                if IS_MACOS:
                    try:
                        result = subprocess.run(
                            ['osascript', '-e', 'POSIX path of (choose file with prompt "Select Video File")'],
                            capture_output=True, text=True
                        )
                        custom_path = result.stdout.strip()
                    except Exception:
                        custom_path = ""
                else:
                    import tkinter as tk
                    from tkinter import filedialog
                    
                    root = tk.Tk()
                    root.attributes("-topmost", True)
                    root.withdraw()
                    
                    custom_path = filedialog.askopenfilename(
                        title="Select Video File",
                        filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.webm *.mov"), ("All Files", "*.*")]
                    )
                    root.destroy()
                
                if not custom_path:
                    continue
            else:
                prompt_text = "Enter live stream URL:" if is_live_stream else "Enter video URL:"
                custom_path = questionary.text(prompt_text).ask()
                if not custom_path:
                    continue
                
            clean_path = custom_path.strip("\"'")
            if sub_choice in ["link", "live"] and not clean_path.startswith("http"):
                clean_path = "https://" + clean_path
                
            is_url = clean_path.startswith("http://") or clean_path.startswith("https://")
            
            if not is_url and not os.path.exists(clean_path):
                console.print(f"[red]❌ File not found and does not look like a valid URL:[/red] {clean_path}")
                time.sleep(2)
                continue

            selected_quality = None
            if is_url:
                selected_quality = questionary.select(
                    "Select Max Quality:",
                    choices=[
                        questionary.Choice("🚀 Best Available", value="best"),
                        questionary.Choice("💎 1080p", value="1080"),
                        questionary.Choice("🎬 720p", value="720"),
                        questionary.Choice("📺 480p", value="480"),
                        questionary.Choice("📟 360p", value="360"),
                    ]
                ).ask()
                if selected_quality == "best":
                    selected_quality = None

            with status_after("[yellow]▶️ Preparing playback...[/yellow]", center=True):
                time.sleep(0.5)
            
            play_video(clean_path, anime_title="Custom Playback", episode_num="", is_custom=True, is_live=is_live_stream, quality=selected_quality)
            
        elif choice == 'search':
            # Search with History helper
            search_query_choices = [questionary.Choice(f"Recent: {q}", value=q) for q in search_history[:5]]
            if search_query_choices:
                search_query_choices.append(questionary.Separator())
            search_query_choices.append(questionary.Choice("🆕 New Search", value="new"))
            
            if search_history:
                query_choice = questionary.select("Search Anime:", choices=search_query_choices).ask()
                if query_choice == "new":
                    query = questionary.text("Enter anime title:").ask()
                else:
                    query = query_choice
            else:
                query = questionary.text("Enter anime title:").ask()
                
            if not query:
                search_history.pop(0) if search_history else None
                continue
            if query not in search_history:
                search_history.insert(0, query)
                
            with status_after(f"[yellow]🔍 Searching for '{query}'[/yellow]", center=True):
                try:
                    # Search results stay for 1h
                    data = fetch_json(f"{API_BASE}/search", params={"query": query}, ttl_hours=1)
                    results = data.get("results", [])
                except Exception as e:
                    console.print(f"[red]❌ Connection Error: {e}[/red]")
                    console.print("[dim]Check '.logs/anilix_backend.log' for details.[/dim]")
                    continue
            
            if not results:
                console.print(Align.center("[red]❌ No anime found![/red]"))
                time.sleep(1.5)
                continue
                
            selected_anime = show_anime_grid(results)
            if selected_anime:
                t_str, anilist_id = display_anime_details(selected_anime)
                handle_episode_flow(anilist_id, t_str, ipc_server_path=ipc_server_path)
                
        elif choice == 'trending':
            with status_after(f"[yellow]🔥 Fetching Trending Anime[/yellow]", center=True):
                try:
                    # Trending stays for 1h
                    data = fetch_json(f"{API_BASE}/trending", ttl_hours=1)
                    results = data.get("results", [])
                except Exception as e:
                    console.print(f"[red]❌ Connection Error: {e}[/red]")
                    console.print("[dim]Check '.logs/anilix_backend.log' for details.[/dim]")
                    continue
            
            if not results:
                console.print(Align.center("[red]❌ No trending anime found![/red]"))
                time.sleep(1.5)
                continue
                
            selected_anime = show_anime_grid(results)
            if selected_anime:
                t_str, anilist_id = display_anime_details(selected_anime)
                handle_episode_flow(anilist_id, t_str, ipc_server_path=ipc_server_path)
                
        elif choice == 'history' and cache:
            console.clear()
            console.print(Align.center(Panel.fit("[bold cyan]📚 WATCH HISTORY[/bold cyan]", border_style="cyan", box=box.ROUNDED)))
            
            # Sort by most recent
            sorted_history = sorted(cache.values(), key=lambda x: x.get("timestamp", 0), reverse=True)
            
            h_choices = []
            for item in sorted_history:
                title = item.get("title", "Unknown")
                prov = item.get("provider", "?")
                ep = item.get("last_watched_ep", "?")
                h_choices.append(questionary.Choice(
                    f"{title} | {prov.upper()} (Ep {ep})",
                    value=item
                ))
            
            h_choices.append(questionary.Separator())
            h_choices.append(questionary.Choice("🔙 Back", value="back"))
            
            selected_item = questionary.select("Resume watching:", choices=h_choices).ask()
            
            if selected_item == "back":
                continue
            
            mock_anime = {"id": selected_item["anilist_id"], "title": {"english": selected_item["title"]}}
            t_str, anilist_id = display_anime_details(mock_anime)
            handle_episode_flow(
                anilist_id, 
                t_str, 
                pre_provider=selected_item.get("provider"),
                pre_category=selected_item.get("category"),
                ipc_server_path=ipc_server_path
            )
        else:
            console.print("[red]❌ Invalid option![/red]")

if __name__ == "__main__":
    import signal
    
    def _signal_cleanup(signum, frame):
        """Handle SIGTERM/SIGINT for clean shutdown."""
        console.print("\n[dim]Received shutdown signal, cleaning up...[/dim]")
        stop_backend()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, _signal_cleanup)
    signal.signal(signal.SIGINT, _signal_cleanup)
    
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[bold magenta]Goodbye! ✨ Logging out of Anilix[/bold magenta]")
    except Exception as e:
        console.print(f"\n\n[bold red]❌ CRITICAL ERROR:[/bold red] {e}")
        console.print("[dim]Backend has been safely shut down.[/dim]")
    finally:
        stop_backend()
        sys.exit(0)
