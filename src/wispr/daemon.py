from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio import default_input_device, microphone_source
from .config import Settings
from .gemini import GeminiLiveTranscriber
from .output import TextOutput, notify, start_notifications

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"


def token_path(settings: Settings) -> Path:
    return settings.state_dir / "control-token"


class WisprDaemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = "idle"
        self.interim = ""
        self.final = ""
        self.last_error = ""
        self._stop_recording = asyncio.Event()
        self._cancelled = False
        self._session_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._token = ""
        self._hotkey_tasks: set[asyncio.Task[dict[str, Any]]] = set()

    async def serve(self) -> None:
        if WINDOWS:
            await self._serve_windows()
            return

        socket_path = self.settings.socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()

        server = await asyncio.start_unix_server(self._handle_client, path=socket_path)
        socket_path.chmod(0o600)
        log.info(
            "Daemon listening on %s (model %s, debug %s)",
            socket_path,
            self.settings.model,
            self.settings.debug,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown.set)

        try:
            async with server:
                await self._shutdown.wait()
        finally:
            await self._finish_session()
            socket_path.unlink(missing_ok=True)

    async def _serve_windows(self) -> None:
        # No Unix sockets: listen on loopback and require a token that only
        # this Windows user can read from their profile.
        # On a restart, the previous instance may still be shutting down.
        for attempt in range(20):
            try:
                server = await asyncio.start_server(
                    self._handle_client, "127.0.0.1", self.settings.control_port
                )
                break
            except OSError as error:
                if attempt == 19:
                    raise RuntimeError(
                        f"Port {self.settings.control_port} is busy; is another Wispr "
                        "daemon running? (set WISPR_CONTROL_PORT to change it)"
                    ) from error
                await asyncio.sleep(0.5)

        self._token = secrets.token_hex(16)
        token_file = token_path(self.settings)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(self._token, encoding="utf-8")

        hotkey = None
        if self.settings.hotkey:
            from .windows import HotkeyListener

            loop = asyncio.get_running_loop()
            hotkey = HotkeyListener(
                self.settings.hotkey, lambda: loop.call_soon_threadsafe(self._on_hotkey)
            )
            hotkey.start()
        await start_notifications()
        log.info(
            "Daemon listening on 127.0.0.1:%s (hotkey %s, model %s, debug %s)",
            self.settings.control_port,
            self.settings.hotkey or "disabled",
            self.settings.model,
            self.settings.debug,
        )

        try:
            async with server:
                await self._shutdown.wait()
        finally:
            if hotkey:
                hotkey.stop()
            await self._finish_session()
            token_file.unlink(missing_ok=True)

    def _on_hotkey(self) -> None:
        task = asyncio.create_task(self._command("toggle"))
        self._hotkey_tasks.add(task)
        task.add_done_callback(self._hotkey_tasks.discard)

    async def _finish_session(self) -> None:
        if self._session_task and not self._session_task.done():
            self._cancelled = True
            self._stop_recording.set()
            await self._session_task

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2)
            request = json.loads(raw.decode("utf-8"))
            if self._token and not hmac.compare_digest(
                str(request.get("token", "")), self._token
            ):
                raise PermissionError("Invalid control token")
            response = await self._command(str(request.get("command", "status")))
        except Exception as error:
            response = {"ok": False, "error": str(error)}
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _command(self, command: str) -> dict[str, Any]:
        log.info("Received command %r in state %r", command, self.state)
        if command == "toggle":
            command = "stop" if self.state == "recording" else "start"

        if command == "start":
            if self.state != "idle":
                return {"ok": False, "state": self.state, "error": "Already active"}
            self._start_recording()
            return {"ok": True, "state": self.state}

        if command == "stop":
            if self.state != "recording":
                return {"ok": False, "state": self.state, "error": "Not recording"}
            self.state = "processing"
            self._stop_recording.set()
            await notify("Processing dictation…")
            return {"ok": True, "state": self.state}

        if command == "cancel":
            if self.state not in {"recording", "processing"}:
                return {"ok": False, "state": self.state, "error": "Nothing to cancel"}
            self._cancelled = True
            self._stop_recording.set()
            return {"ok": True, "state": "cancelling"}

        if command == "quit":
            self._shutdown.set()
            return {"ok": True, "state": "stopping"}

        if command == "status":
            return self._status()

        return {"ok": False, "error": f"Unknown command: {command}"}

    def _start_recording(self) -> None:
        self.state = "recording"
        self.interim = ""
        self.final = ""
        self.last_error = ""
        self._cancelled = False
        self._stop_recording = asyncio.Event()
        self._session_task = asyncio.create_task(self._run_session())

    async def _run_session(self) -> None:
        await notify("Listening…", "Press the dictation shortcut again to stop")

        async def update_interim(text: str) -> None:
            self.interim = text

        async def update_final(text: str) -> None:
            self.final = text

        record_path = None
        if self.settings.recordings_dir:
            name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".wav"
            record_path = self.settings.recordings_dir / name

        try:
            log.info("Session starting; WISPR_AUDIO_SOURCE=%s", self.settings.audio_source)
            if self.settings.debug:
                # Querying the default device is slow; don't delay recording for it.
                log.debug("System default source: %s", default_input_device())
            transcriber = GeminiLiveTranscriber(self.settings)
            audio = microphone_source(self.settings.audio_source, record_path)
            transcript = await transcriber.transcribe(
                audio,
                self._stop_recording,
                on_interim=update_interim,
                on_final=update_final,
            )
            if self._cancelled:
                await notify("Dictation cancelled")
            elif transcript:
                result = await TextOutput(self.settings.output_mode).deliver(transcript)
                await notify("Dictation ready", result.message)
            else:
                log.warning("No speech detected; %s", audio.level_summary())
                await notify("No speech detected")
        except Exception as error:
            self.last_error = str(error).replace(self.settings.api_key, "[REDACTED]")
            log.error("Session failed: %s", self.last_error)
            await notify("Dictation failed", self.last_error[:240])
        finally:
            self.state = "idle"
            self.interim = ""
            log.info("Session finished")

    def _status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "state": self.state,
            "interim": self.interim,
            "final": self.final,
            "last_error": self.last_error,
        }


async def send_command(settings: Settings, command: str) -> dict[str, Any]:
    request: dict[str, str] = {"command": command}
    try:
        if WINDOWS:
            request["token"] = token_path(settings).read_text(encoding="utf-8").strip()
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", settings.control_port
            )
        else:
            reader, writer = await asyncio.open_unix_connection(settings.socket_path)
    except OSError as error:
        raise RuntimeError(
            "Wispr daemon is not running. Start it with `wispr daemon`."
        ) from error

    writer.write((json.dumps(request) + "\n").encode("utf-8"))
    await writer.drain()
    response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()
    return response

