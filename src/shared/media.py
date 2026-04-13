import shutil
import os


def is_network_media_url(url):
    """Return True for network-playable URLs and False for local file paths."""
    if not url:
        return False
    lower = str(url).strip().lower()
    return lower.startswith(("http://", "https://", "rtmp://", "rtsp://", "m3u8://", "ytdl://"))

def get_mpv_path():
    """Find mpv on the system (Linux/macOS)."""
    cmd = shutil.which("mpv")
    if cmd:
        return cmd
    possible_paths = [
        # Local relative path
        "./mpv.exe",
        "bin/mpv.exe",
        # Linux/macOS
        "/usr/bin/mpv",
        "/usr/local/bin/mpv",
        "/snap/bin/mpv",
        "/opt/homebrew/bin/mpv",
        # Windows common paths
        "C:\\Program Files\\mpv\\mpv.exe",
        "C:\\Program Files\\MPV Player\\mpv.exe",
        "C:\\mpv\\mpv.exe",
        os.path.expanduser("~\\AppData\\Local\\Microsoft\\WindowsApps\\mpv.exe")
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None

def get_streaming_headers(url, provider=None):
    """Generate optimal HTTP headers for a given stream URL/Provider."""
    if not is_network_media_url(url):
        return []

    lower_url = str(url).lower()

    # Only inject site headers for providers/domains that require anti-hotlink values.
    provider_name = (provider or "").lower()
    needs_site_headers = any(
        token in lower_url
        for token in ("kwik.cx", "bunny.net", "miruro", "anify", "anime")
    ) or provider_name in {"kiwi", "kwik", "miruro"}

    headers = []

    # Dynamic Referrer Logic
    referrer = "https://miruro.to/"
    origin = "https://miruro.to"
    if "kwik.cx" in lower_url:
        referrer = "https://kwik.cx/"
        origin = "https://kwik.cx"
    elif provider_name == "kiwi":
        referrer = "https://kwik.cx/"
        origin = "https://kwik.cx"
    elif "bunny.net" in lower_url:
        referrer = "https://miruro.to/"

    # Keep a generic browser UA for robust CDN compatibility.
    headers.append(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )

    if needs_site_headers:
        headers.extend(
            [
                f"--referrer={referrer}",
                f"--http-header-fields=Origin: {origin}",
                "--tls-verify=no",
            ]
        )

    # Global online-stream optimizations that are safe across providers.
    optimizations = [
        "--cache-secs=60",
        "--hls-bitrate=max",
    ]
    return headers + optimizations
