import shutil
import os

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
    # Dynamic Referrer Logic
    referrer = "https://miruro.to/" # Default
    if "kwik.cx" in url:
        referrer = "https://kwik.cx/"
    elif provider and provider.lower() == "kiwi":
        referrer = "https://kwik.cx/"
    elif "bunny.net" in url:
        referrer = "https://miruro.to/"
        
    headers = [
        f"--referrer={referrer}",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        # Standard browser origin
        "--http-header-fields=Origin: https://miruro.to"
    ]
    
    # Global optimizations for online streams
    optimizations = [
        "--tls-verify=no",           # Skip verification if SNI/Certs are mismatched
        "--cache-secs=60",           # Cache up to 60s ahead
        "--hls-bitrate=max",         # Always aim for best HLS quality
    ]
    return headers + optimizations
