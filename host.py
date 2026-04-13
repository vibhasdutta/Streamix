import asyncio
import json
import websockets
from websockets.protocol import State
import time
import os
import subprocess
import threading
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich.align import Align
from rich.markup import escape
from rich.console import Group
from utils.os_detector import IS_WINDOWS
from voice_manager import VoiceManager
from utils.logger import setup_logger

# Initialize host session logger
logger = setup_logger("host_tui", "host_session.log")

console = Console()
CHAT_TOKEN = "__CHAT__"

class PartyAdminTUI:
    def __init__(self, username="Host", ipc_path=None, ws_url="ws://localhost:9000"):
        self.username = username
        self.ipc_path = ipc_path
        self.ws_url = ws_url
        self.ws = None
        self.running = True
        
        self.users = []
        self.chat_history = []
        self.room_url = "Starting..."
        self.room_name = "Watch Party Admin"
        self.input_text = ""
        self.system_messages = []
        self.scroll_offset = 0 # Track how many lines we are scrolled up
        
        # Local-only filters (admin's own view, doesn't affect others)
        self.local_muted = set()
        self.local_deafened = set()
        
        # Load persistent config
        from config import get_admin_config
        self.config = get_admin_config()
        self.volume = self.config.get("volume", 100)
        self.notifications_enabled = self.config.get("notifications", True)
        self.mic_muted = True 
        self.speaker_muted = False
        
        # Audio restrictions from server (Admin forced)
        self.admin_muted = False
        self.admin_deafened = False
        
        self.voice_manager = None
        self.chat_limit = self.config.get("chat_history_limit", 50)
        self._mpv_path = None # Cache for mpv path
        self._last_sound_time = 0 # Cooldown for sounds
        
        # Try to load info from file
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "party_info.json"), "r") as f:
                info = json.load(f)
                self.room_url = info.get("url", "ws://localhost:9000")
                self.room_name = info.get("room_name", "Party")
        except:
            pass

    def _append_chat(self, sender, message, ts=None):
        if not ts: ts = time.strftime("%H:%M")
        payload = {"sender": sender, "message": message, "time": ts}
        self.chat_history.append(f"{CHAT_TOKEN}{json.dumps(payload, ensure_ascii=False)}")

    def _play_notification(self):
        if not self.notifications_enabled:
            return
        try:
            from main import get_mpv_path
            mpv_path = get_mpv_path()
            if mpv_path:
                sound_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sound_assests", "notification.mp3")
                import subprocess
                if os.path.exists(sound_path):
                    subprocess.Popen([mpv_path, "--no-video", "--no-terminal", sound_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    async def _send_mpv_command(self, command):
        """Sends an IPC command to the host's mpv via the pipe."""
        try:
            if not self.ipc_path:
                return
            import socket
            if IS_WINDOWS:
                with open(self.ipc_path, "w") as f:
                    f.write(json.dumps({"command": command}) + "\n")
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.ipc_path)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                s.close()
        except Exception:
            pass

    def _play_event_sound(self, filename):
        if not self.notifications_enabled:
            return
            
        # 2 second cooldown for sounds to prevent spam
        now = time.time()
        if now - self._last_sound_time < 2.0:
            return
        self._last_sound_time = now
        
        try:
            if not self._mpv_path:
                from main import get_mpv_path
                self._mpv_path = get_mpv_path()
            
            if self._mpv_path:
                sound_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sound_assets", filename)
                if os.path.exists(sound_path):
                    # Use Popen to play in background without blocking
                    subprocess.Popen([self._mpv_path, "--no-video", "--no-terminal", sound_path], 
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass

    async def connect_and_listen(self):
        try:
            # Connect to local server
            max_retries = 10
            retry_delay = 1.0
            connect_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 10,
            }
            
            for attempt in range(1, max_retries + 1):
                try:
                    # DEEP SANITIZATION: Strip ALL non-standard URL characters
                    import re
                    original_url = self.ws_url
                    self.ws_url = re.sub(r"[^a-zA-Z0-9\.\-\:\/\?\=\&\_]", "", self.ws_url)
                    
                    if original_url != self.ws_url:
                        logger.warning(f"[NETWORK] Sanitized Host URL from '{original_url}' to '{self.ws_url}'")
                    
                    target_display = self.ws_url
                    if len(target_display) > 40:
                        target_display = target_display[:37] + "..."
                        
                    self.chat_history.append(f"[dim]Connecting to {target_display} (attempt {attempt}/{max_retries})...[/dim]")
                    self.ws = await websockets.connect(self.ws_url, **connect_kwargs)
                    logger.info(f"[LIFECYCLE] Connected to party server at {self.ws_url}")
                    break
                except (ConnectionRefusedError, OSError) as e:
                    import socket
                    if isinstance(e, socket.gaierror) or "getaddrinfo" in str(e):
                        # DNS Failure - Log Hex for deep debugging
                        url_hex = self.ws_url.encode('utf-8').hex()
                        logger.error(f"[NETWORK] DNS Resolution failed (Host): {self.ws_url} (Hex: {url_hex})")
                        error_hint = "Check Local URL typos"
                    else:
                        error_hint = str(e)
                        
                    if attempt < max_retries:
                        self.chat_history.append(f"[dim]Could not reach server, retrying in {retry_delay:.0f}s... ([red]{error_hint}[/red])[/dim]")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 1.5, 10.0)
                    else:
                        raise e
            
            # Join as host
            await self.ws.send(json.dumps({"type": "join", "name": self.username, "role": "host"}))

            # Start Voice Manager with persistent config
            from config import load_config
            cfg = load_config()["admin"]
            mic_idx = cfg.get("mic_device_index")
            
            loop = asyncio.get_event_loop()
            self.voice_manager = VoiceManager(loop, input_device=mic_idx)
            self.voice_manager.on_voice_packet = self._voice_packet_callback
            self.voice_manager.mic_muted = self.mic_muted
            self.voice_manager.speaker_muted = self.speaker_muted
            self.voice_manager.start()
            logger.info(f"[LIFECYCLE] Voice Manager started (Input Index: {mic_idx})")
            
            self._append_chat("System", f"Joined room as host. Server: {self.room_url}")
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
                    
                    if mt == "user_list":
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
                                    # NOTE: We DON'T automatically turn the mic back ON (Safety First). 
                                    # User must manually press Ctrl+K to re-enable.
                                
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
                        # Local filter: skip if admin locally muted/deafened this user
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
                        self.system_messages.append(msg)
                        
                        if subtype == "join" and data.get("actor") != self.username:
                            self._play_event_sound("joinin.mp3")
                        elif subtype == "leave" and data.get("actor") != self.username:
                            self._play_event_sound("leave.mp3")
                    elif mt == "error":
                        msg = data.get("message", "")
                        self.chat_history.append(
                            f"[bold red]System Error:[/bold red] {escape(msg)}"
                        )
                        
                    # Keep history manageable
                    if len(self.chat_history) > 10000:
                        self.chat_history = self.chat_history[-10000:]
                        
                except Exception as e:
                    self.chat_history.append(f"[red]Error parsing message: {e}[/red]")
                    
        except websockets.exceptions.ConnectionClosed:
            self.chat_history.append("[bold red]Connection lost. Session ended.[/bold red]")
            self.chat_history.append("[bold yellow]This window will close automatically in 3 seconds...[/bold yellow]")
            self.running = False
        except Exception as e:
            logger.error(f"System disconnected or error in message loop: {e}", exc_info=True)
            self.chat_history.append(f"[bold red]System disconnected: {e}[/bold red]")
            self.running = False
        finally:
            if self.voice_manager:
                self.voice_manager.stop()

    def _voice_packet_callback(self, data):
        if self.ws and self.ws.state == State.OPEN:
            asyncio.create_task(self.ws.send(data))

    def handle_command(self, cmd):
        if not self.ws:
            return
            
        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""
        
        asyncio.create_task(self.dispatch_cmd(action, target))
        
    async def dispatch_cmd(self, action, target):
        # Global admin commands (affect everyone)
        if action in ["/kick", "/mute", "/unmute", "/deafen", "/undeafen", "/undefan", "/ban", "/unban"]:
            if not target:
                self.chat_history.append(f"[yellow]Usage: {action} <username>[/yellow]")
                return
            await self.ws.send(json.dumps({
                "type": "admin",
                "action": action[1:],  # Remove slash
                "target": target
            }, ensure_ascii=False))
        # Local Moderation Commands (Personal Only)
        elif action in ["/lmute", "/localmute", "/lunmute", "/localunmute"]:
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
                
            if action in ["/lmute", "/localmute"]:
                self.local_muted.add(target_hash)
                self.chat_history.append(f"[yellow]Locally muted {target_name} (#{target_hash}). You won't hear them or see their chat.[/yellow]")
            elif action in ["/ldeafen", "/localdeafen"]:
                self.local_deafened.add(target_hash)
                self.chat_history.append(f"[yellow]Locally deafened {target_name} (#{target_hash}). You won't hear them anymore.[/yellow]")
            elif action in ["/lunmute", "/localunmute"]:
                if target_hash in self.local_muted:
                    self.local_muted.remove(target_hash)
                self.chat_history.append(f"[green]Locally unmuted {target_name}. Audio and chat restored.[/green]")
            elif action in ["/lundeafen", "/localundeafen"]:
                if target_hash in self.local_deafened:
                    self.local_deafened.remove(target_hash)
                self.chat_history.append(f"[green]Locally undeafened {target_name}. Audio restored.[/green]")
        # Local volume control
        elif action == "/help":
            self.chat_history.append("[dim]── Global Controls ──[/dim]")
            self.chat_history.append("[dim] /kick [Name/ID]   /mute [Name/ID]   /unmute [Name/ID][/dim]")
            self.chat_history.append("[dim] /ban [Name/ID]    /deafen [Name/ID] /undeafen [Name/ID][/dim]")
            self.chat_history.append("[dim] /unban [Name/ID] [/dim]")
            self.chat_history.append("[dim]── Local Controls (Personal) ──[/dim]")
            self.chat_history.append("[dim] /lmute [Name/ID]  /lunmute [Name/ID][/dim]")
            self.chat_history.append("[dim] Ctrl+K: Mic Toggle       Ctrl+T: Deafen Toggle[/dim]")
            self.chat_history.append("[dim] Ctrl+N: Sounds Toggle    PgUp/Dn: Scroll Dim[/dim]")
            self.chat_history.append("[dim] /close: Exit Application[/dim]")
        elif action in ["/exit", "/close"]:
            self.running = False
        elif action == "/notification":
            if target.lower().startswith("sounds"):
                # Handle "/notification sounds on" or "/notification sounds off"
                sub_parts = target.split(" ")
                val = sub_parts[1].lower() if len(sub_parts) > 1 else ("off" if self.notifications_enabled else "on")
                
                self.notifications_enabled = (val == "on")
                # Persist to config
                from config import update_admin_config
                update_admin_config(notifications=self.notifications_enabled)
                
                status = "enabled" if self.notifications_enabled else "disabled"
                self.chat_history.append(f"[yellow]All notification sounds are now {status}.[/yellow]")
            else:
                self.chat_history.append("[yellow]Usage: /notification sounds <on|off>[/yellow]")
        else:
            self.chat_history.append(f"[yellow]Unknown command: {action}. Type /help[/yellow]")

    async def send_chat(self, msg):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "chat",
                "message": msg
            }, ensure_ascii=False))

    async def broadcast_voice_state(self):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "voice_state",
                "muted": self.mic_muted,
                "deafened": self.speaker_muted
            }))

    def generate_layout(self):
        from rich import box
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="input", size=3)
        )
        layout["main"].split_row(
            Layout(name="sidebar", size=30),
            Layout(name="chat")
        )
        
        # Header with dynamic status icons
        mic_icon = "🎙️" if not self.mic_muted else "🔇"
        speaker_icon = "🔊" if not self.speaker_muted else "🔇"
        notif_icon = "🔔" if self.notifications_enabled else "🔕"
        
        status_bar = f"[bold] {mic_icon} | {speaker_icon} | {notif_icon} [/bold]"
        header_text = f"[bold magenta]{self.room_name}[/bold magenta] [dim]•[/dim] [bold yellow]Host[/bold yellow] [dim]|[/dim] {status_bar}"
        layout["header"].update(Panel(Align.center(header_text), box=box.ROUNDED, title="[dim]Streamix Admin[/dim]", border_style="magenta"))
        
        # Users
        user_table = Table(show_header=True, expand=True, box=None)
        user_table.add_column("", width=2)
        user_table.add_column("Name")
        user_table.add_column("🎙️", width=3, justify="center")
        user_table.add_column("🔊", width=3, justify="center")
        
        for u in self.users:
            name = u.get("name", "Unknown")
            is_host = u.get('role') == 'host'
            status_icon = "👑" if is_host else ("🟢" if u.get('online') else "🔴")
            
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
            user_table.add_row(status_icon, display_name, mic_part, def_part)
            
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
            Layout(user_panel, ratio=3),
            Layout(audio_panel, size=3)
        )
        
        layout["sidebar"].update(sidebar)
        
        # Chat — align to bottom so new messages appear at bottom
        import shutil
        max_lines = max(5, shutil.get_terminal_size().lines - 10)
        chat_content = self._render_chat_feed(max_lines)
        
        chat_title = "Chat & Activity"
        if self.scroll_offset > 0:
            chat_title += f" [yellow](Scrolled: {self.scroll_offset})[/yellow]"
            
        layout["chat"].update(Panel(Align(chat_content, vertical="bottom"), title=chat_title, border_style="blue"))
        
        # Input
        input_panel = Panel(
            f"> {self.input_text}█",
            border_style="green",
            title="Input (/help for commands)",
        )
        layout["input"].update(input_panel)
        
        return layout

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
                                if self.input_text.strip().lower() in ["/close", "/back", "/exit"]:
                                    self.running = False
                                else:
                                    self.handle_command(self.input_text)
                            else:
                                await self.send_chat(self.input_text)
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
                        from config import update_admin_config
                        update_admin_config(notifications=self.notifications_enabled)
                        self.chat_history.append(f"[dim]🔔 Sounds {'On' if self.notifications_enabled else 'Off'}[/dim]")
                    else:
                        if c.isprintable():
                            self.input_text += c
                            # Auto-reset scroll on activity if at bottom
                            if self.scroll_offset < 2: self.scroll_offset = 0
            except Exception as e:
                pass
            await asyncio.sleep(0.01)

    async def _mpv_poller(self):
        """Polls local mpv IPC for playback state to sync to server"""
        if not self.ipc_path:
            return
            
        import socket
        
        while self.running and not self.ws:
            await asyncio.sleep(1)
            
        # Give mpv time to start
        await asyncio.sleep(2)
        
        while self.running:
            try:
                pause_state = False
                time_pos = 0.0
                current_url = None
                current_title = None
                mpv_alive = False
                
                # IPC get property is synchronous for simplicity here
                def query_mpv():
                    import json
                    nonlocal pause_state, time_pos, current_url, current_title, mpv_alive
                    try:
                        if IS_WINDOWS:
                            # Read win32 pipe
                            with open(self.ipc_path, 'r+') as f:
                                # Send multiple property requests and read them back
                                f.write(json.dumps({"command": ["get_property", "pause"]}) + "\n")
                                f.write(json.dumps({"command": ["get_property", "time-pos"]}) + "\n")
                                f.write(json.dumps({"command": ["get_property", "path"]}) + "\n")
                                f.write(json.dumps({"command": ["get_property", "media-title"]}) + "\n")
                                f.flush()
                                pause_state = json.loads(f.readline()).get("data", False)
                                time_pos = json.loads(f.readline()).get("data", 0.0)
                                current_url = json.loads(f.readline()).get("data")
                                current_title = json.loads(f.readline()).get("data")
                        else:
                            # Consolidate Unix IPC property requests
                            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            s.settimeout(0.2)
                            s.connect(self.ipc_path)
                            
                            commands = [
                                ["get_property", "pause"],
                                ["get_property", "time-pos"],
                                ["get_property", "path"],
                                ["get_property", "media-title"]
                            ]
                            
                            for cmd in commands:
                                s.sendall((json.dumps({"command": cmd}) + "\n").encode())
                            
                            # Read responses until we have enough newlines
                            raw_data = b""
                            while raw_data.count(b'\n') < len(commands):
                                chunk = s.recv(4096)
                                if not chunk: break
                                raw_data += chunk
                                    
                            responses = raw_data.decode().split('\n')
                            pause_state = json.loads(responses[0]).get("data", False)
                            time_pos = json.loads(responses[1]).get("data", 0.0)
                            current_url = json.loads(responses[2]).get("data")
                            current_title = json.loads(responses[3]).get("data")
                            s.close()
                        mpv_alive = True
                    except Exception:
                        mpv_alive = False
                
                # Run query in thread to not block async loops
                await asyncio.to_thread(query_mpv)
                
                # Only send sync when MPV is actually running
                # When MPV is closed (host picking new episode), send paused state to freeze clients
                if self.ws:
                    if mpv_alive and current_url:
                        payload = {
                            "type": "sync",
                            "state": "paused" if pause_state else "playing",
                            "timestamp": time_pos
                        }
                        if current_url: payload["url"] = current_url
                        if current_title: payload["anime_title"] = current_title
                        await self.ws.send(json.dumps(payload, ensure_ascii=False))
                    elif not mpv_alive:
                        # MPV is closed — tell clients to close their players too
                        await self.ws.send(json.dumps({
                            "type": "sync",
                            "state": "closed",
                            "timestamp": 0
                        }))
                    
            except Exception:
                pass
            
            # Poll every 0.33 seconds (3Hz) for tighter sync
            await asyncio.sleep(0.33)

    async def run(self):
        from rich import box
        from party_input import NonBlockingInput
        
        input_handler = NonBlockingInput()
        try:
            # Start connection task
            asyncio.create_task(self.connect_and_listen())
            
            # Start input thread task
            asyncio.create_task(self.input_loop(input_handler))
            
            # Start mpv sync poller
            asyncio.create_task(self._mpv_poller())
            
            with Live(self.generate_layout(), refresh_per_second=15) as live:
                while self.running:
                    live.update(self.generate_layout())
                    await asyncio.sleep(0.05)
                
                # Final countdown loop before closing
                for i in range(5, 0, -1):
                    self.chat_history.append(f"[bold yellow]⚠️ Session ended. Terminal closing in {i}...[/bold yellow]")
                    live.update(self.generate_layout())
                    await asyncio.sleep(1)
        finally:
            self.running = False
            input_handler.cleanup()
            
            # Close WebSocket gracefully
            if self.ws:
                try: await self.ws.close()
                except: pass
            
            # Stop voice manager
            if self.voice_manager:
                self.voice_manager.stop()
            
            # Send quit command to Host MPV player
            if self.ipc_path:
                try:
                    import socket, json
                    if IS_WINDOWS:
                        with open(self.ipc_path, 'r+') as f:
                            f.write(json.dumps({"command": ["quit"]}) + "\n")
                            f.flush()
                    else:
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.connect(self.ipc_path)
                        s.sendall((json.dumps({"command": ["quit"]}) + "\n").encode())
                        s.close()
                except:
                    pass
            
            # Wait 3 seconds so user can see the end-of-session message
            await asyncio.sleep(3)
            
            # Cleanup finished
            return

if __name__ == "__main__":
    import sys
    import signal
    import traceback

    try:
        # Handle SIGTERM from pkill so script exits cleanly
        def _handle_term(signum, frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _handle_term)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handle_term)
        
        host = sys.argv[1] if len(sys.argv) > 1 else "Host"
        ipc_path = sys.argv[2] if len(sys.argv) > 2 else None
        ws_url = sys.argv[3] if len(sys.argv) > 3 else "ws://localhost:9000"
        
        tui = PartyAdminTUI(username=host, ipc_path=ipc_path, ws_url=ws_url)
        asyncio.run(tui.run())
        # Force exit to close terminal window when run standalone
        sys.exit(0)
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
    except Exception as e:
        print(f"\n[bold red]FATAL ERROR:[/bold red] {e}")
        traceback.print_exc()
        input("\nPress Enter to exit...")
