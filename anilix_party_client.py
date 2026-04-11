import asyncio
import json
import websockets
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

console = Console()

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
        self.mpv_process = None
        self.mpv_ipc_path = fr"\\.\pipe\anilix_client_{int(time.time())}" if os.name == 'nt' else f"/tmp/anilix_client_{int(time.time())}.sock"
        
        # Local-only filters (only affect this user's view)
        self.local_muted = set()
        self.local_deafened = set()
        self.current_video_url = None
        
        # Load persistent config
        from party_config import get_client_config
        self.config = get_client_config()
        self.volume = self.config.get("volume", 100)
        self.notifications_enabled = self.config.get("notifications", True)
        self.chat_limit = self.config.get("chat_history_limit", 50)

    async def _send_mpv_command(self, command):
        """Sends an IPC command to mpv if it's running."""
        try:
            if os.name == 'nt':
                with open(self.mpv_ipc_path, "w") as f:
                    f.write(json.dumps({"command": command}) + "\n")
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.mpv_ipc_path)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                s.close()
        except Exception:
            pass

    def _play_notification(self):
        if not self.notifications_enabled:
            return
        try:
            from anilix import get_mpv_path
            mpv_path = get_mpv_path()
            if mpv_path:
                sound_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sound_assests", "notification.mp3")
                if os.path.exists(sound_path):
                    subprocess.Popen([mpv_path, "--no-video", "--no-terminal", sound_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    async def connect_and_listen(self):
        try:
            self.ws = await websockets.connect(self.ws_url)
            
            # Join as member
            await self.ws.send(json.dumps({"type": "join", "name": self.username, "role": "member"}))
            
            # Message loop
            async for message_str in self.ws:
                if not self.running:
                    break
                
                try:
                    data = json.loads(message_str)
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
                            
                            self._launch_mpv(url, title, timestamp)
                            
                            # pause if host is paused
                            if state == 'paused':
                                await asyncio.sleep(1) # wait for mpv to start
                                await self._send_mpv_command(["set_property", "pause", True])
                                
                    elif mt == "user_list":
                        self.users = data.get("users", [])
                        
                    elif mt == "chat":
                        sender = data.get("sender", "Unknown")
                        msg = data.get("message", "")
                        # Local filter: skip if we locally muted or deafened this user
                        if sender in self.local_muted or sender in self.local_deafened:
                            continue
                        self.chat_history.append(f"[bold cyan]{sender}:[/bold cyan] {msg}")
                        if sender != self.username:
                            self._play_notification()
                        
                    elif mt == "system":
                        msg = data.get("message", "")
                        self.chat_history.append(f"[dim italic]{msg}[/dim italic]")
                        
                    elif mt == "sync":
                        playback = data.get("playback", {})
                        if playback:
                            url = playback.get("url")
                            title = playback.get("anime_title", "Party Video")
                            timestamp = playback.get("timestamp", 0)
                            state = playback.get("state", "playing")
                            
                            # Auto-launch or restart mpv if video changed
                            if url and hasattr(self, 'current_video_url') and self.current_video_url != url:
                                if getattr(self, 'mpv_process', None):
                                    try:
                                        self.mpv_process.terminate()
                                    except:
                                        pass
                                self._launch_mpv(url, title, timestamp)
                                self.current_video_url = url
                                await asyncio.sleep(1) # wait for mpv IPC
                                
                            # Sync mpv (sync is never locally filtered — host controls playback)
                            await self._send_mpv_command(["set_property", "time-pos", timestamp])
                            await self._send_mpv_command(["set_property", "pause", state == "paused"])
                            
                    elif mt == "kicked":
                        self.chat_history.append(f"[bold red]You have been kicked from the party.[/bold red]")
                        self.running = False
                        
                    elif mt == "error":
                        self.chat_history.append(f"[bold red]Error:[/bold red] {data.get('message')}")
                        
                    if len(self.chat_history) > 20:
                        self.chat_history = self.chat_history[-20:]
                        
                except Exception as e:
                    pass
                    
        except websockets.exceptions.ConnectionClosed:
            self.chat_history.append("[bold red]Connection lost.[/bold red]")
            self.running = False
        except Exception as e:
            self.chat_history.append(f"[bold red]Failed to connect: {e}[/bold red]")
            self.running = False

    def _launch_mpv(self, url, title, timestamp):
        from anilix import get_mpv_path
        mpv_path = get_mpv_path()
        if not mpv_path:
            self.chat_history.append("[bold red]mpv not found! Cannot sync video.[/bold red]")
            return

        args = [
            mpv_path,
            f"--title={title} (Watch Party Sync)",
            f"--start={timestamp}",
            f"--input-ipc-server={self.mpv_ipc_path}",
            "--referrer=https://kwik.cx/",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            url
        ]
        
        self.mpv_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    def _handle_local_command(self, cmd):
        """Handle client-side /commands for local mute/deafen."""
        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        target = parts[1].strip() if len(parts) > 1 else ""
        
        if action == "/mute":
            if not target:
                if self.local_muted:
                    self.chat_history.append(f"[yellow]Locally muted: {', '.join(self.local_muted)}[/yellow]")
                else:
                    self.chat_history.append("[yellow]No one is locally muted. Usage: /mute <user>[/yellow]")
                return
            if target in self.local_muted:
                self.local_muted.discard(target)
                self.chat_history.append(f"[green]Unmuted {target} (local).[/green]")
            else:
                self.local_muted.add(target)
                self.chat_history.append(f"[yellow]Muted {target} (local). Their messages are now hidden for you.[/yellow]")
                
        elif action == "/deafen":
            if not target:
                if self.local_deafened:
                    self.chat_history.append(f"[yellow]Locally deafened: {', '.join(self.local_deafened)}[/yellow]")
                else:
                    self.chat_history.append("[yellow]No one is locally deafened. Usage: /deafen <user>[/yellow]")
                return
            if target in self.local_deafened:
                self.local_deafened.discard(target)
                self.chat_history.append(f"[green]Undeafened {target} (local).[/green]")
            else:
                self.local_deafened.add(target)
                self.chat_history.append(f"[yellow]Deafened {target} (local). Their chat & activity hidden for you.[/yellow]")
                
        elif action == "/users":
            if self.users:
                user_list = ", ".join([f"{u['name']}({'⭐' if u.get('role')=='host' else '👤'})" for u in self.users])
                self.chat_history.append(f"[cyan]Online: {user_list}[/cyan]")
            else:
                self.chat_history.append("[dim]No user list yet.[/dim]")
        
        elif action == "/vol":
            try:
                level = int(target) if target else -1
                if 0 <= level <= 150:
                    asyncio.create_task(self._send_mpv_command(["set_property", "volume", level]))
                    self.chat_history.append(f"[green]🔊 Volume set to {level}%[/green]")
                else:
                    self.chat_history.append("[yellow]Usage: /vol <0-150>[/yellow]")
            except ValueError:
                self.chat_history.append("[yellow]Usage: /vol <0-150>[/yellow]")
                
        elif action == "/help":
            self.chat_history.append("[cyan]/mute <user>[/cyan] — Toggle hide their messages (local only)")
            self.chat_history.append("[cyan]/deafen <user>[/cyan] — Toggle hide their chat & activity (local only)")
            self.chat_history.append("[cyan]/vol <0-150>[/cyan] — Set your video volume")
            self.chat_history.append("[cyan]/users[/cyan] — List online users")
            self.chat_history.append("[cyan]/help[/cyan] — Show this help")
        else:
            self.chat_history.append(f"[red]Unknown command: {action}. Type /help[/red]")

    def generate_layout(self):
        from rich import box
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="input", size=3)
        )
        layout["main"].split_row(
            Layout(name="users", size=28),
            Layout(name="chat")
        )
        
        header_text = f"[bold magenta]{self.room_name}[/bold magenta] | 👤 {self.username}"
        layout["header"].update(Panel(Align.center(header_text), box=box.ROUNDED))
        
        # Users panel
        from rich.table import Table
        user_table = Table(show_header=False, expand=True, box=None)
        user_table.add_column("Status")
        user_table.add_column("Name")
        for u in self.users:
            icon = "👑" if u.get('role') == 'host' else "🟢" if u.get('online') else "🔴"
            name = u.get('name', '?')
            flags = []
            if name in self.local_muted: flags.append("🔇")
            if name in self.local_deafened: flags.append("🔕")
            name_display = f"{name} {' '.join(flags)}" if flags else name
            user_table.add_row(icon, name_display)
        layout["users"].update(Panel(user_table, title="Users", border_style="cyan"))
        
        # Chat panel
        import shutil
        max_lines = max(5, shutil.get_terminal_size().lines - 15)
        chat_content = "\n".join(self.chat_history[-max_lines:])
        layout["chat"].update(Panel(Text.from_markup(chat_content), title="Chat", border_style="blue"))
        
        input_panel = Panel(f"> {self.input_text}█", border_style="green", title="Message (/help for commands)")
        layout["input"].update(input_panel)
        
        return layout

    async def input_loop(self, input_handler):
        while self.running:
            try:
                c = input_handler.get_char()
                if c:
                    if c == '\x08': # backspace
                        self.input_text = self.input_text[:-1]
                    elif c in ('\r', '\n'): # enter
                        if self.input_text:
                            if self.input_text.startswith('/'):
                                self._handle_local_command(self.input_text)
                            elif self.ws:
                                await self.ws.send(json.dumps({"type": "chat", "message": self.input_text}))
                            self.input_text = ""
                    else:
                        if c.isprintable():
                            self.input_text += c
            except Exception as e:
                pass
            await asyncio.sleep(0.01)

    async def run(self):
        from rich import box
        from party_input import NonBlockingInput
        
        input_handler = NonBlockingInput()
        try:
            asyncio.create_task(self.connect_and_listen())
            asyncio.create_task(self.input_loop(input_handler))
            
            last_state = None
            
            with Live(self.generate_layout(), refresh_per_second=10) as live:
                while self.running:
                    current_state = (self.input_text, len(self.chat_history), len(self.users), hash(str(self.users)))
                    if current_state != last_state:
                        live.update(self.generate_layout())
                        last_state = current_state
                    await asyncio.sleep(0.05)
                    if self.mpv_process and self.mpv_process.poll() is not None:
                        # mpv closed manually by user
                        self.running = False
        finally:
            input_handler.cleanup()
            if getattr(self, 'mpv_process', None):
                try: self.mpv_process.terminate()
                except: pass

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:9000"
    username = sys.argv[2] if len(sys.argv) > 2 else f"Guest_{int(time.time())%1000}"
    
    try:
        client = PartyClient(ws_url=url, username=username)
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
