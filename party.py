import asyncio
import json
import logging
import time
from pyngrok import ngrok
import websockets
from utils.os_detector import IS_WINDOWS

logging.basicConfig(level=logging.ERROR)

# Suppress noisy websocket debug logs at the top level
logging.getLogger("websockets").setLevel(logging.ERROR)

class WatchPartyServer:
    def __init__(self, room_name, host_name, max_users=10):
        self.room_name = room_name
        self.host_name = host_name
        self.max_users = max_users
        
        # Connection state
        self.clients = {}  # ws -> client_info
        
        # User details map: username -> info
        self.users = {
            host_name: {"name": host_name, "role": "host", "muted": False, "deafened": False, "online": False, "ws": None, "ip": "localhost"}
        }
        
        # Playback state
        self.playback_state = {
            "url": None,
            "anime_title": None,
            "episode": None,
            "state": "paused", # playing | paused
            "timestamp": 0.0,
            "last_sync": time.time()
        }
        
        # Banned users set
        self.banned_users = set()
        
    def _broadcast(self, message, exclude_ws=None):
        out_msg = json.dumps(message, ensure_ascii=False)
        for ws in list(self.clients.keys()):
            if ws != exclude_ws:
                # Don't send chat or sync to deafened users, except admin messages
                client = self.clients.get(ws)
                if not client: continue
                
                user_info = self.users.get(client['name'])
                if user_info and user_info['deafened']:
                    if message['type'] in ['chat', 'sync']:
                        continue
                        
                asyncio.create_task(self._send_safe(ws, out_msg))

    async def _send_safe(self, ws, msg_str):
        try:
            await ws.send(msg_str)
        except websockets.exceptions.ConnectionClosed:
            pass
            
    def _broadcast_user_list(self):
        # Send safe user list
        safe_users = []
        for name, info in self.users.items():
            safe_users.append({
                "name": name,
                "role": info["role"],
                "muted": info["muted"],
                "deafened": info["deafened"],
                "online": info["online"],
                "ip": info.get("ip", ""),
                "banned": name in self.banned_users
            })
        self._broadcast({"type": "user_list", "users": safe_users})

    async def handler(self, websocket):
        client_name = None
        
        try:
            # First message should be join
            init_msg_str = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            init_msg = json.loads(init_msg_str)
            
            if init_msg.get('type') != 'join':
                await websocket.close(1008, "Expected join message")
                return
                
            client_name = init_msg.get('name')
            if not client_name:
                await websocket.close(1008, "Username required")
                return

            # Block banned users
            if client_name in self.banned_users:
                await websocket.send(
                    json.dumps({"type": "error", "message": "You are banned from this room."}, ensure_ascii=False)
                )
                await websocket.close(1008, "Banned")
                return

            # Get client IP from websocket
            client_ip = "unknown"
            try:
                client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
            except:
                pass

            if client_name in self.users:
                if self.users[client_name]['online']:
                    await websocket.close(1008, "Username already in use")
                    return
                # Reconnecting
                self.users[client_name]['online'] = True
                self.users[client_name]['ws'] = websocket
                self.users[client_name]['ip'] = client_ip
            else:
                # New user
                current_online = sum(1 for u in self.users.values() if u['online'])
                if current_online >= self.max_users:
                    await websocket.close(1008, "Room full")
                    return
                    
                self.users[client_name] = {"name": client_name, "role": "member", "muted": False, "deafened": False, "online": True, "ws": websocket, "ip": client_ip}
            
            self.clients[websocket] = {'name': client_name}
            
            # Send initial state
            await websocket.send(
                json.dumps(
                    {
                        "type": "room_state",
                        "room_name": self.room_name,
                        "playback": self.playback_state,
                    },
                    ensure_ascii=False,
                )
            )
            
            self._broadcast({"type": "system", "message": f"{client_name} joined the room."})
            self._broadcast_user_list()
            
            # Message loop
            async for message in websocket:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                    
                msg_type = data.get("type")
                user_info = self.users[client_name]
                
                if msg_type == "chat":
                    if user_info['muted']:
                        await websocket.send(
                            json.dumps({"type": "error", "message": "You are muted."}, ensure_ascii=False)
                        )
                        continue
                    
                    self._broadcast({
                        "type": "chat",
                        "sender": client_name,
                        "message": data.get("message", "")
                    })
                    
                elif msg_type == "sync":
                    if user_info['role'] != 'host':
                        continue # Only host can sync
                    
                    self.playback_state.update({
                        "url": data.get("url", self.playback_state["url"]),
                        "anime_title": data.get("anime_title", self.playback_state["anime_title"]),
                        "episode": data.get("episode", self.playback_state["episode"]),
                        "state": data.get("state", self.playback_state["state"]),
                        "timestamp": data.get("timestamp", self.playback_state["timestamp"]),
                        "last_sync": time.time()
                    })
                    
                    # Relay to others
                    self._broadcast({
                        "type": "sync",
                        "playback": self.playback_state
                    }, exclude_ws=websocket)
                    
                elif msg_type == "admin":
                    if user_info['role'] != 'host':
                        continue
                    
                    action = data.get("action")
                    target = data.get("target")
                    
                    if not target or target not in self.users or target == self.host_name:
                        continue
                        
                    target_info = self.users[target]
                    
                    if action == "kick":
                        if target_info['ws']:
                            await target_info['ws'].send(
                                json.dumps(
                                    {"type": "kicked", "message": "You have been kicked by the host."},
                                    ensure_ascii=False,
                                )
                            )
                            await target_info['ws'].close()
                        self._broadcast({"type": "system", "message": f"{target} was kicked."})
                        
                    elif action == "ban":
                        self.banned_users.add(target)
                        if target_info['ws']:
                            await target_info['ws'].send(
                                json.dumps(
                                    {"type": "kicked", "message": "You have been banned by the host."},
                                    ensure_ascii=False,
                                )
                            )
                            await target_info['ws'].close()
                        self._broadcast({"type": "system", "message": f"{target} was banned."})
                    
                    elif action == "unban":
                        if target in self.banned_users:
                            self.banned_users.discard(target)
                            self._broadcast({"type": "system", "message": f"{target} was unbanned."})
                        else:
                            if self.host_name in self.users and self.users[self.host_name]['ws']:
                                await self.users[self.host_name]['ws'].send(
                                    json.dumps(
                                        {"type": "system", "message": f"{target} is not banned."},
                                        ensure_ascii=False,
                                    )
                                )
                        
                    elif action == "mute":
                        target_info['muted'] = not target_info['muted']
                        status = "muted" if target_info['muted'] else "unmuted"
                        self._broadcast({"type": "system", "message": f"{target} has been {status}."})
                        
                    elif action == "deafen":
                        target_info['deafened'] = not target_info['deafened']
                        status = "deafened" if target_info['deafened'] else "undeafened"
                        self._broadcast({"type": "system", "message": f"{target} has been {status}."})
                        
                    self._broadcast_user_list()
                    
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            print(f"Error handling client: {e}")
        finally:
            if websocket in self.clients:
                del self.clients[websocket]
            if client_name and client_name in self.users:
                role = self.users[client_name].get('role')
                if role == 'host':
                    # Keep host in list but mark offline
                    self.users[client_name]['online'] = False
                    self.users[client_name]['ws'] = None
                else:
                    # Remove non-host users entirely so they don't appear as ghosts
                    del self.users[client_name]
                self._broadcast({"type": "system", "message": f"{client_name} left the room."})
                self._broadcast_user_list()
                
                # If host leaves, completely terminate the server thereby disconnecting everyone
                if role == 'host':
                    logger.info("Host disconnected. Shutting down global server.")
                    # Close all remaining client connections gracefully
                    for ws in list(self.clients.keys()):
                        try:
                            await ws.close(1001, "Host ended the party")
                        except:
                            pass
                    import os
                    os._exit(0)

import logging
import traceback
from datetime import datetime
import os

# Ensure .logs directory exists
log_dir = os.path.join(os.path.dirname(__file__), "data", "logs")
os.makedirs(log_dir, exist_ok=True)

# Configure logging to write to .logs/streamix_backend.log
log_file_path = os.path.join(log_dir, "streamix_backend.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8')
    ]
)
logger = logging.getLogger("streamix_party")

# Suppress noisy handshake errors from ngrok 0-byte TCP ping probes
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
logging.getLogger("websockets").setLevel(logging.CRITICAL)

async def _health_check(connection, request):
    """Handle non-WebSocket HTTP requests (ngrok health pings, browser checks, etc.)
    Return None to proceed with the normal WebSocket handshake.
    In websockets v16, return a Response to reject the upgrade."""
    # All requests proceed to WebSocket handshake
    return None

async def serve(room_name, host_name, max_users, port=9000):
    server_logic = WatchPartyServer(room_name, host_name, max_users)
    server = await websockets.serve(
        server_logic.handler,
        "0.0.0.0",
        port,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=2**20,  # 1MB max message
        process_request=_health_check,
        # No origin restriction — allow connections from any origin
        # (Python clients, ngrok proxy, localhost, etc.)
    )
    logger.info(f"WebSocket server listening on 0.0.0.0:{port}")
    return server

def start_server_and_tunnel(room_name, host_name, max_users=10, port=9000):
    logger.info(f"Starting watch party server for room: {room_name}")
    print(f"[Party] Starting watch party: {room_name}")
    
    # Aggressive cleanup of any zombie ngrok processes before starting
    import os
    if IS_WINDOWS:
        os.system("taskkill /F /IM ngrok.exe /T >nul 2>&1")
    else:
        os.system("pkill -9 ngrok >/dev/null 2>&1")
        
    try:
        # Fix Python 3.10+ asyncio warning
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 1. Start WebSocket server FIRST so port 9000 is ready for connections
        logger.info("Starting WebSocket server...")
        print(f"[Party] Starting WebSocket server on port {port}...")
        server = loop.run_until_complete(serve(room_name, host_name, max_users, port))
        print(f"[Party] WebSocket server listening on port {port}")
        
        # 2. THEN open ngrok tunnel (so it points to an already-listening port)
        logger.info(f"Opening ngrok tunnel on port {port}...")
        print("[Party] Opening ngrok tunnel...")
        
        # Use http instead of tcp to avoid auth-token requirements and raw TCP ping issues
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url.replace("https://", "wss://").replace("http://", "ws://")
        logger.info(f"ngrok tunnel opened successfully: {public_url}")
        print(f"[Party] Tunnel ready: {public_url}")
        
        # 3. Save room info for the admin client to read
        json_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(json_dir, exist_ok=True)
        party_info_path = os.path.join(json_dir, "party_info.json")
        with open(party_info_path, "w") as f:
            json.dump({
                "url": public_url,
                "room_name": room_name,
                "host_name": host_name,
                "max_users": max_users
            }, f)
            
        print(f"[Party] Room info saved. Party is live!")
        
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down party server...")
        finally:
            try:
                ngrok.disconnect(tunnel.public_url)
                ngrok.kill()
            except Exception as e:
                logger.error(f"Error while disconnecting ngrok: {e}")
                
            server.close()
            loop.run_until_complete(server.wait_closed())
            try:
                os.remove(party_info_path)
            except:
                pass

    except Exception as e:
        err_msg = f"Failed to start party server or ngrok tunnel:\n{traceback.format_exc()}"
        logger.error(err_msg)
        print(f"[Party] Error: {e}")

if __name__ == "__main__":
    import sys
    room = sys.argv[1] if len(sys.argv) > 1 else "Watch Party"
    host = sys.argv[2] if len(sys.argv) > 2 else "Host"
    start_server_and_tunnel(room, host)
