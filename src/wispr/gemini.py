from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .audio import AudioProcess
from .config import Settings


GEMINI_LIVE_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

log = logging.getLogger(__name__)

TranscriptCallback = Callable[[str], Awaitable[None]]


class GeminiTranscriptionError(RuntimeError):
    pass


def merge_transcript(existing: str, incoming: str) -> str:
    existing = existing.strip()
    incoming = incoming.strip()
    if not incoming:
        return existing
    if not existing:
        return incoming
    if incoming == existing or existing.endswith(incoming):
        return existing
    if incoming.startswith(existing):
        return incoming
    return f"{existing} {incoming}"


def _text_at(value: Any, *keys: str) -> str:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current.strip() if isinstance(current, str) else ""


class GeminiLiveTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def _uri(self) -> str:
        return f"{GEMINI_LIVE_ENDPOINT}?key={self.settings.api_key}"

    def _safe_error(self, error: BaseException) -> str:
        return str(error).replace(self.settings.api_key, "[REDACTED]")

    def _setup_message(self) -> dict[str, Any]:
        return {
            "setup": {
                "model": f"models/{self.settings.model}",
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": {
                    "languageCodes": list(self.settings.language_codes),
                    "mode": self.settings.transcription_mode,
                },
            }
        }

    async def check_connection(self) -> None:
        import websockets

        try:
            async with websockets.connect(
                self._uri, open_timeout=15, close_timeout=3, max_size=2**22
            ) as websocket:
                await websocket.send(json.dumps(self._setup_message()))
                response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=15))
                if "setupComplete" not in response:
                    raise GeminiTranscriptionError(self._response_error(response))
        except GeminiTranscriptionError:
            raise
        except Exception as error:
            raise GeminiTranscriptionError(self._safe_error(error)) from error

    async def transcribe(
        self,
        audio: AudioProcess,
        stop_event: asyncio.Event,
        *,
        stop_on_eof: bool = False,
        on_interim: TranscriptCallback | None = None,
        on_final: TranscriptCallback | None = None,
    ) -> str:
        import websockets

        final_text = ""
        interim_text = ""
        audio_finished = asyncio.Event()

        try:
            async with websockets.connect(
                self._uri,
                open_timeout=15,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**22,
            ) as websocket:
                log.info("Connected to Gemini; setup: %s", json.dumps(self._setup_message()))
                await websocket.send(json.dumps(self._setup_message()))
                first = json.loads(await asyncio.wait_for(websocket.recv(), timeout=15))
                log.info("Gemini setup response: %s", first)
                if "setupComplete" not in first:
                    raise GeminiTranscriptionError(self._response_error(first))

                await audio.start()

                chunks_sent = 0

                async def send_audio() -> None:
                    nonlocal chunks_sent
                    started = asyncio.get_running_loop().time()
                    try:
                        while not stop_event.is_set():
                            if (
                                asyncio.get_running_loop().time() - started
                                >= self.settings.max_duration_seconds
                            ):
                                log.warning("Max duration reached, stopping audio")
                                stop_event.set()
                                break
                            chunk = await audio.read_chunk()
                            if not chunk:
                                log.info("Audio source returned EOF")
                                if stop_on_eof:
                                    stop_event.set()
                                break
                            payload = {
                                "realtimeInput": {
                                    "audio": {
                                        "data": base64.b64encode(chunk).decode("ascii"),
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }
                            await websocket.send(json.dumps(payload))
                            chunks_sent += 1
                    finally:
                        log.info(
                            "Audio sending finished: %s chunks sent (stop requested: %s)",
                            chunks_sent,
                            stop_event.is_set(),
                        )
                        await audio.stop()
                        await websocket.send(
                            json.dumps({"realtimeInput": {"audioStreamEnd": True}})
                        )
                        audio_finished.set()

                sender = asyncio.create_task(send_audio())
                finish_deadline: float | None = None
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                        except TimeoutError:
                            if audio_finished.is_set():
                                now = asyncio.get_running_loop().time()
                                if finish_deadline is None:
                                    finish_deadline = (
                                        now + self.settings.final_timeout_seconds
                                    )
                                elif now >= finish_deadline:
                                    log.info(
                                        "No final response within %ss after audio ended",
                                        self.settings.final_timeout_seconds,
                                    )
                                    break
                            continue
                        if audio_finished.is_set() and finish_deadline is None:
                            finish_deadline = (
                                asyncio.get_running_loop().time()
                                + self.settings.final_timeout_seconds
                            )
                        log.debug("Gemini message: %s", raw)
                        try:
                            response = json.loads(raw)
                        except json.JSONDecodeError as error:
                            log.error("Invalid Gemini message: %r", raw)
                            raise GeminiTranscriptionError(
                                "Gemini returned an invalid WebSocket message"
                            ) from error
                        if "error" in response:
                            log.error("Gemini error: %s", response)
                            raise GeminiTranscriptionError(self._response_error(response))

                        content = response.get("serverContent", {})
                        incoming_interim = _text_at(
                            content, "interimInputTranscription", "text"
                        )
                        incoming_final = _text_at(content, "inputTranscription", "text")

                        if incoming_interim and incoming_interim != interim_text:
                            interim_text = incoming_interim
                            if on_interim:
                                await on_interim(interim_text)

                        if incoming_final:
                            log.info("Final transcript segment: %r", incoming_final)
                            final_text = merge_transcript(final_text, incoming_final)
                            interim_text = ""
                            if on_final:
                                await on_final(final_text)

                        if audio_finished.is_set() and (
                            content.get("turnComplete") or content.get("generationComplete")
                        ):
                            log.info("Gemini turn complete")
                            break
                finally:
                    if not sender.done():
                        stop_event.set()
                    await sender
        except GeminiTranscriptionError:
            raise
        except Exception as error:
            log.error("Transcription session failed: %s", self._safe_error(error))
            raise GeminiTranscriptionError(self._safe_error(error)) from error

        log.info("Transcription result: %r", final_text.strip())
        return final_text.strip()

    @staticmethod
    def _response_error(response: dict[str, Any]) -> str:
        error = response.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or error)
        return f"Unexpected Gemini response: {response}"
