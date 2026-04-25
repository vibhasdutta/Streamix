import time
import threading
import queue
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "1494020414173872389")

class DiscordRPCManager:
    """Manages Discord Rich Presence for Streamix."""

    def __init__(self):
        self.rpc = None
        self.enabled = False
        self.connected = False
        self.last_update = 0
        self.current_state = None
        self.update_queue = queue.Queue()
        self.lock = threading.Lock()
        self._session_start = int(time.time())

        try:
            from core.config import load_config
            cfg = load_config()
            self.enabled = cfg.get("admin", {}).get("discord_rpc", True)
        except Exception:
            self.enabled = True

        if self.enabled:
            self._connect_thread = threading.Thread(target=self._connection_loop, daemon=True)
            self._connect_thread.start()

    def _connection_loop(self):
        try:
            from pypresence import Presence
        except ImportError:
            return

        last_rpc_call = 0
        pending_state = None
        last_sent_state = None  # Track what was last pushed to Discord

        while self.enabled:
            if not self.connected:
                try:
                    self.rpc = Presence(DISCORD_CLIENT_ID)
                    self.rpc.connect()
                    self.connected = True
                    last_rpc_call = 0  # allow immediate update on reconnect
                    last_sent_state = None  # force refresh on reconnect
                    with self.lock:
                        if self.current_state:
                            pending_state = self.current_state
                except Exception:
                    if self.rpc:
                        try: self.rpc.close()
                        except: pass
                    self.connected = False
                    self.rpc = None
                    time.sleep(15)
                    continue

            # Drain latest state from queue (keep only newest, discard stale)
            try:
                while True:
                    pending_state = self.update_queue.get_nowait()
            except queue.Empty:
                pass

            if pending_state and self.connected and self.rpc:
                now = time.time()

                # Detect if this is a genuine state change vs. a same-state refresh.
                # State changes (e.g. "Watching Anime" → "Browsing") use a shorter
                # cooldown so Discord presence feels responsive on transitions.
                is_state_change = (
                    not last_sent_state
                    or pending_state.get("clear_rpc_signal")
                    or last_sent_state.get("clear_rpc_signal")
                    or last_sent_state.get("state") != pending_state.get("state")
                    or last_sent_state.get("details") != pending_state.get("details")
                    or last_sent_state.get("large_image") != pending_state.get("large_image")
                )

                cooldown = 5.0 if is_state_change else 15.1
                wait = cooldown - (now - last_rpc_call)
                if wait > 0:
                    time.sleep(wait)

                try:
                    if pending_state.get("clear_rpc_signal"):
                        self.rpc.clear()
                        last_sent_state = {"clear_rpc_signal": True}
                    else:
                        kwargs = {k: v for k, v in pending_state.items() if v is not None}
                        self.rpc.update(**kwargs)
                        last_sent_state = dict(pending_state)
                    last_rpc_call = time.time()
                    pending_state = None
                except Exception:
                    try: self.rpc.close()
                    except: pass
                    self.connected = False
                    self.rpc = None
            else:
                time.sleep(1)

    def update_presence(self, state=None, details=None, start=None, large_image=None, large_text=None, small_image=None, small_text=None, buttons=None, party_id=None, party_size=None):
        if not self.enabled:
            return

        with self.lock:
            now = time.time()

            effective_image = large_image or "icon_large"
            is_new_state = (not self.current_state or
                            self.current_state.get('state') != state or
                            self.current_state.get('details') != details or
                            self.current_state.get('large_image') != effective_image)

            if not is_new_state and (now - self.last_update < 15):
                return

            self.current_state = {
                "state": state,
                "details": details,
                "start": start,
                "large_image": large_image or "icon_large",
                "large_text": large_text or "Streamix",
                "small_image": small_image,
                "small_text": small_text,
                "buttons": buttons,
                "party_id": party_id,
                "party_size": party_size,
            }
            self.last_update = now
            self.update_queue.put(self.current_state)

    def clear_presence(self):
        self.current_state = None
        if self.connected and self.rpc:
            try:
                self.update_queue.put({"clear_rpc_signal": True})
            except Exception:
                pass

    def _build_large_text(self, anime_meta):
        if not anime_meta:
            return "Streamix"
        parts = []
        score = anime_meta.get("score")
        if score:
            parts.append(f"\u2b50 {score / 10:.1f}")
        genres = anime_meta.get("genres") or []
        if genres:
            parts.append(", ".join(genres))
        studio = anime_meta.get("studio")
        if studio:
            parts.append(studio)
        text = " \u00b7 ".join(parts) if parts else "Streamix"
        return text[:128]

    def _format_timer(self, pos, dur):
        if pos is None or dur is None or dur <= 0 or pos < 0:
            return None
        def fmt(s):
            s = int(s)
            return f"{s // 60:02d}:{s % 60:02d}"
        return f"{fmt(pos)} / {fmt(dur)}"

    def set_browsing(self):
        self.update_presence(
            details="Browsing Anime",
            state="In Menus",
            start=self._session_start,
            large_image="icon_large"
        )

    def set_in_party(self, room_name, member_count=1, party_max=10, host_name=None):
        state = f"{room_name} · Host: {host_name}" if host_name else room_name
        self.update_presence(
            details="In Watch Party",
            state=state[:128],
            large_image="icon_large",
            large_text=room_name,
            small_image="icon_party",
            small_text="Watch Party",
            start=self._session_start,
            party_id=room_name,
            party_size=[max(member_count, 1), party_max],
        )

    def set_watching_solo(self, title, episode="1", total_eps=None, runtime_pos=None, runtime_duration=None, anime_meta=None):
        ep_label = f"Ep {episode} / {total_eps}" if total_eps else f"Ep {episode}"
        cover = (anime_meta or {}).get("cover_url") or "icon_large"
        start_ts = (int(time.time()) - int(runtime_pos)) if runtime_pos is not None else self._session_start
        self.update_presence(
            details=f"Watching {title}",
            state=f"{ep_label} (Solo)",
            large_image=cover,
            large_text=self._build_large_text(anime_meta),
            small_image="icon_play",
            small_text="Playing",
            start=start_ts,
        )

    def set_watching_party(self, title, episode="1", total_eps=None, party_name="A Party", member_count=1, party_max=10, runtime_pos=None, runtime_duration=None, anime_meta=None, host_name=None):
        ep_label = f"Ep {episode} / {total_eps}" if total_eps else f"Ep {episode}"
        cover = (anime_meta or {}).get("cover_url") or "icon_large"
        start_ts = (int(time.time()) - int(runtime_pos)) if runtime_pos is not None else self._session_start
        small = f"{party_name} · Host: {host_name}" if host_name else party_name
        self.update_presence(
            details=f"Watching {title}",
            state=ep_label,
            large_image=cover,
            large_text=self._build_large_text(anime_meta),
            small_image="icon_party",
            small_text=small[:128],
            start=start_ts,
            party_id=party_name,
            party_size=[max(member_count, 1), party_max],
        )

# Global instance
rpc_manager = DiscordRPCManager()
