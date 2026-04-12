import asyncio
import json
import os
import sys
import time
import subprocess
import socket
from datetime import datetime

import websockets
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal, Container
from textual.widgets import Header, Footer, Input, RichLog, Static, Label
from textual.binding import Binding
from textual.message import Message
from textual.worker import Worker
from textual import on, work
from rich.text import Text
from rich.table import Table
from rich import box

from utils.os_detector import IS_WINDOWS
import config

# --- Widgets ---

class UserList(Static):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.users = []

    def update_users(self, users: list):
        self.users = users
        self.refresh()

    def render(self) -> Table:
        table = Table(show_header=True, expand=True, box=box.SIMPLE, header_style="bold magenta")
        table.add_column("S", width=2)
        table.add_column("Name")
        
        for u in self.users:
            status = "👑" if u.get('role') == 'host' else ("🟢" if u.get('online') else "🔴")
            name = u.get("name", "Unknown")
            table.add_row(status, name)
        return table

# --- Main App ---

class StreamixClient(App):
    TITLE = "Streamix Client"
    CSS = """
    Screen {
        background: $boost;
    }
    #main_container {
        layout: horizontal;
        height: 1fr;
    }
    #chat_container {
        width: 3fr;
        border: solid $accent;
        padding: 1;
    }
    #side_panel {
        width: 1fr;
        border-left: solid $accent;
        padding: 1;
        background: $surface;
    }
    RichLog {
        height: 1fr;
    }
    Input {
        border: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("f1", "help", "Help", show=True),
        Binding("f5", "rejoin", "Re-join Stream", show=True),
    ]

    def __init__(self, ws_url, username):
        super().__init__()
        self.ws_url = ws_url
        self.username = username
        self.ws = None
        self.room_name = "Connecting..."
        
        self.mpv_process = None
        self.mpv_ipc_path = fr"\\.\pipe\streamix_client_{int(time.time())}" if IS_WINDOWS else f"/tmp/streamix_client_{int(time.time())}.sock"
        self.current_video_url = None
        
        self.user_config = config.get_client_config()

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main_container"):
            with Vertical(id="chat_container"):
                yield RichLog(id="chat_log", highlight=True, markup=True)
                yield Input(placeholder="Type message...", id="chat_input")
            with Vertical(id="side_panel"):
                yield Label("[bold magenta]USERS[/bold magenta]")
                yield UserList(id="user_list")
        yield Footer()

    async def on_mount(self) -> None:
        self.chat_log = self.query_one("#chat_log", RichLog)
        self.user_list = self.query_one("#user_list", UserList)
        self.run_worker(self.connect_to_server(), exclusive=True)
        self.log_system("System", f"Connecting to {self.ws_url}...")

    @on(Input.Submitted, "#chat_input")
    async def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text: return
        event.input.value = ""
        
        if text.startswith("/"):
            await self.handle_command(text)
        else:
            await self.send_chat(text)

    async def handle_command(self, cmd: str):
        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""

        if action == "/help":
            self.chat_log.write("[bold]Commands:[/bold]\n/vol <n> - Set volume\n/rejoin - Fix sync\n/quit - Leave party")
        elif action == "/vol":
            try:
                level = int(target)
                await self.send_mpv_command(["set_property", "volume", level])
                self.log_system("Audio", f"Volume set to {level}%")
            except: pass
        elif action in ["/rejoin", "/join"]:
            await self.action_rejoin()
        elif action in ["/quit", "/exit"]:
            self.exit()

    async def action_rejoin(self):
        if self.current_video_url:
            self.log_system("System", "Re-joining stream...")
            self.launch_mpv(self.current_video_url, "Streamix Sync", 0)

    async def connect_to_server(self):
        try:
            connect_kwargs = {"ping_interval": 20, "ping_timeout": 20}
            if self.ws_url.startswith("wss://"):
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                connect_kwargs["ssl"] = ctx

            async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "join", "name": self.username, "role": "member"}))
                
                async for message_str in ws:
                    data = json.loads(message_str)
                    await self.handle_server_message(data)
        except Exception as e:
            self.log_system("Error", f"Connection lost: {e}")

    async def handle_server_message(self, data: dict):
        mt = data.get("type")
        if mt == "chat":
            sender = data.get("sender")
            msg = data.get("message")
            color = "magenta" if sender == self.username else "cyan"
            self.chat_log.write(f"[bold {color}]{sender}[/bold {color}]: {msg}")
        elif mt == "system":
            self.log_system("System", data.get("message"))
        elif mt == "user_list":
            self.user_list.update_users(data.get("users", []))
        elif mt == "room_state":
            self.room_name = data.get("room_name", "Party")
            self.title = f"Streamix - {self.room_name}"
            playback = data.get("playback", {})
            if playback.get("url"):
                self.handle_sync(playback)
        elif mt == "sync":
            self.handle_sync(data.get("playback", {}))
        elif mt == "kicked":
            self.log_system("Alert", "You have been kicked.")
            await asyncio.sleep(3)
            self.exit()

    def handle_sync(self, playback: dict):
        url = playback.get("url")
        title = playback.get("anime_title", "Video")
        timestamp = playback.get("timestamp", 0)
        state = playback.get("state", "playing")

        if state == "closed":
            self.kill_mpv()
            return

        if url and self.current_video_url != url:
            self.kill_mpv()
            self.launch_mpv(url, title, timestamp)
            self.current_video_url = url
            return

        # Sync existing mpv
        asyncio.create_task(self.sync_mpv(state, timestamp))

    async def sync_mpv(self, state, timestamp):
        await self.send_mpv_command(["set_property", "pause", state == "paused"])
        # Seek if out of sync
        try:
            curr = await self.query_mpv_property("time-pos")
            if curr is None or abs(curr - timestamp) > 2.0:
                await self.send_mpv_command(["set_property", "time-pos", timestamp])
        except: pass

    def launch_mpv(self, url, title, timestamp):
        # We need to import main.py's get_mpv_path, but it's now main.py
        # For now we'll assume it's in path or use common locations
        import shutil
        mpv_path = shutil.which("mpv")
        if not mpv_path: return

        args = [
            mpv_path,
            f"--title={title} (Streamix Client)",
            f"--start={timestamp}",
            f"--input-ipc-server={self.mpv_ipc_path}",
            "--referrer=https://kwik.cx/",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            url
        ]
        self.mpv_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def send_mpv_command(self, command):
        try:
            if IS_WINDOWS:
                with open(self.mpv_ipc_path, "w") as f:
                    f.write(json.dumps({"command": command}) + "\n")
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.mpv_ipc_path)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                s.close()
        except: pass

    async def query_mpv_property(self, prop):
        try:
            if IS_WINDOWS:
                with open(self.mpv_ipc_path, 'r+') as f:
                    f.write(json.dumps({"command": ["get_property", prop]}) + "\n")
                    f.flush()
                    return json.loads(f.readline()).get("data")
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.mpv_ipc_path)
                s.sendall(f'{{"command": ["get_property", "{prop}"]}}\n'.encode())
                res = json.loads(s.recv(1024).decode().split('\n')[0])
                s.close()
                return res.get("data")
        except: return None

    def kill_mpv(self):
        if self.mpv_process:
            try: self.mpv_process.terminate()
            except: pass
            self.mpv_process = None
            self.current_video_url = None

    async def send_chat(self, msg: str):
        if self.ws:
            await self.ws.send(json.dumps({"type": "chat", "message": msg}))

    def log_system(self, tag: str, message: str):
        self.chat_log.write(f"[dim italic][{tag}] {message}[/dim italic]")

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:9000"
    user = sys.argv[2] if len(sys.argv) > 2 else "Guest"
    app = StreamixClient(url, user)
    app.run()
