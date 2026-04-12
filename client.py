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
from rich.markup import escape
from rich.console import Group
from utils.os_detector import IS_WINDOWS

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
        self.mpv_process = None
        self.mpv_ipc_path = fr"\\.\pipe\anilix_client_{int(time.time())}" if IS_WINDOWS else f"/tmp/anilix_client_{int(time.time())}.sock"
        
        # Local-only filters (only affect this user's view)
        self.local_muted = set()
        self.local_deafened = set()
        self.current_video_url = None
        
        # Load persistent config
        from config import get_client_config
        self.config = get_client_config()
        self.volume = self.config.get("volume", 100)
        self.notifications_enabled = self.config.get("notifications", True)
        self.chat_limit = self.config.get("chat_history_limit", 50)

    def _append_chat(self, sender, message):
        payload = {"sender": sender, "message": message}
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

    def _play_notification(self):
        if not self.notifications_enabled:
            return
        try:
            from main import get_mpv_path
            mpv_path = get_mpv_path()
            if mpv_path:
                sound_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sound_assests", "notification.mp3")
                if os.path.exists(sound_path):
                    subprocess.Popen([mpv_path, "--no-video", "--no-terminal", sound_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                    "User-Agent": "Anilix-Party-Client/1.0",
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
                    self.chat_history.append(f"[dim]Connecting to server (attempt {attempt}/{max_retries})...[/dim]")
                    self.ws = await websockets.connect(self.ws_url, **connect_kwargs)
                    break
                except (ConnectionRefusedError, OSError, websockets.exceptions.InvalidStatusCode) as e:
                    if attempt < max_retries:
                        self.chat_history.append(f"[dim]Could not reach server, retrying in {retry_delay:.0f}s... ([red]{e}[/red])[/dim]")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 15.0)
                    else:
                        raise e
            
            # Join as member
            await self.ws.send(
                json.dumps({"type": "join", "name": self.username, "role": "member"}, ensure_ascii=False)
            )
            
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
                        self._append_chat(sender, msg)
                        if sender != self.username:
                            self._play_notification()
                        
                    elif mt == "system":
                        msg = data.get("message", "")
                        self.chat_history.append(f"[dim italic]{escape(msg)}[/dim italic]")
                        
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
                            await self._send_mpv_command(["set_property", "pause", state == "paused"])
                            client_time = await asyncio.to_thread(self._get_mpv_property_sync, "time-pos")
                            
                            # Only seek if we're out of sync by more than 2 seconds to avoid stuttering/frame drops
                            if client_time is None or abs(client_time - timestamp) > 2.0:
                                await self._send_mpv_command(["set_property", "time-pos", timestamp])
                            
                    elif mt == "kicked":
                        self.chat_history.append(f"[bold red]You have been kicked from the party.[/bold red]")
                        self.running = False
                        
                    elif mt == "error":
                        self.chat_history.append(
                            f"[bold red]Error:[/bold red] {escape(data.get('message', ''))}"
                        )
                        
                    if len(self.chat_history) > 100:
                        self.chat_history = self.chat_history[-100:]
                        
                except Exception as e:
                    pass
                    
        except websockets.exceptions.ConnectionClosed as e:
            self.chat_history.append(f"[bold red]Connection lost: {e.reason if e.reason else 'Server closed connection'}[/bold red]")
            self.running = False
        except websockets.exceptions.InvalidStatusCode as e:
            self.chat_history.append(f"[bold red]Connection rejected (HTTP {e.status_code}). Is the party still active?[/bold red]")
            self.running = False
        except ConnectionRefusedError:
            self.chat_history.append("[bold red]Connection refused. The party server may not be running.[/bold red]")
            self.running = False
        except Exception as e:
            self.chat_history.append(f"[bold red]Failed to connect: {type(e).__name__}: {e}[/bold red]")
            self.running = False

    def _launch_mpv(self, url, title, timestamp):
        from main import get_mpv_path
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
                self.chat_history.append(f"[yellow]Deafened {target} (local). You will no longer hear their voice.[/yellow]")
                
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
            self.chat_history.append("[cyan]/close[/cyan] — Close this window")
            self.chat_history.append("[cyan]/help[/cyan] — Show this help")
        elif action in ["/exit", "/close"]:
            self.running = False
        else:
            self.chat_history.append(f"[red]Unknown command: {action}. Type /help[/red]")

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
        
        # Chat panel — align to bottom so new messages appear at bottom
        import shutil
        max_lines = max(5, shutil.get_terminal_size().lines - 10)
        chat_content = self._render_chat_feed(max_lines)
        layout["chat"].update(Panel(Align(chat_content, vertical="bottom"), title="Chat", border_style="blue"))
        
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
            
            # Render loop — always refresh for instant chat updates
            with Live(self.generate_layout(), refresh_per_second=15) as live:
                while self.running:
                    live.update(self.generate_layout())
                    await asyncio.sleep(0.05)
                    if self.mpv_process and self.mpv_process.poll() is not None:
                        # mpv closed manually by user or by host changing episode
                        # Don't close the chat — just clear the process so we don't keep polling
                        self.mpv_process = None
        finally:
            input_handler.cleanup()
            # Kill mpv if still running (no point playing without sync)
            if getattr(self, 'mpv_process', None):
                try: await self._send_mpv_command(["quit"])
                except: pass
                try: self.mpv_process.terminate()
                except: pass
                try: self.mpv_process.kill()
                except: pass
            # Close WebSocket gracefully
            if self.ws:
                try: await self.ws.close()
                except: pass
            # Show shutdown message briefly so user can read disconnect reason
            if not self.running:
                console.print("\n[bold yellow]Closing in 3 seconds...[/bold yellow]")
                await asyncio.sleep(3)

if __name__ == "__main__":
    import sys
    import signal
    
    # Handle SIGTERM from pkill so script exits cleanly
    def _handle_term(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_term)
    
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:9000"
    username = sys.argv[2] if len(sys.argv) > 2 else f"Guest_{int(time.time())%1000}"
    
    try:
        client = PartyClient(ws_url=url, username=username)
        asyncio.run(client.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        print(f"Error: {e}")
