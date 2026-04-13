import asyncio
import uuid

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import json
import websockets
from websockets.protocol import State
import time
import subprocess
import os
import ctypes
import socket
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich.align import Align
from rich.markup import escape
from rich.console import Group
from shared.utils.os_detector import IS_WINDOWS
from features.voice_chat.voice_manager import VoiceManager
from shared.utils.logger import setup_logger
from core.paths import SOUND_ASSETS_DIR

# Initialize client session logger
logger = setup_logger("client_tui", "client_session.log")

console = Console()
CHAT_TOKEN = "__CHAT__"

class PartyClient:
    def __init__(self, ws_url, username):
        self.ws_url = ws_url
        self.username = username
        self.ws = None
        self.running = True
        
        self.room_name = "Joining..."
        self.chat_history = []
        self.users = []
        self.input_text = ""
        self.scroll_offset = 0 # Track how many lines we are scrolled up
        self.mpv_process = None
        unique_id = uuid.uuid4().hex[:10]
        self.mpv_ipc_path = fr"\\.\pipe\streamix_client_{unique_id}" if IS_WINDOWS else f"/tmp/streamix_client_{unique_id}.sock"
        
        # Local-only filters (only affect this user's view)
        self.local_muted = set()
        self.local_deafened = set()
        self.current_video_url = None
        
        # Load persistent config
        from core.config import get_client_config
        self.config = get_client_config()
        self.volume = self.config.get("volume", 100)
        self.notifications_enabled = self.config.get("notifications", True)
        self.mic_muted = True 
        self.speaker_muted = False
        
        # Audio restrictions from server (Admin forced)
        self.admin_muted = False
        self.admin_deafened = False
        
        self.voice_manager = None
        self.chat_limit = self.config.get("chat_history_limit", 50)
        self._last_sound_times = {}
        self._mpv_path = None

    def _append_chat(self, sender, message, ts=None):
        if not ts: ts = time.strftime("%H:%M")
        payload = {"sender": sender, "message": message, "time": ts}
        self.chat_history.append(f"{CHAT_TOKEN}{json.dumps(payload, ensure_ascii=False)}")

    async def _send_mpv_command(self, command):
        """Sends an IPC command to mpv if it's running."""
        try:
            if IS_WINDOWS:
                with open(self.mpv_ipc_path, "w") as f:
                    f.write(json.dumps({"command": command}) + "\n")
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.mpv_ipc_path)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                s.close()
        except Exception:
            pass

    def _get_mpv_property_sync(self, prop):
        """Synchronously request a property from mpv via IPC."""
        try:
            if not IS_WINDOWS and not os.path.exists(self.mpv_ipc_path):
                return None

            if IS_WINDOWS:
                with open(self.mpv_ipc_path, 'r+') as f:
                    f.write(json.dumps({"command": ["get_property", prop]}) + "\n")
                    f.flush()
                    res = json.loads(f.readline())
                    return res.get("data")
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.mpv_ipc_path)
                s.sendall((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
                res = json.loads(s.recv(1024).decode().split('\n')[0])
                s.close()
                return res.get("data")
        except Exception:
            return None

    async def _wait_for_mpv_ipc_ready(self, timeout=3.0):
        """Wait briefly until mpv IPC responds to property queries."""
        start = time.time()
        while (time.time() - start) < timeout:
            if self.mpv_process and self.mpv_process.poll() is not None:
                return False

            pause_state = await asyncio.to_thread(self._get_mpv_property_sync, "pause")
            if isinstance(pause_state, bool):
                return True

            await asyncio.sleep(0.1)

        return False

    def _play_event_sound(self, filename):
        if not self.notifications_enabled:
            return

        # Per-sound cooldown keeps rapid chat alerts responsive without spam.
        now = time.time()
        cooldown = 0.35 if filename == "notification.mp3" else 0.8
        last_time = self._last_sound_times.get(filename, 0.0)
        if now - last_time < cooldown:
            return
        self._last_sound_times[filename] = now
        
        try:
            if not self._mpv_path:
                from shared.media import get_mpv_path
                self._mpv_path = get_mpv_path()

            if self._mpv_path:
                sound_path = SOUND_ASSETS_DIR / filename
                if sound_path.exists():
                    subprocess.Popen([self._mpv_path, "--no-video", "--no-terminal", str(sound_path)], 
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    async def connect_and_listen(self):
        try:
            # Build connection kwargs for ngrok compatibility
            connect_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 10,
                "open_timeout": 15,
                "additional_headers": {
                    "User-Agent": "Streamix-Party-Client/1.0",
                    "ngrok-skip-browser-warning": "true",
                },
            }
            
            # Handle wss:// (ngrok HTTPS tunnels) — need SSL context
            if self.ws_url.startswith("wss://"):
                import ssl
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                connect_kwargs["ssl"] = ssl_ctx
            
            max_retries = 5
            retry_delay = 2.0
            
            for attempt in range(1, max_retries + 1):
                try:
                    # DEEP SANITIZATION: Strip ALL non-standard URL characters
                    # This removes invisible junk (Unicode boms, zero-width spaces, etc.)
                    import re
                    original_url = self.ws_url
                    self.ws_url = re.sub(r"[^a-zA-Z0-9\.\-\:\/\?\=\&\_]", "", self.ws_url)
                    
                    if original_url != self.ws_url:
                        logger.warning(f"[NETWORK] Sanitized URL from '{original_url}' to '{self.ws_url}'")
                    
                    target_display = self.ws_url
                    if len(target_display) > 40:
                        target_display = target_display[:37] + "..."
                        
                    self.chat_history.append(f"[dim]Connecting to {target_display} (attempt {attempt}/{max_retries})...[/dim]")
                    self.ws = await websockets.connect(self.ws_url, **connect_kwargs)
                    logger.info(f"[LIFECYCLE] Connected to party server at {self.ws_url}")
                    break
                except (ConnectionRefusedError, OSError, websockets.exceptions.InvalidStatusCode) as e:
                    import socket
                    if isinstance(e, socket.gaierror) or "getaddrinfo" in str(e):
                        # DNS Failure - Log Hex for deep debugging
                        url_hex = self.ws_url.encode('utf-8').hex()
                        logger.error(f"[NETWORK] DNS Resolution failed for URL: {self.ws_url} (Hex: {url_hex})")
                        error_hint = "Check for typos or spaces in URL"
                    else:
                        error_hint = str(e)
                        
                    if attempt < max_retries:
                        self.chat_history.append(f"[dim]Could not reach server, retrying in {retry_delay:.0f}s... ([red]{error_hint}[/red])[/dim]")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 15.0)
                    else:
                        raise e
            
            # Join as member
            await self.ws.send(
                json.dumps({"type": "join", "name": self.username, "role": "member"}, ensure_ascii=False)
            )
            
            # Start Voice Manager with persistent config
            from core.config import load_config
            cfg = load_config()["client"]
            mic_idx = cfg.get("mic_device_index")
            
            loop = asyncio.get_event_loop()
            self.voice_manager = VoiceManager(loop, input_device=mic_idx)
            self.voice_manager.on_voice_packet = self._voice_packet_callback
            self.voice_manager.mic_muted = self.mic_muted
            self.voice_manager.speaker_muted = self.speaker_muted
            self.voice_manager.start()
            logger.info(f"[LIFECYCLE] Voice Manager started (Input Index: {mic_idx})")
            
            # Message loop
            async for message in self.ws:
                if not self.running:
                    break
                
                if isinstance(message, bytes):
                    # Protocol: [1 byte: hash_len] + [N bytes: hash_id] + [Binary: compressed_audio]
                    try:
                        hash_len = int(message[0])
                        sender_hash = message[1:1+hash_len].decode('utf-8')
                        audio_payload = message[1+hash_len:]
                        
                        # Apply Local Mute/Deafen filter using Hash-ID
                        if sender_hash in self.local_muted or sender_hash in self.local_deafened:
                            continue
                            
                        if self.voice_manager:
                            self.voice_manager.handle_incoming_audio(audio_payload)
                    except Exception as e:
                        logger.error(f"Error parsing voice packet header: {e}")
                    continue
                    
                try:
                    data = json.loads(message)
                    mt = data.get("type")
                    
                    if mt == "room_state":
                        self.room_name = data.get("room_name", "Party Room")
                        playback = data.get("playback", {})
                        self.chat_history.append(f"[bold green]Joined room '{self.room_name}'.[/bold green]")
                        
                        # Launch mpv
                        if playback and playback.get('url'):
                            url = playback.get('url')
                            title = playback.get('anime_title', 'Party Video')
                            timestamp = playback.get('timestamp', 0)
                            state = playback.get('state', 'paused')
                            
                            launched = self._launch_mpv(url, title, timestamp)
                            if launched:
                                self.current_video_url = url
                                await self._wait_for_mpv_ipc_ready(timeout=4.0)
                            
                            # pause if host is paused
                            if state == 'paused':
                                await self._send_mpv_command(["set_property", "pause", True])
                                
                    elif mt == "user_list":
                        self.users = data.get("users", [])
                        
                        # Find self in list to check for forced mute/deafen
                        for u in self.users:
                            if u.get('name') == self.username:
                                server_muted = u.get('muted', False)
                                server_deafened = u.get('deafened', False)
                                
                                # 1. Synchronize Mic Mute
                                if server_muted and not self.admin_muted:
                                    self.admin_muted = True
                                    self._append_chat("System", "[bold red]Your microphone has been muted by the Administrator.[/bold red]")
                                    # Force-mute the local hardware
                                    self.mic_muted = True
                                    if self.voice_manager: self.voice_manager.mic_muted = True
                                elif not server_muted and self.admin_muted:
                                    self.admin_muted = False
                                    self._append_chat("System", "[bold green]Your microphone has been unmuted by the Administrator.[/bold green]")
                                    # Safety First: User must manually press Ctrl+K to re-enable mic.
                                
                                # 2. Synchronize Speaker Deafen
                                if server_deafened and not self.admin_deafened:
                                    self.admin_deafened = True
                                    self._append_chat("System", "[bold red]You have been deafened by the Administrator.[/bold red]")
                                    self.speaker_muted = True
                                    if self.voice_manager: self.voice_manager.speaker_muted = True
                                elif not server_deafened and self.admin_deafened:
                                    self.admin_deafened = False
                                    self._append_chat("System", "[bold green]The deafen restriction was lifted by the Administrator.[/bold green]")
                                break
                        
                    elif mt == "chat":
                        sender = data.get("sender", "Unknown")
                        s_hash = data.get("hash_id")
                        msg = data.get("message", "")
                        # Local filter: skip if we locally muted or deafened this user
                        if s_hash in self.local_muted or s_hash in self.local_deafened:
                            continue
                        self._append_chat(sender, msg)
                        if sender != self.username:
                            self._play_event_sound("notification.mp3")
                            
                    elif mt == "chat_history":
                        history = data.get("history", [])
                        for m in history:
                            m_type = m.get("type", "chat")
                            if m_type == "chat":
                                self._append_chat(m.get("sender"), m.get("message"), ts=m.get("time"))
                            elif m_type == "system":
                                msg = m.get("message", "")
                                self.chat_history.append(f"[dim italic]{escape(msg)}[/dim italic]")
                            elif m_type == "sync":
                                playback = m.get("playback", {})
                                state = playback.get("state", "updated")
                                title = playback.get("anime_title", "Nothing")
                                if title != "Nothing" and state not in ["closed", "updated"]:
                                    self.chat_history.append(f"[dim grey][Sync] {title} {state}[/dim grey]")
                        
                    elif mt == "system":
                        msg = data.get("message", "")
                        subtype = data.get("subtype")
                        self.chat_history.append(f"[dim italic]{escape(msg)}[/dim italic]")
                        
                        if subtype == "join" and data.get("actor") != self.username:
                            self._play_event_sound("joinin.mp3")
                        elif subtype == "leave" and data.get("actor") != self.username:
                            self._play_event_sound("leave.mp3")
                        
                    elif mt == "sync":
                        playback = data.get("playback", {})
                        if playback:
                            url = playback.get("url")
                            title = playback.get("anime_title", "Party Video")
                            timestamp = playback.get("timestamp", 0)
                            state = playback.get("state", "playing")
                            
                            if state == "closed":
                                if getattr(self, 'mpv_process', None):
                                    try: await self._send_mpv_command(["quit"])
                                    except: pass
                                    try: self.mpv_process.terminate()
                                    except: pass
                                    self.mpv_process = None
                                self.current_video_url = None
                                continue  # Skip the rest of the sync logic
                            
                            # Auto-launch or restart mpv when needed.
                            # This also recovers if local MPV exits while the host keeps playing
                            # the same URL (common on same-device host/client testing).
                            process_missing = (not getattr(self, 'mpv_process', None)) or (self.mpv_process.poll() is not None)
                            needs_relaunch = (self.current_video_url != url) or process_missing

                            if url and needs_relaunch:
                                if getattr(self, 'mpv_process', None):
                                    try:
                                        self.mpv_process.terminate()
                                    except:
                                        pass
                                launched = self._launch_mpv(url, title, timestamp)
                                if launched:
                                    self.current_video_url = url
                                    await self._wait_for_mpv_ipc_ready(timeout=4.0)
                                else:
                                    self.current_video_url = None
                                    continue

                            if not getattr(self, 'mpv_process', None) or self.mpv_process.poll() is not None:
                                self.current_video_url = None
                                continue

                            ipc_ready = await self._wait_for_mpv_ipc_ready(timeout=1.2)
                            if not ipc_ready:
                                continue
                                
                            # Sync mpv (sync is never locally filtered — host controls playback)
                            await self._send_mpv_command(["set_property", "pause", state == "paused"])
                            client_time = await asyncio.to_thread(self._get_mpv_property_sync, "time-pos")
                            
                            # Only seek if we're out of sync by more than 0.8 seconds (tighter sync)
                            # Larger thresholds (e.g. 2.0s) lead to noticeable desynchronization
                            if client_time is None or abs(client_time - timestamp) > 0.8:
                                await self._send_mpv_command(["set_property", "time-pos", timestamp])
                            
                    elif mt == "kicked":
                        self.chat_history.append(f"[bold red]You have been kicked from the party.[/bold red]")
                        self.running = False
                        
                    elif mt == "error":
                        self.chat_history.append(
                            f"[bold red]Error:[/bold red] {escape(data.get('message', ''))}"
                        )
                        
                    if len(self.chat_history) > 10000:
                        self.chat_history = self.chat_history[-10000:]
                        
                except Exception as e:
                    pass
                    
        except websockets.exceptions.InvalidStatus as e:
            if e.response.status_code == 404:
                self.chat_history.append("[bold red]Error: Connection Refused (404)[/bold red]")
                self.chat_history.append("[yellow]The Party Link might be expired or invalid.[/yellow]")
                self.chat_history.append("[dim]The Host may have restarted their terminal/ngrok.[/dim]")
            else:
                self.chat_history.append(f"[bold red]Connection rejected (HTTP {e.response.status_code}).[/bold red]")
            
            self.chat_history.append(f"[dim]This window will close in 5 seconds...[/dim]")
            self.running = False
            await asyncio.sleep(5)
        except websockets.exceptions.ConnectionClosed as e:
            self.chat_history.append(f"[bold red]Connection lost: {e.reason if e.reason else 'Host ended the session'}[/bold red]")
            self.chat_history.append(f"[bold red]This terminal will close automatically in 3 seconds...[/bold red]")
            self.running = False
        except ConnectionRefusedError:
            self.chat_history.append("[bold red]Connection refused. The party server may not be running.[/bold red]")
            self.running = False
        except Exception as e:
            logger.error(f"Failed to connect or system error: {e}", exc_info=True)
            self.chat_history.append(f"[bold red]Failed to connect: {type(e).__name__}: {e}[/bold red]")
            self.running = False
        finally:
            self.running = False
            if self.voice_manager:
                self.voice_manager.stop()

    def _voice_packet_callback(self, data):
        if self.ws and self.ws.state == State.OPEN:
            asyncio.create_task(self.ws.send(data))

    def _launch_mpv(self, url, title, timestamp):
        from shared.media import get_mpv_path, get_streaming_headers
        mpv_path = get_mpv_path()
        if not mpv_path:
            self.chat_history.append("[bold red]mpv not found! Cannot sync video.[/bold red]")
            return False

        # On Unix, stale IPC socket files can prevent mpv from starting.
        if not IS_WINDOWS and os.path.exists(self.mpv_ipc_path):
            try:
                os.remove(self.mpv_ipc_path)
            except Exception as e:
                logger.warning(f"[LIFECYCLE] Could not remove stale mpv IPC socket: {e}")

        args = [
            mpv_path,
            f"--title={title} (Watch Party Sync)",
            f"--start={timestamp}",
            f"--input-ipc-server={self.mpv_ipc_path}",
            "--fs",
        ]
        
        # Add shared headers and optimizations
        args.extend(get_streaming_headers(url))
        
        # Additional buffer optimization for shared playback
        args.extend([
            "--demuxer-max-bytes=100MiB",
            "--demuxer-readahead-secs=20",
        ])
        
        args.append(url)
        
        try:
            self.mpv_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"[LIFECYCLE] MPV process launched (PID: {self.mpv_process.pid}) for URL: {url}")
            if self.mpv_process.poll() is not None:
                self.chat_history.append("[bold red]Player exited immediately. Retrying on next sync...[/bold red]")
                self.mpv_process = None
                return False
            return True
        except Exception as e:
            logger.error(f"[LIFECYCLE] Failed to launch MPV in client: {e}", exc_info=True)
            self.chat_history.append(f"[bold red]Failed to launch player:[/bold red] {e}")
            self.mpv_process = None
            return False
        
    async def broadcast_voice_state(self):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "voice_state",
                "muted": self.mic_muted,
                "deafened": self.speaker_muted
            }))

    def _handle_local_command(self, cmd):
        """Handle client-side /commands for local mute/deafen."""
        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        target = parts[1].strip() if len(parts) > 1 else ""
        
        if action in ["/mute", "/unmute"]:
            if not target:
                self.chat_history.append(f"[yellow]Usage: {action} <Name/ID>[/yellow]")
                return
            
            # Resolve target to Hash-ID
            target_hash = None
            target_name = None
            for u in self.users:
                if u.get('name').lower() == target.lower() or u.get('hash_id') == target:
                    target_hash = u.get('hash_id')
                    target_name = u.get('name')
                    break
            
            if not target_hash:
                self.chat_history.append(f"[red]Could not find user: {target}[/red]")
                return
                
            if action == "/mute":
                self.local_muted.add(target_hash)
                self.chat_history.append(f"[yellow]Locally muted {target_name} (#{target_hash}). Audio and chat are now ignored.[/yellow]")
            else:
                if target_hash in self.local_muted:
                    self.local_muted.remove(target_hash)
                self.chat_history.append(f"[green]Locally unmuted {target_name}. Audio and chat restored.[/green]")
                
        elif action in ["/deafen", "/undeafen"]:
            if not target:
                self.chat_history.append(f"[yellow]Usage: {action} <Name/ID>[/yellow]")
                return
            
            # Resolve target to Hash-ID
            target_hash = None
            target_name = None
            for u in self.users:
                if u.get('name').lower() == target.lower() or u.get('hash_id') == target:
                    target_hash = u.get('hash_id')
                    target_name = u.get('name')
                    break
            
            if not target_hash:
                self.chat_history.append(f"[red]Could not find user: {target}[/red]")
                return
                
            if action == "/deafen":
                self.local_deafened.add(target_hash)
                self.chat_history.append(f"[yellow]Locally deafened {target_name} (#{target_hash}). You won't hear them anymore.[/yellow]")
            else:
                if target_hash in self.local_deafened:
                    self.local_deafened.remove(target_hash)
                self.chat_history.append(f"[green]Locally undeafened {target_name}. Audio restored.[/green]")
                
        elif action == "/join":
            if getattr(self, 'current_video_url', None) and getattr(self, 'mpv_process', None) is None:
                self.chat_history.append("[dim italic]Re-joining the video stream...[/dim italic]")
                # Timestamp 0 will be fixed instantly on the next sync tick from the host
                self._launch_mpv(self.current_video_url, "Party Video", 0)
            else:
                self.chat_history.append("[dim italic]No active stream to join, or you are already watching.[/dim italic]")
                
        elif action == "/users":
            if self.users:
                user_list = ", ".join([f"{u['name']}({'👑' if u.get('role')=='host' else '👤'})" for u in self.users])
                self.chat_history.append(f"[cyan]Online: {user_list}[/cyan]")
            else:
                self.chat_history.append("[dim]No user list yet.[/dim]")
        
        elif action == "/help":
            self.chat_history.append("[dim]── Local Controls ──[/dim]")
            self.chat_history.append("[dim] /mute [Name/ID]   /unmute [Name/ID][/dim]")
            self.chat_history.append("[dim] /deafen [Name/ID] /undeafen [Name/ID][/dim]")
            self.chat_history.append("[dim] Ctrl+K: Mic Toggle       Ctrl+T: Deafen Toggle[/dim]")
            self.chat_history.append("[dim] Ctrl+N: Sounds Toggle    PgUp/Dn: Scroll Dim[/dim]")
            self.chat_history.append("[dim] /users: List Online      /close: Exit Application[/dim]")
        elif action == "/notification":
            if target.lower().startswith("sounds"):
                # Handle "/notification sounds on" or "/notification sounds off"
                sub_parts = target.split(" ")
                val = sub_parts[1].lower() if len(sub_parts) > 1 else ("off" if self.notifications_enabled else "on")
                
                self.notifications_enabled = (val == "on")
                # Persist to config
                from core.config import update_client_config
                update_client_config(notifications=self.notifications_enabled)
                
                status = "enabled" if self.notifications_enabled else "disabled"
                self.chat_history.append(f"[yellow]All notification sounds are now {status}.[/yellow]")
            else:
                self.chat_history.append("[yellow]Usage: /notification sounds <on|off>[/yellow]")
        elif action in ["/exit", "/close"]:
            self.running = False
        else:
            self.chat_history.append(f"[red]Unknown command: {action}. Type /help[/red]")

    def _render_chat_feed(self, max_lines):
        chat_rows = []
        
        # Calculate view window
        total = len(self.chat_history)
        end = total - self.scroll_offset
        start = max(0, end - max_lines)
        end = max(0, end)
        
        for entry in self.chat_history[start:end]:
            if entry.startswith(CHAT_TOKEN):
                try:
                    payload = json.loads(entry[len(CHAT_TOKEN):])
                    sender = payload.get("sender", "Unknown")
                    message = payload.get("message", "")
                    ts = payload.get("time", "")
                    color = "magenta" if sender == self.username else "cyan"
                    chat_rows.append(Text.from_markup(f"[dim]{ts}[/dim] [bold {color}]{escape(sender)}[/bold {color}] » {escape(message)}"))
                except Exception:
                    chat_rows.append(Text.from_markup(entry))
            else:
                chat_rows.append(Text.from_markup(entry))
        return Group(*chat_rows) if chat_rows else Text("")

    def generate_layout(self):
        from rich import box
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="input", size=3)
        )
        layout["main"].split_row(
            Layout(name="sidebar", size=25),
            Layout(name="chat")
        )
        
        # Header with dynamic status icons
        mic_icon = "🎙️" if not self.mic_muted else "🔇"
        speaker_icon = "🔊" if not self.speaker_muted else "🔇"
        notif_icon = "🔔" if self.notifications_enabled else "🔕"
        
        status_bar = f"[bold] {mic_icon} | {speaker_icon} | {notif_icon} [/bold]"
        header_text = f"[bold magenta]{self.room_name}[/bold magenta] [dim]•[/dim] 👤 [bold cyan]{self.username}[/bold cyan] [dim]|[/dim] {status_bar}"
        layout["header"].update(Panel(Align.center(header_text), box=box.ROUNDED, title="[dim]Streamix Client[/dim]", border_style="blue"))
        
        # Users panel
        from rich.table import Table
        user_table = Table(show_header=True, expand=True, box=None)
        user_table.add_column("", width=2)
        user_table.add_column("Name")
        user_table.add_column("🎙️", width=3, justify="center")
        user_table.add_column("🔊", width=3, justify="center")
        for u in self.users:
            name = u.get('name', '?')
            is_host = u.get('role') == 'host'
            icon = "👑" if is_host else ("🟢" if u.get('online') else "🔴")
            
            # Voice / Audio Icons (Discord style)
            mic_part = ""
            def_part = ""
            
            # Mic logic
            is_speaking = (time.time() - u.get('last_spoke', 0)) < 0.8
            if u.get('muted'): # Global
                mic_part = "[red]🔇[/red]"
            elif u.get('hash_id') in self.local_muted: # Local
                mic_part = "[dim]🔇[/dim]"
            elif is_speaking:
                mic_part = "[bold green]🎙️[/bold green]"
            else:
                mic_part = "[dim]🎙️[/dim]"
                
            # Deafen logic
            if u.get('deafened'): # Global
                def_part = "[red]🔕[/red]"
            elif u.get('hash_id') in self.local_deafened: # Local
                def_part = "[dim]🔕[/dim]"
            else:
                def_part = "[dim]🔊[/dim]"
            
            # Split into columns
            display_name = f"{name}\n[dim]#{u.get('hash_id', '????')}[/dim]"
            user_table.add_row(icon, display_name, mic_part, def_part)
            
        user_panel = Panel(user_table, title="👥 Participants", border_style="blue")
        
        # Audio Meter
        meter_content = "[dim]Mic Muted[/dim]"
        if not self.mic_muted:
            vol = getattr(self.voice_manager, 'current_volume', 0.0)
            bar_len = int(vol * 20)
            bar = "■" * bar_len + " " * (20 - bar_len)
            color = "green" if vol < 0.6 else "yellow"
            meter_content = f"[{color}]{bar}[/{color}]"
            
        audio_panel = Panel(Align.center(meter_content), title="🎙️ Your Mic", border_style="magenta")
        
        sidebar = Layout()
        sidebar.split(
            Layout(user_panel, ratio=4),
            Layout(audio_panel, size=3)
        )
        
        layout["sidebar"].update(sidebar)
        
        # Chat panel — align to bottom so new messages appear at bottom
        import shutil
        max_lines = max(5, shutil.get_terminal_size().lines - 10)
        chat_content = self._render_chat_feed(max_lines)
        
        chat_title = "Chat"
        if self.scroll_offset > 0:
            chat_title += f" [yellow](Scrolled: {self.scroll_offset})[/yellow]"
            
        layout["chat"].update(Panel(Align(chat_content, vertical="bottom"), title=chat_title, border_style="blue"))
        
        input_panel = Panel(
            f"> {self.input_text}█",
            border_style="green",
            title="Message (/help for commands)",
        )
        layout["input"].update(input_panel)
        
        return layout

    async def input_loop(self, input_handler):
        while self.running:
            try:
                c = input_handler.get_char()
                if c:
                    if c == '\x08': # backspace
                        self.input_text = self.input_text[:-1]
                    elif c == '\x1b': # esc
                        self.running = False
                    elif c in ('\r', '\n'): # enter
                        if self.input_text:
                            if self.input_text.startswith('/'):
                                self._handle_local_command(self.input_text)
                            elif self.ws:
                                await self.ws.send(
                                    json.dumps(
                                        {"type": "chat", "message": self.input_text},
                                        ensure_ascii=False,
                                    )
                                )
                            self.input_text = ""
                    elif c in ['PAGEUP', '\x17']: # PAGEUP or Ctrl+W
                        self.scroll_offset += 5
                        self.scroll_offset = min(self.scroll_offset, max(0, len(self.chat_history) - 5))
                    elif c in ['PAGEDOWN', '\x13']: # PAGEDOWN or Ctrl+S
                        self.scroll_offset = max(0, self.scroll_offset - 5)
                    elif c == '\x0b': # Ctrl+K
                        if self.admin_muted:
                            self.chat_history.append("[bold red]Cannot unmute: You are globally muted by the Host.[/bold red]")
                        else:
                            self.mic_muted = not self.mic_muted
                            if self.voice_manager: self.voice_manager.mic_muted = self.mic_muted
                            self.chat_history.append(f"[dim]🎙️ Mic {'On' if not self.mic_muted else 'Muted'}[/dim]")
                            asyncio.create_task(self.broadcast_voice_state())
                    elif c == '\x14': # Ctrl+T
                        if self.admin_deafened:
                            self.chat_history.append("[bold red]Cannot undeafen: You are globally deafened by the Host.[/bold red]")
                        else:
                            self.speaker_muted = not self.speaker_muted
                            if self.voice_manager: self.voice_manager.speaker_muted = self.speaker_muted
                            self.chat_history.append(f"[dim]🔊 Voice {'On' if not self.speaker_muted else 'Deafened'}[/dim]")
                            asyncio.create_task(self.broadcast_voice_state())
                    elif c == '\x0e': # Ctrl+N
                        self.notifications_enabled = not self.notifications_enabled
                        from core.config import update_client_config
                        update_client_config(notifications=self.notifications_enabled)
                        self.chat_history.append(f"[dim]🔔 Sounds {'On' if self.notifications_enabled else 'Off'}[/dim]")
                    else:
                        if c.isprintable():
                            self.input_text += c
                            # Auto-reset scroll on activity if at bottom
                            if self.scroll_offset < 2: self.scroll_offset = 0
            except Exception as e:
                pass
            await asyncio.sleep(0.01)

    async def run(self):
        from rich import box
        from features.watch_party.party_input import NonBlockingInput
        
        input_handler = NonBlockingInput()
        try:
            asyncio.create_task(self.connect_and_listen())
            asyncio.create_task(self.input_loop(input_handler))
            
            # Render loop — always refresh for instant chat updates
            with Live(self.generate_layout(), refresh_per_second=15) as live:
                while self.running:
                    live.update(self.generate_layout())
                    await asyncio.sleep(0.05)
                    if self.mpv_process and self.mpv_process.poll() is not None:
                        self.mpv_process = None
                        self.chat_history.append("[dim]Video player closed.[/dim]")
            
                # Final countdown loop before closing
                for i in range(5, 0, -1):
                    self.chat_history.append(f"[bold yellow]⚠️ Session ended. Terminal closing in {i}...[/bold yellow]")
                    live.update(self.generate_layout())
                    await asyncio.sleep(1)
            
        finally:
            self.running = False
            if self.voice_manager:
                self.voice_manager.stop()
            if self.mpv_process:
                try: self.mpv_process.terminate()
                except: pass
            
            # Explicit exit to close the terminal window
            import sys
            sys.exit(0)

if __name__ == "__main__":
    import sys
    import signal
    import traceback

    def _install_shutdown_handlers():
        def _handle_term(signum, frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _handle_term)
        signal.signal(signal.SIGINT, _handle_term)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _handle_term)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handle_term)

    _install_shutdown_handlers()
    
    try:
        url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:9000"
        username = sys.argv[2] if len(sys.argv) > 2 else f"Guest_{int(time.time())%1000}"
        
        client = PartyClient(ws_url=url, username=username)
        asyncio.run(client.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        print(f"\n[bold red]FATAL ERROR:[/bold red] {e}")
        traceback.print_exc()
        input("\nPress Enter to exit...")
