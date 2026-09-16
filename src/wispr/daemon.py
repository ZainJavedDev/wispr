from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio import AudioProcess, microphone_command
from .config import Settings
from .gemini import GeminiLiveTranscriber
from .output import TextOutput, notify

log = logging.getLogger(__name__)


def default_audio_source() -> str:
    if not shutil.which("pactl"):
        return "unknown (pactl not installed)"
    result = subprocess.run(
        ["pactl", "get-default-source"], capture_output=True, text=True, timeout=2
    )
    return result.stdout.strip() or result.stderr.strip()


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

    async def serve(self) -> None:
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
            if self._session_task and not self._session_task.done():
                self._cancelled = True
                self._stop_recording.set()
                await self._session_task
            socket_path.unlink(missing_ok=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2)
            request = json.loads(raw.decode("utf-8"))
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
            log.info(
                "Session starting; WISPR_AUDIO_SOURCE=%s, system default source=%s",
                self.settings.audio_source,
                default_audio_source(),
            )
            transcriber = GeminiLiveTranscriber(self.settings)
            audio = AudioProcess(
                microphone_command(self.settings.audio_source), record_path
            )
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


async def send_command(socket_path: Path, command: str) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except OSError as error:
        raise RuntimeError(
            "Wispr daemon is not running. Start it with `wispr daemon`."
        ) from error

    writer.write((json.dumps({"command": command}) + "\n").encode("utf-8"))
    await writer.drain()
    response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()
    return response

