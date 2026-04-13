#!/usr/bin/env python3
"""Launch one mpv instance and verify it stays alive for a short probe window."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from shared.media import get_mpv_path  # noqa: E402
from shared.utils.os_detector import IS_WINDOWS  # noqa: E402


def _ipc_path(label: str) -> str:
    token = uuid.uuid4().hex[:10]
    if IS_WINDOWS:
        return rf"\\.\pipe\streamix_probe_{label}_{token}"
    return f"/tmp/streamix_probe_{label}_{token}.sock"


def _build_cmd(mpv_path: str, url: str, title: str, ipc_path: str) -> list[str]:
    return [
        mpv_path,
        f"--title={title}",
        "--force-window=yes",
        "--ytdl=yes",
        f"--input-ipc-server={ipc_path}",
        "--profile=low-latency",
        url,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe one mpv playback instance")
    parser.add_argument("url", help="Media URL (YouTube link works if mpv supports ytdl)")
    parser.add_argument("--label", default="A", help="Label used in the window title")
    parser.add_argument("--probe-seconds", type=float, default=8.0, help="How long to verify process stays alive")
    parser.add_argument("--keep-open", action="store_true", help="Do not terminate mpv after probe")
    args = parser.parse_args()

    mpv_path = get_mpv_path()
    if not mpv_path:
        print("ERROR: mpv not found in PATH/common install locations.")
        return 2

    ipc = _ipc_path(args.label)
    cmd = _build_cmd(mpv_path, args.url, f"MPV Probe {args.label}", ipc)

    print(f"Launching [{args.label}] with: {mpv_path}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    start = time.time()
    while time.time() - start < args.probe_seconds:
        if proc.poll() is not None:
            print(f"FAIL [{args.label}]: mpv exited early with code {proc.returncode}")
            return 1
        time.sleep(0.2)

    print(f"PASS [{args.label}]: mpv stayed alive for {args.probe_seconds:.1f}s")

    if not args.keep_open and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
