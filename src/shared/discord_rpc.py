import time
import threading
import queue
import os
from urllib.parse import urlparse
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
            # whether to include external/stream-derived metadata (covers, thumbnails)
            self.include_stream_metadata = cfg.get("admin", {}).get("discord_rpc_include_streaming_metadata", True)
        except Exception:
            self.enabled = True

        if self.enabled:
            self._connect_thread = threading.Thread(target=self._connection_loop, daemon=True)
            self._connect_thread.start()
        # small in-memory cache for fetched media metadata (title, thumbnail)
        self._meta_cache = {}  # url -> (timestamp, {"title":..., "thumbnail":...})

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

    def _fetch_media_metadata(self, url):
        """Try to fetch a human-friendly title and thumbnail for common stream URLs.

        Returns a dict with keys 'title' and 'thumbnail' or None on failure.
        Caches results for 1 hour.
        """
        if not url or not isinstance(url, str):
            return None
        now = time.time()
        cached = self._meta_cache.get(url)
        if cached and now - cached[0] < 3600:
            return cached[1]

        try:
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            # Try provider oEmbed endpoints for quick title extraction
            import requests
            meta = None
            if 'youtube.com' in host or 'youtu.be' in host:
                oe = f"https://www.youtube.com/oembed?url={requests.utils.requote_uri(url)}&format=json"
                r = requests.get(oe, timeout=2)
                if r.status_code == 200:
                    j = r.json()
                    meta = {"title": j.get('title'), "thumbnail": j.get('thumbnail_url')}
            elif 'vimeo.com' in host:
                oe = f"https://vimeo.com/api/oembed.json?url={requests.utils.requote_uri(url)}"
                r = requests.get(oe, timeout=2)
                if r.status_code == 200:
                    j = r.json()
                    meta = {"title": j.get('title'), "thumbnail": j.get('thumbnail_url')}

            # Generic fallback: fetch HTML and look for og:title / og:image
            if not meta:
                r = requests.get(url, timeout=2, headers={"User-Agent": "streamix/1.0 (+https://example)"})
                if r.status_code == 200 and r.text:
                    html = r.text
                    import re
                    def og(tag):
                        m = re.search(rf'<meta[^>]+property=["\']og:{tag}["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
                        if m:
                            return m.group(1)
                        m = re.search(rf'<meta[^>]+name=["\']{tag}["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
                        if m:
                            return m.group(1)
                        return None
                    title = og('title') or og('site_name')
                    thumb = og('image')
                    if title or thumb:
                        meta = {"title": title, "thumbnail": thumb}

            if meta:
                self._meta_cache[url] = (now, meta)
                return meta
        except Exception:
            pass
        return None

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

    def set_watching_solo(self, title, episode="1", total_eps=None, runtime_pos=None, runtime_duration=None, anime_meta=None, media_url=None):
        # Determine the state label: prefer episode info when available,
        # but for stream URLs omit empty episode labels and show host/domain.
        if episode and str(episode).strip():
            ep_label = f"Ep {episode} / {total_eps}" if total_eps else f"Ep {episode}"
        else:
            if media_url and isinstance(media_url, str) and media_url.startswith("http"):
                try:
                    netloc = urlparse(media_url).netloc or "Streaming"
                    ep_label = netloc
                except Exception:
                    ep_label = "Streaming"
            else:
                ep_label = ""
        cover = (anime_meta or {}).get("cover_url") or "icon_large"
        # Respect admin setting: avoid sending external stream metadata if disabled
        if media_url and isinstance(media_url, str):
            try:
                if media_url.startswith("http") and not self.include_stream_metadata:
                    cover = "icon_large"
                elif os.path.exists(media_url) and not anime_meta:
                    # If playing a local file and no anime metadata available,
                    # prefer using the parent folder name as the large_text so
                    # it shows a meaningful title when thumbnails are missing.
                    folder = os.path.basename(os.path.dirname(media_url))
                    if folder:
                        large_text = folder[:128]
                    else:
                        large_text = self._build_large_text(anime_meta)
                else:
                    large_text = self._build_large_text(anime_meta)
            except Exception:
                large_text = self._build_large_text(anime_meta)
        else:
            large_text = self._build_large_text(anime_meta)

        start_ts = (int(time.time()) - int(runtime_pos)) if runtime_pos is not None else self._session_start
        # If ep_label is empty, don't include the parenthetical suffix.
        state_text = f"{ep_label} (Solo)" if ep_label else "Solo"

        # If we don't have anime metadata, try to fetch a stream title to show in
        # the presence `details` (useful for YouTube links). This is cached.
        details_text = f"Watching {title}"
        if not anime_meta and media_url and self.include_stream_metadata:
            try:
                md = self._fetch_media_metadata(media_url)
                if md and md.get('title'):
                    details_text = md.get('title')
                    # If we fetched a thumbnail and the CLI has an asset matching
                    # that thumbnail key, developers can map it; otherwise Discord
                    # will show the default image. We still send the thumbnail key
                    # as `large_image` in case it's an asset name.
                    if md.get('thumbnail'):
                        cover = md.get('thumbnail')
            except Exception:
                pass

        self.update_presence(
            details=details_text,
            state=state_text,
            large_image=cover,
            large_text=large_text,
            small_image="icon_play",
            small_text="Playing",
            start=start_ts,
        )

    def set_watching_party(self, title, episode="1", total_eps=None, party_name="A Party", member_count=1, party_max=10, runtime_pos=None, runtime_duration=None, anime_meta=None, host_name=None, media_url=None):
        if episode and str(episode).strip():
            ep_label = f"Ep {episode} / {total_eps}" if total_eps else f"Ep {episode}"
        else:
            if media_url and isinstance(media_url, str) and media_url.startswith("http"):
                try:
                    netloc = urlparse(media_url).netloc or "Streaming"
                    ep_label = netloc
                except Exception:
                    ep_label = "Streaming"
            else:
                ep_label = ""
        cover = (anime_meta or {}).get("cover_url") or "icon_large"
        # Respect admin setting for external metadata
        if media_url and isinstance(media_url, str):
            try:
                if media_url.startswith("http") and not self.include_stream_metadata:
                    cover = "icon_large"
            except Exception:
                pass
        start_ts = (int(time.time()) - int(runtime_pos)) if runtime_pos is not None else self._session_start
        small = f"{party_name} · Host: {host_name}" if host_name else party_name
        state_text = ep_label or "Watching"

        details_text = f"Watching {title}"
        if not anime_meta and media_url and self.include_stream_metadata:
            try:
                md = self._fetch_media_metadata(media_url)
                if md and md.get('title'):
                    details_text = md.get('title')
                    if md.get('thumbnail'):
                        cover = md.get('thumbnail')
            except Exception:
                pass

        self.update_presence(
            details=details_text,
            state=state_text,
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
