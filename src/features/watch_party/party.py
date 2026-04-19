import asyncio

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import json
import logging
import time
import hashlib
from pyngrok import ngrok
import websockets
from shared.utils.os_detector import IS_WINDOWS
from core.config import get_admin_config
from core.paths import PARTY_INFO_PATH, ensure_data_directories
from shared.utils.logger import install_asyncio_exception_handler

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("watch_party")

# Suppress noisy websocket debug logs
logging.getLogger("websockets").setLevel(logging.ERROR)

class WatchPartyServer:
    def __init__(self, room_name, host_name, max_users=10):
        self._history_limit = get_admin_config().get("chat_history_limit", 500)
        self.room_name = room_name
        self.host_name = host_name
        self.max_users = max_users
        
        # Connection state
        self.clients = {}  # ws -> client_info
        
        # User details map: username -> info
        self.users = {
            host_name: {
                "name": host_name, 
                "role": "host", 
                "muted": False, 
                "deafened": False, 
                "local_muted": True, # Host starts muted locally
                "local_deafened": False,
                "online": False, 
                "ws": None, 
                "ip": "localhost",
                "last_spoke": 0
            }
        }
        
        # Playback state
        self.playback_state = {
            "url": "",
            "anime_title": "Nothing",
            "episode": "",
            "provider": "",
            "state": "closed", # playing | paused | closed
            "timestamp": 0.0,
            "last_sync": time.time()
        }
        
        # Banned users set
        self.banned_users = set()
        
        # IP to last-known-username mapping (to handle name changes on reconnection)
        self.ip_to_name = {}
        self.hash_to_name = {} # Map hash_id -> name for history resolution
        self.chat_history = [] # Rolling list of recent messages
        self.last_logged_playback = {} # To throttle sync logs

    def _save_to_history(self, msg_payload):
        """Standard method to save messages (chat or system) to history."""
        self.chat_history.append(msg_payload)
        
        # Keep history at a reasonable limit from config
        if len(self.chat_history) > self._history_limit:
            self.chat_history.pop(0)

    def _broadcast(self, message, exclude_ws=None, save_history=False, sender_hash=None):
        if save_history:
            self._save_to_history(message)
            
        # Determine if we are sending raw binary (voice) or JSON (chat/system)
        is_binary = isinstance(message, bytes)
        
        # Binary Voice Packing: Prepend sender hash for Local Muting support
        if is_binary and sender_hash:
            try:
                hash_bytes = sender_hash.encode('utf-8')
                hash_len = len(hash_bytes)
                if hash_len > 255: hash_len = 255
                # Packet = [1-byte len] + [Hash ID] + [Audio Data]
                out_msg = bytes([hash_len]) + hash_bytes[:hash_len] + message
            except:
                out_msg = message
        else:
            out_msg = message if is_binary else json.dumps(message, ensure_ascii=False)
        
        for ws in list(self.clients.keys()):
            if ws != exclude_ws:
                # Don't send chat or sync to deafened users, except admin messages
                client = self.clients.get(ws)
                if not client: continue
                
                user_info = self.users.get(client['name'])
                if not user_info: continue
                
                # Filter Logic:
                if is_binary:
                    # Don't send voice to deafened users (Global Deafen)
                    if user_info.get('deafened'): continue
                else:
                    # Don't send chat/sync to deafened users (except system alerts)
                    if user_info.get('deafened'):
                        if isinstance(message, dict) and message.get('type') in ['chat', 'sync']:
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
                "local_muted": info.get("local_muted", False),
                "local_deafened": info.get("local_deafened", False),
                "last_spoke": info.get("last_spoke", 0),
                "online": info["online"],
                "ip": info.get("ip", ""),
                "hash_id": info.get("hash_id", ""),
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

            client_ip = "unknown"
            try:
                # Support new and old websockets versions for header access
                headers = getattr(websocket, 'request_headers', None)
                if headers is None and hasattr(websocket, 'request'):
                    headers = websocket.request.headers
                
                if headers:
                    # Order of preference for proxy headers
                    potential_ips = [
                        headers.get("X-Forwarded-For"),
                        headers.get("X-Real-IP"),
                        headers.get("CF-Connecting-IP"),
                        headers.get("True-Client-IP")
                    ]
                    
                    for ip_str in potential_ips:
                        if ip_str:
                            # Take the first IP in cases of comma-separated chains
                            client_ip = ip_str.split(',')[0].strip()
                            break
                
                if client_ip == "unknown" or not client_ip:
                    client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
            except Exception as e:
                logger.error(f"IP detection error for {client_name}: {e}")

            # Generate a unique Hash ID based on IP 
            # This is stable for the duration of the connection's IP source
            hash_id = hashlib.md5(client_ip.encode()).hexdigest()[:6].upper()

            # SECURITY CHECK: Block by Hash-ID (prevents name-change evasion)
            if hash_id in self.banned_users:
                await websocket.send(
                    json.dumps({"type": "error", "message": "You are banned from this room."}, ensure_ascii=False)
                )
                await websocket.close(1008, "Banned")
                logger.warning(f"[SECURITY] Denied join from banned Hash-ID: #{hash_id} ({client_name})")
                return

            # IP-based Identity Logic: If this IP has been here before under a different name,
            # and that user is offline, rename the old record instead of creating a new one.
            old_name = self.ip_to_name.get(client_ip)
            if old_name and old_name != client_name and old_name in self.users:
                if not self.users[old_name]['online']:
                    user_info = self.users.pop(old_name)
                    user_info['name'] = client_name
                    user_info['online'] = True
                    user_info['ws'] = websocket
                    user_info['ip'] = client_ip
                    user_info['hash_id'] = hash_id
                    self.users[client_name] = user_info
                    client_name = client_name # Update local var
            
            if client_name in self.users:
                if self.users[client_name]['online'] and self.users[client_name]['ws'] != websocket:
                    await websocket.close(1008, "Username already in use")
                    return
                # Reconnecting or already online (migrated)
                self.users[client_name]['online'] = True
                self.users[client_name]['ws'] = websocket
                self.users[client_name]['ip'] = client_ip
                self.users[client_name]['hash_id'] = hash_id
            else:
                # New user
                current_online = sum(1 for u in self.users.values() if u['online'])
                if current_online >= self.max_users:
                    await websocket.close(1008, "Room full")
                    return
                    
                self.users[client_name] = {
                    "name": client_name, "role": "member", 
                    "muted": False, "deafened": False, 
                    "local_muted": True, "local_deafened": False,
                    "online": True, "ws": websocket, "ip": client_ip, 
                    "hash_id": hash_id, "last_spoke": 0
                }
            
            logger.info(f"[JOIN] {client_name} ({hash_id}) from IP {client_ip}")
            
            # Update Identity mappings
            self.ip_to_name[client_ip] = client_name
            self.hash_to_name[hash_id] = client_name
            self.clients[websocket] = {'name': client_name}
            
            # Send initial state
            await websocket.send(
                json.dumps(
                    {
                        "type": "room_state",
                        "room_name": self.room_name,
                        "host_name": self.host_name,
                        "playback": self.playback_state,
                    },
                    ensure_ascii=False,
                )
            )
            
            # Send chat history to new user
            if self.chat_history:
                # Dynamically resolve names in history in case users changed them
                resolved_history = []
                # Map hash_id -> current online/best-known name
                current_names = self.hash_to_name.copy()
                
                for msg in self.chat_history:
                    m = msg.copy()
                    if m.get("type") == "chat":
                        hid = m.get("hash_id")
                        if hid in current_names:
                            m["sender"] = current_names[hid]
                    resolved_history.append(m)
                
                await websocket.send(json.dumps({
                    "type": "chat_history",
                    "history": resolved_history
                }, ensure_ascii=False))
            
            self._broadcast({
                "type": "system", 
                "subtype": "join", 
                "actor": client_name,
                "role": self.users[client_name]['role'],
                "message": f"{client_name} joined the room.",
                "time": time.strftime("%H:%M")
            }, save_history=True)
            self._broadcast_user_list()
            
            # Message loop
            async for message in websocket:
                if isinstance(message, bytes):
                    # Relaying voice packet (binary payload)
                    u = self.users.get(client_name)
                    now = time.time()
                    
                    # Log voice activity start (cooldown of 5s between logs for the same user)
                    if u:
                        u["last_spoke"] = now
                    
                    # SERVER-SIDE MUTE CHECK: Only broadcast if user is NOT globally muted
                    if u and u.get("muted"):
                        continue
                        
                    # Include sender's unique Hash-ID so clients can perform Local Muting
                    self._broadcast(message, exclude_ws=websocket, sender_hash=u.get('hash_id'))
                    continue
                    
                try:
                    data = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                    
                msg_type = data.get("type")
                user_info = self.users[client_name]
                
                if msg_type == "chat":
                    if user_info['muted']:
                        await websocket.send(
                            json.dumps({"type": "error", "message": "You are muted."}, ensure_ascii=False)
                        )
                        continue
                    
                    chat_payload = {
                        "type": "chat",
                        "sender": client_name,
                        "hash_id": user_info.get("hash_id"),
                        "message": data.get("message", ""),
                        "time": time.strftime("%H:%M")
                    }
                    # Log full chat content
                    logger.info(f"[CHAT] {client_name} ({user_info.get('hash_id')}): {data.get('message')}")
                    
                    # Already saved via _broadcast(..., save_history=True) below
                    # No need to manually append here
                    
                    self._broadcast(chat_payload, save_history=True)
                    
                elif msg_type == "sync":
                    if user_info['role'] != 'host':
                        continue # Only host can sync
                    
                    # Update state
                    old_url = self.playback_state.get("url")
                    self.playback_state.update({
                        "url": data.get("url", self.playback_state["url"]),
                        "anime_title": data.get("anime_title", self.playback_state["anime_title"]),
                        "episode": data.get("episode", self.playback_state["episode"]),
                        "provider": data.get("provider", self.playback_state.get("provider", "")),
                        "timestamp": data.get("timestamp", self.playback_state["timestamp"]),
                        "state": data.get("state", self.playback_state["state"]),
                    })
                    
                    # Throttled Logging: Only log if state/url changes or if seek > 10s
                    should_log = False
                    if old_url != self.playback_state["url"]:
                        logger.info(f"[SYNC] New video: {self.playback_state['anime_title']} - {self.playback_state['url']}")
                        should_log = True
                    
                    last_logged = self.last_logged_playback
                    if (self.playback_state["state"] != last_logged.get("state") or
                        abs(float(self.playback_state["timestamp"]) - float(last_logged.get("timestamp", -100))) > 10.0):
                        should_log = True
                    
                    if should_log:
                        logger.info(f"[SYNC] Playback Update: {self.playback_state['state']} at {self.playback_state['timestamp']}s (by {client_name})")
                        self.last_logged_playback = self.playback_state.copy()
                    
                    # Relay to others
                    # Save history ONLY if it was a significant change (should_log)
                    self._broadcast({
                        "type": "sync",
                        "playback": self.playback_state,
                        "time": time.strftime("%H:%M"),
                        "hash_id": user_info.get("hash_id")
                    }, save_history=should_log, exclude_ws=websocket)
                    
                elif msg_type == "admin":
                    if user_info['role'] != 'host':
                        continue
                    
                    action = data.get("action")
                    target = data.get("target")
                    
                    # TARGET RESOLUTION: Search by name or Hash-ID
                    target_info = None
                    if target in self.users:
                        target_info = self.users[target]
                    else:
                        # Search for Hash-ID (case insensitive)
                        for u_name, u_info in self.users.items():
                            if u_info.get("hash_id", "").upper() == target.upper():
                                target_info = u_info
                                target = u_name # Resolve to actual username
                                break
                    
                    if not target_info or target == self.host_name:
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
                        logger.info(f"[ADMIN] {client_name} kicked {target}")
                        
                    elif action == "ban":
                        # Ban using the unique Hash-ID
                        self.banned_users.add(target_info['hash_id'])
                        if target_info['ws']:
                            await target_info['ws'].send(
                                json.dumps(
                                    {"type": "kicked", "message": "You have been banned by the host."},
                                    ensure_ascii=False,
                                )
                            )
                            await target_info['ws'].close()
                        self._broadcast({"type": "system", "message": f"{target} (#{target_info['hash_id']}) was banned."})
                        logger.info(f"[ADMIN] {client_name} banned {target} [#{target_info['hash_id']}]")
                    
                    elif action == "unban":
                        # Support unbanning by targeting a specific name if they are in the session, 
                        # or by providing the Hash-ID directly (e.g., /unban #A1B2C3)
                        h_id = target_info.get('hash_id') if target_info else (target[1:] if target.startswith('#') else target)
                        
                        if h_id in self.banned_users:
                            self.banned_users.discard(h_id)
                            self._broadcast({"type": "system", "message": f"User/ID {h_id} was unbanned."})
                            logger.info(f"[ADMIN] {client_name} unbanned {h_id}")
                        else:
                            # Send error back to host
                            for ws in self.clients:
                                if self.clients[ws]['name'] == self.host_name:
                                    asyncio.create_task(ws.send(json.dumps({
                                        "type": "error", "message": f"Hash-ID {h_id} not found in ban list."
                                    })))
                                    break
                        
                    elif action == "mute":
                        target_info['muted'] = True
                        log_msg = f"{target} has been muted by admin ({client_name})."
                        logger.info(f"[ADMIN] {log_msg}")
                        self._broadcast({"type": "system", "message": log_msg})
                        
                    elif action == "unmute":
                        target_info['muted'] = False
                        log_msg = f"{target} has been unmuted by admin ({client_name})."
                        logger.info(f"[ADMIN] {log_msg}")
                        self._broadcast({"type": "system", "message": log_msg})
                        
                    elif action == "deafen":
                        target_info['deafened'] = True
                        log_msg = f"{target} has been deafened by admin ({client_name})."
                        logger.info(f"[ADMIN] {log_msg}")
                        self._broadcast({"type": "system", "message": log_msg})

                    elif action in ["undeafen", "undefan"]:
                        target_info['deafened'] = False
                        log_msg = f"{target} has been undeafened by admin ({client_name})."
                        logger.info(f"[ADMIN] {log_msg}")
                        self._broadcast({"type": "system", "message": log_msg})
                        
                    self._broadcast_user_list()
                    
                elif msg_type == "voice_state":
                    u = self.users.get(client_name)
                    if u:
                        old_m = u.get("local_muted")
                        old_d = u.get("local_deafened")
                        u["local_muted"] = data.get("muted", False)
                        u["local_deafened"] = data.get("deafened", False)
                        
                        if old_m != u["local_muted"] or old_d != u["local_deafened"]:
                            logger.info(f"Voice state change: {client_name} (local_muted={u['local_muted']}, local_deafened={u['local_deafened']})")
                        
                        self._broadcast_user_list()
                    
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"Error handling client {client_name}: {e}")
            import traceback
            traceback.print_exc()
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
                self._broadcast({
                    "type": "system", 
                    "subtype": "leave", 
                    "actor": client_name,
                    "role": role,
                    "message": f"{client_name} left the room."
                })
                logger.info(f"[LEAVE] {client_name} ({role})")
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
from shared.utils.logger import setup_logger

# Initialize centralized logger
logger = setup_logger("party_server", "streamix_backend.log")

# Suppress noisy handshake errors from ngrok 0-byte TCP ping probes
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
logging.getLogger("websockets").setLevel(logging.CRITICAL)

async def _health_check(connection, request):
    """Handle non-WebSocket HTTP requests (ngrok health pings, browser checks, etc.)
    Return None to proceed with the normal WebSocket handshake.
    In websockets v16, return a Response to reject the upgrade."""
    # All requests proceed to WebSocket handshake
    return None

async def serve(room_name, host_name, max_users, start_port=9000):
    server_logic = WatchPartyServer(room_name, host_name, max_users)
    
    # Port Selection Logic: Try start_port, then increment if busy
    port = start_port
    max_ports_to_try = 10
    
    for attempt in range(max_ports_to_try):
        try:
            # We use the higher-level serve which handles the loop internally
            server = await websockets.serve(
                server_logic.handler,
                "0.0.0.0",
                port,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_size=2**20,
                process_request=_health_check,
            )
            logger.info(f"[LIFECYCLE] WebSocket server bound to port {port}")
            return server, port
        except OSError as e:
            if e.errno in [10048, 98, 48]: # Port in use (Win, Linux, macOS)
                logger.warning(f"[LIFECYCLE] Port {port} is busy. Trying {port + 1}...")
                port += 1
                continue
            else:
                raise e
    
    raise OSError(f"Could not find a free port in range {start_port}-{start_port + max_ports_to_try}")

def start_server_and_tunnel(room_name, host_name, max_users=10, port=9000):
    logger.info(f"Starting watch party server for room: {room_name}")
    print(f"[Party] Starting watch party: {room_name}")

    ensure_data_directories()
    
    # Aggressive cleanup of any zombie ngrok processes before starting
    import os
    if IS_WINDOWS:
        os.system("taskkill /F /IM ngrok.exe /T >nul 2>&1")
        logger.info("[LIFECYCLE] Aggressive cleanup of stale ngrok processes performed.")
    else:
        os.system("pkill -9 ngrok >/dev/null 2>&1")
        
    try:
        # Fix Python 3.10+ asyncio warning
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        install_asyncio_exception_handler(loop, logger)
        
        # 1. Start WebSocket server FIRST
        logger.info("[LIFECYCLE] WebSocket server starting...")
        print(f"[Party] Searching for an available port starting at {port}...")
        server, actual_port = loop.run_until_complete(serve(room_name, host_name, max_users, port))
        
        # 2. THEN open ngrok tunnel on the ACTUAL port found
        logger.info(f"Opening ngrok tunnel on port {actual_port}...")
        print(f"[Party] Port {actual_port} secured. Opening ngrok tunnel...")
        
        # Use http instead of tcp to avoid auth-token requirements
        tunnel = ngrok.connect(actual_port, "http")
        public_url = tunnel.public_url.replace("https://", "wss://").replace("http://", "ws://").strip()
        logger.info(f"[LIFECYCLE] ngrok tunnel opened successfully on port {actual_port}: {public_url}")
        print(f"[Party] Tunnel ready: {public_url}")
        
        # 3. Save room info for the admin client to read
        local_url = f"ws://127.0.0.1:{actual_port}"
        with open(PARTY_INFO_PATH, "w") as f:
            json.dump({
                "url": public_url,
                "local_url": local_url,
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
                if PARTY_INFO_PATH.exists():
                    PARTY_INFO_PATH.unlink()
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
    try:
        max_u = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    except ValueError:
        max_u = 10
    start_server_and_tunnel(room, host, max_users=max_u)
