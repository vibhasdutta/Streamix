#!/usr/bin/env python3
"""Launch two mpv windows and verify active synchronization via IPC."""

from __future__ import annotations

import argparse
import json
import os
import socket
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
        return rf"\\.\pipe\streamix_sync_{label}_{token}"
    return f"/tmp/streamix_sync_{label}_{token}.sock"


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


def _ipc_request(ipc_path: str, command: list, timeout: float = 1.0):
    payload = json.dumps({"command": command}) + "\n"

    if IS_WINDOWS:
        with open(ipc_path, "r+") as pipe:
            pipe.write(payload)
            pipe.flush()
            line = pipe.readline()
            return json.loads(line) if line else {}

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(ipc_path)
    sock.sendall(payload.encode("utf-8"))

    data = b""
    while b"\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()

    first_line = data.decode("utf-8", errors="ignore").split("\n", 1)[0]
    return json.loads(first_line) if first_line else {}


def _get_property(ipc_path: str, name: str):
    try:
        res = _ipc_request(ipc_path, ["get_property", name])
        return res.get("data")
    except Exception:
        return None


def _set_property(ipc_path: str, name: str, value):
    try:
        _ipc_request(ipc_path, ["set_property", name, value])
        return True
    except Exception:
        return False


def _wait_ipc_ready(ipc_path: str, proc: subprocess.Popen, timeout: float = 6.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if proc.poll() is not None:
            return False
        pause = _get_property(ipc_path, "pause")
        if isinstance(pause, bool):
            return True
        time.sleep(0.1)
    return False


def _safe_terminate(proc: subprocess.Popen):
    if not proc:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check two mpv instances and verify sync drift")
    parser.add_argument("url", help="Media URL to open in both instances")
    parser.add_argument("--sync-seconds", type=float, default=20.0, help="How long to run sync verification")
    parser.add_argument("--interval", type=float, default=0.35, help="Sync polling interval in seconds")
    parser.add_argument("--threshold", type=float, default=0.45, help="Allowed drift in seconds before correction")
    parser.add_argument("--keep-open", action="store_true", help="Keep both mpv windows open after checks")
    args = parser.parse_args()

    mpv_path = get_mpv_path()
    if not mpv_path:
        print("ERROR: mpv not found in PATH/common install locations.")
        return 2

    ipc_leader = _ipc_path("leader")
    ipc_follower = _ipc_path("follower")

    leader = subprocess.Popen(_build_cmd(mpv_path, args.url, "MPV Sync Leader", ipc_leader), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    follower = subprocess.Popen(_build_cmd(mpv_path, args.url, "MPV Sync Follower", ipc_follower), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if not _wait_ipc_ready(ipc_leader, leader):
            print("FAIL: Leader IPC not ready.")
            return 1
        if not _wait_ipc_ready(ipc_follower, follower):
            print("FAIL: Follower IPC not ready.")
            return 1

        # Start both deterministically at t=0 paused, then release together.
        _set_property(ipc_leader, "pause", True)
        _set_property(ipc_follower, "pause", True)
        _set_property(ipc_leader, "time-pos", 0.0)
        _set_property(ipc_follower, "time-pos", 0.0)
        _set_property(ipc_follower, "pause", False)
        _set_property(ipc_leader, "pause", False)

        start = time.time()
        max_drift = 0.0
        corrections = 0
        samples = 0

        while time.time() - start < args.sync_seconds:
            if leader.poll() is not None:
                print(f"FAIL: Leader exited early with code {leader.returncode}")
                return 1
            if follower.poll() is not None:
                print(f"FAIL: Follower exited early with code {follower.returncode}")
                return 1

            leader_pause = _get_property(ipc_leader, "pause")
            leader_t = _get_property(ipc_leader, "time-pos")
            follower_pause = _get_property(ipc_follower, "pause")
            follower_t = _get_property(ipc_follower, "time-pos")

            if isinstance(leader_pause, bool) and isinstance(follower_pause, bool) and leader_pause != follower_pause:
                _set_property(ipc_follower, "pause", leader_pause)

            if isinstance(leader_t, (int, float)) and isinstance(follower_t, (int, float)):
                drift = abs(float(leader_t) - float(follower_t))
                max_drift = max(max_drift, drift)
                samples += 1
                if drift > args.threshold:
                    _set_property(ipc_follower, "time-pos", float(leader_t))
                    corrections += 1

            time.sleep(args.interval)

        # Final snapshot
        leader_t = _get_property(ipc_leader, "time-pos")
        follower_t = _get_property(ipc_follower, "time-pos")
        final_drift = None
        if isinstance(leader_t, (int, float)) and isinstance(follower_t, (int, float)):
            final_drift = abs(float(leader_t) - float(follower_t))
            max_drift = max(max_drift, final_drift)

        print(f"SYNC RESULT: samples={samples}, corrections={corrections}, max_drift={max_drift:.3f}s")
        if final_drift is not None:
            print(f"SYNC RESULT: final_drift={final_drift:.3f}s")

        passed = (samples > 0) and (max_drift <= max(args.threshold * 2.0, 0.9))
        if passed:
            print("PASS: Dual mpv synchronization is working.")
            return 0

        print("FAIL: Drift remained too high during sync window.")
        return 1
    finally:
        if not args.keep_open:
            _safe_terminate(leader)
            _safe_terminate(follower)


if __name__ == "__main__":
    raise SystemExit(main())
