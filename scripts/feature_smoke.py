from __future__ import annotations

import asyncio
import importlib
import socket
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DummyVoiceManager:
    def __init__(self, loop, input_device=None):
        self.loop = loop
        self.input_device = input_device
        self.on_voice_packet = None
        self.mic_muted = False
        self.speaker_muted = False
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def handle_incoming_audio(self, data):
        return None


async def _wait_for(predicate, timeout: float = 10.0, step: float = 0.1):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise TimeoutError("Timed out waiting for condition")


async def run_watch_party_smoke() -> None:
    print("STAGE watch_party_smoke: importing modules")
    party = importlib.import_module("features.watch_party.party")
    host_mod = importlib.import_module("features.watch_party.host")
    client_mod = importlib.import_module("features.watch_party.client")

    print("STAGE watch_party_smoke: monkeypatching runtime dependencies")
    party.os._exit = lambda code=0: None  # type: ignore[attr-defined]
    host_mod.VoiceManager = DummyVoiceManager
    client_mod.VoiceManager = DummyVoiceManager

    start_port = _get_free_port()
    server = None
    host_task = None
    client_task = None
    host = None
    client = None

    try:
        print(f"STAGE watch_party_smoke: starting local server on port {start_port}")
        server, actual_port = await party.serve("Smoke Room", "SmokeHost", 5, start_port=start_port)
        ws_url = f"ws://127.0.0.1:{actual_port}"
        print(f"STAGE watch_party_smoke: server ready on {ws_url}")

        host = host_mod.PartyAdminTUI(username="SmokeHost", ws_url=ws_url)
        client = client_mod.PartyClient(ws_url=ws_url, username="SmokeClient")

        print("STAGE watch_party_smoke: launching host/client tasks")
        host_task = asyncio.create_task(host.connect_and_listen())
        client_task = asyncio.create_task(client.connect_and_listen())

        print("STAGE watch_party_smoke: waiting for connections")
        await _wait_for(lambda: host.ws is not None and client.ws is not None, timeout=12.0)
        await _wait_for(lambda: host.voice_manager is not None and client.voice_manager is not None, timeout=12.0)

        print("STAGE watch_party_smoke: closing client connection")
        if client.ws is not None:
            await client.ws.close()

        print("STAGE watch_party_smoke: waiting for tasks to finish")
        await asyncio.wait_for(client_task, timeout=12.0)
        await _wait_for(lambda: host.running is True and host.ws is not None, timeout=5.0)

        assert host.running is True, "Host should stay active after client closes"
        assert client.running is False, "Client did not stop after client close"
        print("OK watch_party_smoke")
    finally:
        if client is not None and getattr(client, "ws", None) is not None:
            try:
                await client.ws.close()
            except Exception:
                pass
        if host is not None and getattr(host, "ws", None) is not None:
            try:
                await host.ws.close()
            except Exception:
                pass
        if host_task is not None and not host_task.done():
            host_task.cancel()
            try:
                await host_task
            except Exception:
                pass
        if client_task is not None and not client_task.done():
            client_task.cancel()
            try:
                await client_task
            except Exception:
                pass
        if server is not None:
            server.close()
            await server.wait_closed()


async def run_host_shutdown_smoke() -> None:
    print("STAGE host_shutdown_smoke: importing modules")
    party = importlib.import_module("features.watch_party.party")
    host_mod = importlib.import_module("features.watch_party.host")
    client_mod = importlib.import_module("features.watch_party.client")

    print("STAGE host_shutdown_smoke: monkeypatching runtime dependencies")
    party.os._exit = lambda code=0: None  # type: ignore[attr-defined]
    host_mod.VoiceManager = DummyVoiceManager
    client_mod.VoiceManager = DummyVoiceManager

    start_port = _get_free_port()
    server = None
    host_task = None
    client_task = None
    host = None
    client = None

    try:
        print(f"STAGE host_shutdown_smoke: starting local server on port {start_port}")
        server, actual_port = await party.serve("Smoke Room", "SmokeHost", 5, start_port=start_port)
        ws_url = f"ws://127.0.0.1:{actual_port}"

        host = host_mod.PartyAdminTUI(username="SmokeHost", ws_url=ws_url)
        client = client_mod.PartyClient(ws_url=ws_url, username="SmokeClient")

        print("STAGE host_shutdown_smoke: launching host/client tasks")
        host_task = asyncio.create_task(host.connect_and_listen())
        client_task = asyncio.create_task(client.connect_and_listen())

        print("STAGE host_shutdown_smoke: waiting for connections")
        await _wait_for(lambda: host.ws is not None and client.ws is not None, timeout=12.0)

        print("STAGE host_shutdown_smoke: closing host connection")
        if host.ws is not None:
            await host.ws.close()

        print("STAGE host_shutdown_smoke: waiting for client shutdown")
        await asyncio.wait_for(client_task, timeout=12.0)
        await asyncio.wait_for(host_task, timeout=12.0)

        assert host.running is False, "Host did not stop after host close"
        assert client.running is False, "Client did not stop when host closed"
        print("OK host_shutdown_smoke")
    finally:
        if client is not None and getattr(client, "ws", None) is not None:
            try:
                await client.ws.close()
            except Exception:
                pass
        if host is not None and getattr(host, "ws", None) is not None:
            try:
                await host.ws.close()
            except Exception:
                pass
        if host_task is not None and not host_task.done():
            host_task.cancel()
            try:
                await host_task
            except Exception:
                pass
        if client_task is not None and not client_task.done():
            client_task.cancel()
            try:
                await client_task
            except Exception:
                pass
        if server is not None:
            server.close()
            await server.wait_closed()


async def main() -> int:
    modules = [
        "core.paths",
        "core.config",
        "shared.utils.logger",
        "shared.utils.os_detector",
        "shared.media",
        "features.api_backend.backend",
        "features.voice_chat.voice_manager",
        "features.watch_party.party_input",
        "features.watch_party.party",
        "features.watch_party.host",
        "features.watch_party.client",
        "main",
    ]

    for name in modules:
        importlib.import_module(name)
        print(f"OK import {name}")

    await run_watch_party_smoke()
    await run_host_shutdown_smoke()
    print("OK all smoke checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        raise
