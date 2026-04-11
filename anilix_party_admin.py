import asyncio
import json
import websockets
import time
import os
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
        
        # Local-only filters (admin's own view, doesn't affect others)
        self.local_muted = set()
        self.local_deafened = set()
        
        # Load persistent config
        from party_config import get_admin_config
        self.config = get_admin_config()
        self.volume = self.config.get("volume", 100)
        self.notifications_enabled = self.config.get("notifications", True)
        self.chat_limit = self.config.get("chat_history_limit", 50)
        
        # Try to load info from file
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".json", "party_info.json"), "r") as f:
                info = json.load(f)
                self.room_url = info.get("url", "ws://localhost:9000")
                self.room_name = info.get("room_name", "Party")
        except:
            pass

    def _append_chat(self, sender, message):
        payload = {"sender": sender, "message": message}
        self.chat_history.append(f"{CHAT_TOKEN}{json.dumps(payload, ensure_ascii=False)}")

    def _play_notification(self):
        if not self.notifications_enabled:
            return
        try:
            from anilix import get_mpv_path
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

    async def connect_and_listen(self):
        try:
            # Connect to local server
            self.ws = await websockets.connect("ws://127.0.0.1:9000")
            
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
                            self._play_notification()
                    elif mt == "system":
                        msg = data.get("message", "")
                        self.chat_history.append(f"[dim italic]{escape(msg)}[/dim italic]")
                        self.system_messages.append(msg)
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
        elif action == "/vol":
            try:
                level = int(target) if target else -1
                if 0 <= level <= 150:
                    await self._send_mpv_command(["set_property", "volume", level])
                    self.volume = level
                    from party_config import update_admin_config
                    update_admin_config(volume=level)
                    self.chat_history.append(f"[green]🔊 Volume set to {level}%[/green]")
                else:
                    self.chat_history.append("[yellow]Usage: /vol <0-150>[/yellow]")
            except ValueError:
                self.chat_history.append("[yellow]Usage: /vol <0-150>[/yellow]")
        # Toggle notifications
        elif action == "/notify":
            self.notifications_enabled = not self.notifications_enabled
            from party_config import update_admin_config
            update_admin_config(notifications=self.notifications_enabled)
            status = "enabled" if self.notifications_enabled else "disabled"
            self.chat_history.append(f"[green]🔔 Notifications {status}[/green]")
        # Local-only commands (only affect admin's own view)
        elif action == "/lmute":
            if not target:
                if self.local_muted:
                    self.chat_history.append(f"[yellow]Locally muted: {', '.join(self.local_muted)}[/yellow]")
                else:
                    self.chat_history.append("[yellow]No one locally muted. Usage: /lmute <user>[/yellow]")
                return
            if target in self.local_muted:
                self.local_muted.discard(target)
                self.chat_history.append(f"[green]Unmuted {target} (local only).[/green]")
            else:
                self.local_muted.add(target)
                self.chat_history.append(f"[yellow]Muted {target} locally. Their messages are hidden for you only.[/yellow]")
        elif action == "/ldeafen":
            if not target:
                if self.local_deafened:
                    self.chat_history.append(f"[yellow]Locally deafened: {', '.join(self.local_deafened)}[/yellow]")
                else:
                    self.chat_history.append("[yellow]No one locally deafened. Usage: /ldeafen <user>[/yellow]")
                return
            if target in self.local_deafened:
                self.local_deafened.discard(target)
                self.chat_history.append(f"[green]Undeafened {target} (local only).[/green]")
            else:
                self.local_deafened.add(target)
                self.chat_history.append(f"[yellow]Deafened {target} locally. Hidden for you only.[/yellow]")
        elif action == "/help":
            self.chat_history.append("[bold]── Global (affects everyone) ──[/bold]")
            self.chat_history.append("[cyan]/kick <user>[/cyan] — Remove from room")
            self.chat_history.append("[cyan]/mute <user>[/cyan] — Server-wide mute (can't send messages)")
            self.chat_history.append("[cyan]/deafen <user>[/cyan] — Server-wide deafen")
            self.chat_history.append("[cyan]/ban <user>[/cyan] — Ban from room")
            self.chat_history.append("[cyan]/unban <user>[/cyan] — Unban a user")
            self.chat_history.append("[bold]── Local (only for you) ──[/bold]")
            self.chat_history.append("[cyan]/vol <0-150>[/cyan] — Set your video volume")
            self.chat_history.append("[cyan]/notify[/cyan] — Toggle notification sounds")
            self.chat_history.append("[cyan]/lmute <user>[/cyan] — Hide their messages for you")
            self.chat_history.append("[cyan]/ldeafen <user>[/cyan] — Hide their activity for you")
            self.chat_history.append("[cyan]/close[/cyan] — Close this window")
        elif action in ["/exit", "/close"]:
            self.running = False
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
        user_table.add_column("IP", style="dim")
        
        for u in self.users:
            if u.get('role') == 'host':
                status = "👑"
            else:
                status = "🟢" if u.get('online') else "🔴"
                
            name = u.get("name", "Unknown")
            ip = u.get("ip", "")
            name_styled = name
            
            flags = []
            if u.get("muted"): flags.append("🔇G")  # Global mute
            if u.get("deafened"): flags.append("🔕G")  # Global deafen
            if name in self.local_muted: flags.append("🔇L")  # Local mute
            if name in self.local_deafened: flags.append("🔕L")  # Local deafen
            
            if flags:
                name_styled += f" [{' '.join(flags)}]"
                
            user_table.add_row(status, name_styled, ip)
            
        layout["users"].update(Panel(user_table, title="Users", border_style="cyan"))
        
        # Chat — align to bottom so new messages appear at bottom
        import shutil
        max_lines = max(5, shutil.get_terminal_size().lines - 10)
        chat_content = self._render_chat_feed(max_lines)
        layout["chat"].update(Panel(Align(chat_content, vertical="bottom"), title="Chat & Activity", border_style="blue"))
        
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
        for entry in self.chat_history[-max_lines:]:
            if entry.startswith(CHAT_TOKEN):
                try:
                    payload = json.loads(entry[len(CHAT_TOKEN):])
                except Exception:
                    chat_rows.append(Text.from_markup(entry))
                    continue

                sender = payload.get("sender", "Unknown")
                message = payload.get("message", "")
                color = "magenta" if sender == self.username else "cyan"
                chat_rows.append(Text.from_markup(f"[bold {color}]{escape(sender)}[/bold {color}] :: {escape(message)}"))
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
                    else:
                        if c.isprintable():
                            self.input_text += c
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
                                f.write(json.dumps({"command": ["get_property", "pause"]}) + "\n")
                                f.flush()
                                res = json.loads(f.readline())
                                pause_state = res.get("data", False)
                                
                                f.write(json.dumps({"command": ["get_property", "time-pos"]}) + "\n")
                                f.flush()
                                res = json.loads(f.readline())
                                time_pos = res.get("data", 0.0)
                                
                                f.write(json.dumps({"command": ["get_property", "path"]}) + "\n")
                                f.flush()
                                res = json.loads(f.readline())
                                current_url = res.get("data")
                                
                                f.write(json.dumps({"command": ["get_property", "media-title"]}) + "\n")
                                f.flush()
                                res = json.loads(f.readline())
                                current_title = res.get("data")
                        else:
                            # Unix socket
                            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            s.connect(self.ipc_path)
                            s.sendall((json.dumps({"command": ["get_property", "pause"]}) + "\n").encode())
                            res = json.loads(s.recv(1024).decode().split('\n')[0])
                            pause_state = res.get("data", False)
                            
                            s.sendall((json.dumps({"command": ["get_property", "time-pos"]}) + "\n").encode())
                            res = json.loads(s.recv(1024).decode().split('\n')[0])
                            time_pos = res.get("data", 0.0)
                            
                            s.sendall((json.dumps({"command": ["get_property", "path"]}) + "\n").encode())
                            res = json.loads(s.recv(1024).decode().split('\n')[0])
                            current_url = res.get("data")
                            
                            s.sendall((json.dumps({"command": ["get_property", "media-title"]}) + "\n").encode())
                            res = json.loads(s.recv(1024).decode().split('\n')[0])
                            current_title = res.get("data")
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
            
            # Poll every 1 second
            await asyncio.sleep(1)

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
