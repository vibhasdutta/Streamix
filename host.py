import asyncio
import json
import os
import sys
import time
import threading
from datetime import datetime

import websockets
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal, Container
from textual.widgets import Header, Footer, Input, RichLog, Static, Label
from textual.binding import Binding
from textual.message import Message
from textual.worker import Worker, WorkerState
from textual import on, work
from rich.text import Text
from rich.table import Table
from rich import box
from rich.panel import Panel

from utils.os_detector import IS_WINDOWS
import config  # Shared config

# --- Custom Messages ---
class ChatMessage(Message):
    def __init__(self, sender: str, message: str, is_system: bool = False) -> None:
        self.sender = sender
        self.message = message
        self.is_system = is_system
        super().__init__()

class UserListUpdate(Message):
    def __init__(self, users: list) -> None:
        self.users = users
        super().__init__()

class SyncUpdate(Message):
    def __init__(self, playback_state: dict) -> None:
        self.playback_state = playback_state
        super().__init__()

# --- Widgets ---

class UserList(Static):
    """A widget to display the current users in the room."""
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
            
            # Flags
            flags = []
            if u.get("muted"): flags.append("M")
            if u.get("deafened"): flags.append("D")
            
            name_styled = name
            if flags:
                name_styled += f" [dim]({','.join(flags)})[/dim]"
            
            table.add_row(status, name_styled)
        return table

# --- Main App ---

class StreamixHost(App):
    """Streamix Host TUI App."""
    
    TITLE = "Streamix Host"
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

    #input_container {
        height: 3;
        dock: bottom;
    }

    RichLog {
        height: 1fr;
    }

    Input {
        border: none;
    }

    .system_msg {
        color: $text-disabled;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("f1", "help", "Help", show=True),
        Binding("f2", "toggle_users", "Toggle Users", show=True),
    ]

    def __init__(self, username="Host", ipc_path=None):
        super().__init__()
        self.username = username
        self.ipc_path = ipc_path
        self.ws = None
        self.room_url = "Connecting..."
        self.room_name = "Watch Party"
        
        # Load config
        self.user_config = config.get_admin_config()
        self.notifications_enabled = self.user_config.get("notifications", True)

        # Room Info from file
        try:
            info_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "party_info.json")
            if os.path.exists(info_path):
                with open(info_path, "r") as f:
                    info = json.load(f)
                    self.room_url = info.get("url", "ws://localhost:9000")
                    self.room_name = info.get("room_name", "Party")
        except:
            pass

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main_container"):
            with Vertical(id="chat_container"):
                yield RichLog(id="chat_log", highlight=True, markup=True)
                yield Input(placeholder="Type message or command (/help)...", id="chat_input")
            with Vertical(id="side_panel"):
                yield Label("[bold magenta]USERS[/bold magenta]", id="user_label")
                yield UserList(id="user_list")
        yield Footer()

    async def on_mount(self) -> None:
        self.chat_log = self.query_one("#chat_log", RichLog)
        self.user_list = self.query_one("#user_list", UserList)
        self.title = f"Streamix - {self.room_name}"
        self.sub_title = f"🔗 {self.room_url}"
        
        # Start background tasks
        self.run_worker(self.connect_to_server(), exclusive=True, name="ws_worker")
        self.mpv_poller()
        
        self.log_system("System", "Host TUI initialized. Welcome!")
        self.log_system("System", "Type /help for a list of commands.")

    @on(Input.Submitted, "#chat_input")
    async def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        
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
            self.show_help()
        elif action in ["/kick", "/mute", "/deafen", "/ban", "/unban"]:
            if not target:
                self.log_system("Error", f"Usage: {action} <username>")
                return
            await self.ws_send({"type": "admin", "action": action[1:], "target": target})
        elif action == "/vol":
            try:
                level = int(target)
                await self.send_mpv_command(["set_property", "volume", level])
                config.update_admin_config(volume=level)
                self.log_system("Audio", f"Volume set to {level}%")
            except:
                self.log_system("Error", "Usage: /vol <0-150>")
        elif action in ["/quit", "/exit", "/close"]:
            self.exit()
        else:
            self.log_system("Error", f"Unknown command: {action}")

    def show_help(self):
        help_text = """
[bold]Commands:[/bold]
[cyan]/kick <user>[/cyan] - Remove user
[cyan]/mute <user>[/cyan] - Toggle server mute
[cyan]/ban <user>[/cyan] - Permanent ban
[cyan]/vol <n>[/cyan] - Set local MPV volume
[cyan]/quit[/cyan] - Close party
"""
        self.chat_log.write(help_text)

    # --- Communication ---

    async def connect_to_server(self):
        try:
            async with websockets.connect("ws://127.0.0.1:9000") as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "join", "name": self.username, "role": "host"}))
                
                async for message_str in ws:
                    data = json.loads(message_str)
                    await self.handle_server_message(data)
        except Exception as e:
            self.log_system("Error", f"Connection failed: {e}")
            await asyncio.sleep(5)

    async def handle_server_message(self, data: dict):
        mt = data.get("type")
        if mt == "chat":
            sender = data.get("sender")
            msg = data.get("message")
            color = "magenta" if sender == self.username else "cyan"
            self.chat_log.write(f"[bold {color}]{sender}[/bold {color}]: {msg}")
            if sender != self.username:
                self.play_notification()
        elif mt == "system":
            self.log_system("System", data.get("message"))
        elif mt == "user_list":
            self.user_list.update_users(data.get("users", []))
        elif mt == "room_state":
            self.log_system("Room", f"Joined room: {data.get('room_name')}")

    async def ws_send(self, data: dict):
        if self.ws:
            await self.ws.send(json.dumps(data, ensure_ascii=False))

    async def send_chat(self, msg: str):
        await self.ws_send({"type": "chat", "message": msg})

    # --- Logic ---

    def log_system(self, tag: str, message: str):
        self.chat_log.write(f"[dim italic][{tag}] {message}[/dim italic]")

    @work(exclusive=True)
    async def mpv_poller(self):
        """Polls local mpv IPC for playback state to sync to server."""
        if not self.ipc_path: return
        await asyncio.sleep(2)
        while True:
            try:
                state = await self.query_mpv()
                if state:
                    await self.ws_send({
                        "type": "sync",
                        "state": state["state"],
                        "timestamp": state["timestamp"],
                        "url": state.get("url"),
                        "anime_title": state.get("title")
                    })
                else:
                    await self.ws_send({"type": "sync", "state": "closed", "timestamp": 0})
            except: pass
            await asyncio.sleep(1)

    async def query_mpv(self):
        try:
            import socket
            if IS_WINDOWS:
                with open(self.ipc_path, 'r+') as f:
                    f.write(json.dumps({"command": ["get_property", "pause"]}) + "\n")
                    f.flush()
                    paused = json.loads(f.readline()).get("data", False)
                    f.write(json.dumps({"command": ["get_property", "time-pos"]}) + "\n")
                    f.flush()
                    pos = json.loads(f.readline()).get("data", 0.0)
                    f.write(json.dumps({"command": ["get_property", "path"]}) + "\n")
                    f.flush()
                    path = json.loads(f.readline()).get("data")
                    f.write(json.dumps({"command": ["get_property", "media-title"]}) + "\n")
                    f.flush()
                    title = json.loads(f.readline()).get("data")
                    return {"state": "paused" if paused else "playing", "timestamp": pos, "url": path, "title": title}
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.ipc_path)
                s.sendall(b'{"command": ["get_property", "pause"]}\n')
                paused = json.loads(s.recv(1024).decode().split('\n')[0]).get("data", False)
                s.sendall(b'{"command": ["get_property", "time-pos"]}\n')
                pos = json.loads(s.recv(1024).decode().split('\n')[0]).get("data", 0.0)
                s.sendall(b'{"command": ["get_property", "path"]}\n')
                path = json.loads(s.recv(1024).decode().split('\n')[0]).get("data")
                s.sendall(b'{"command": ["get_property", "media-title"]}\n')
                title = json.loads(s.recv(1024).decode().split('\n')[0]).get("data")
                s.close()
                return {"state": "paused" if paused else "playing", "timestamp": pos, "url": path, "title": title}
        except: return None

    async def send_mpv_command(self, command):
        try:
            if IS_WINDOWS:
                with open(self.ipc_path, "w") as f:
                    f.write(json.dumps({"command": command}) + "\n")
            else:
                import socket
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.ipc_path)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                s.close()
        except: pass

    def play_notification(self):
        if not self.notifications_enabled: return
        # Notification logic...
        pass

if __name__ == "__main__":
    import sys
    username = sys.argv[1] if len(sys.argv) > 1 else "Host"
    ipc = sys.argv[2] if len(sys.argv) > 2 else None
    
    app = StreamixHost(username=username, ipc_path=ipc)
    app.run()
