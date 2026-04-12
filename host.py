import asyncio
import json
import websockets
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

console = Console()
CHAT_TOKEN = "__CHAT__"

class PartyAdminTUI:
    def __init__(self, username="Host", ipc_path=None):
        self.username = username
        self.ipc_path = ipc_path
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
            
            for attempt in range(1, max_retries + 1):
                try:
                    self.chat_history.append(f"[dim]Connecting to local server (attempt {attempt}/{max_retries})...[/dim]")
                    self.ws = await websockets.connect(
                        "ws://127.0.0.1:9000",
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=10,
                    )
                    break
                except (ConnectionRefusedError, OSError) as e:
                    if attempt < max_retries:
                        self.chat_history.append(f"[dim]Server not ready, retrying in {retry_delay:.0f}s...[/dim]")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 1.5, 10.0)
                    else:
                        raise e
            
            # Join as host
            await self.ws.send(json.dumps({"type": "join", "name": self.username, "role": "host"}))
            
            # Message loop
            async for message_str in self.ws:
                if not self.running:
                    break
                
                try:
                    data = json.loads(message_str)
                    mt = data.get("type")
                    
                    if mt == "user_list":
                        self.users = data.get("users", [])
                    elif mt == "chat":
                        sender = data.get("sender", "Unknown")
                        msg = data.get("message", "")
                        # Local filter: skip if admin locally muted/deafened this user
                        if sender in self.local_muted or sender in self.local_deafened:
                            continue
                        self._append_chat(sender, msg)
                        if sender != self.username:
                            self._play_event_sound("notification.mp3")
                            
                    elif mt == "chat_history":
                        history = data.get("history", [])
                        for m in history:
                            self._append_chat(m.get("sender"), m.get("message"), ts=m.get("time"))
                            
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
                    if len(self.chat_history) > 100:
                        self.chat_history = self.chat_history[-100:]
                        
                except Exception as e:
                    self.chat_history.append(f"[red]Error parsing message: {e}[/red]")
                    
        except websockets.exceptions.ConnectionClosed:
            self.chat_history.append("[bold red]Connection to server closed.[/bold red]")
            self.running = False
        except Exception as e:
            self.chat_history.append(f"[bold red]Failed to connect to local server: {e}[/bold red]")
            self.running = False

    def handle_command(self, cmd):
        if not self.ws:
            return
            
        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""
        
        asyncio.create_task(self.dispatch_cmd(action, target))
        
    async def dispatch_cmd(self, action, target):
        # Global admin commands (affect everyone)
        if action in ["/kick", "/mute", "/deafen", "/ban", "/unban"]:
            if not target:
                self.chat_history.append(f"[yellow]Usage: {action} <username>[/yellow]")
                return
            await self.ws.send(json.dumps({
                "type": "admin",
                "action": action[1:],  # Remove slash
                "target": target
            }, ensure_ascii=False))
        # Local volume control
        elif action == "/help":
            self.chat_history.append("[bold]── Global (affects everyone) ──[/bold]")
            self.chat_history.append("[cyan]/kick <user>[/cyan] — Remove from room")
            self.chat_history.append("[cyan]/mute <user>[/cyan] — Server-wide mute")
            self.chat_history.append("[cyan]/deafen <user>[/cyan] — Server-wide deafen")
            self.chat_history.append("[cyan]/ban <user>[/cyan] — Ban from room")
            self.chat_history.append("[cyan]/unban <user>[/cyan] — Unban a user")
            self.chat_history.append("[bold]── Local (only for you) ──[/bold]")
            self.chat_history.append("[cyan]PgUp/PgDn[/cyan] — Scroll chat view")
            self.chat_history.append("[cyan]Ctrl+V[/cyan] — Toggle Microphone (Mute)")
            self.chat_history.append("[cyan]Ctrl+B[/cyan] — Toggle Voice (Deafen)")
            self.chat_history.append("[cyan]/notification sounds <on|off>[/cyan] — Toggle all audio alerts (Chat, Join/Leave)")
            self.chat_history.append("[cyan]/close[/cyan] — Close this window")
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

    def generate_layout(self):
        from rich import box
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="input", size=3)
        )
        layout["main"].split_row(
            Layout(name="users", size=45),
            Layout(name="chat")
        )
        
        # Header
        header_text = f"[bold magenta]{self.room_name}[/bold magenta] | 🔗 {self.room_url}"
        layout["header"].update(Panel(Align.center(header_text), box=box.ROUNDED))
        
        # Users
        user_table = Table(show_header=True, expand=True, box=None)
        user_table.add_column("")
        user_table.add_column("Name")
        user_table.add_column("ID", style="dim")
        
        for u in self.users:
            if u.get('role') == 'host':
                status = "👑"
            else:
                status = "🟢" if u.get('online') else "🔴"
                
            name = u.get("name", "Unknown")
            uid = u.get("hash_id", "")
            name_styled = name
            
            flags = []
            if u.get("muted"): flags.append("🔇G")  # Global mute
            if u.get("deafened"): flags.append("🔕G")  # Global deafen
            if name in self.local_muted: flags.append("🔇L")  # Local mute
            if name in self.local_deafened: flags.append("🔕L")  # Local deafen
            
            if flags:
                name_styled += f" [{' '.join(flags)}]"
                
            user_table.add_row(status, name_styled, uid)
            
        layout["users"].update(Panel(user_table, title="Users", border_style="cyan"))
        
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
                        # Clamp scroll
                        self.scroll_offset = min(self.scroll_offset, max(0, len(self.chat_history) - 5))
                    elif c in ['PAGEDOWN', '\x13']: # PAGEDOWN or Ctrl+S
                        self.scroll_offset = max(0, self.scroll_offset - 5)
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
            
            # Render loop — always refresh for instant chat updates
            with Live(self.generate_layout(), refresh_per_second=15) as live:
                while self.running:
                    live.update(self.generate_layout())
                    await asyncio.sleep(0.05)
        finally:
            input_handler.cleanup()
            # Close WebSocket gracefully
            if self.ws:
                try: await self.ws.close()
                except: pass
            
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
            # Show shutdown message briefly so user can read it
            if not self.running:
                console.print("\n[bold yellow]Admin panel closing in 3 seconds...[/bold yellow]")
                await asyncio.sleep(3)

if __name__ == "__main__":
    import sys
    import signal
    
    # Handle SIGTERM from pkill so script exits cleanly
    def _handle_term(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_term)
    
    host = sys.argv[1] if len(sys.argv) > 1 else "Host"
    ipc_path = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        tui = PartyAdminTUI(username=host, ipc_path=ipc_path)
        asyncio.run(tui.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        print(f"Error: {e}")
