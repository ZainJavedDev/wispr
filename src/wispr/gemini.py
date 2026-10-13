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

# After audio ends, finish once Gemini has been quiet this long.
QUIET_SECONDS = 0.3


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
        speaking = False
        # True once Gemini has finalized everything heard so far (a completion
        # with no unfinalized text after it).
        settled = False
        loop = asyncio.get_running_loop()
        audio_ended_at: float | None = None
        last_message_at = loop.time()

        # Record during the connect/setup handshake so the first words aren't
        # lost. Audio is queued in memory, so a slow handshake drops nothing.
        await audio.start()
        captured: asyncio.Queue[bytes | None] = asyncio.Queue()
        capture = asyncio.create_task(
            self._capture(audio, captured, stop_event, stop_on_eof)
        )

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
                log.info(
                    "Gemini ready with %.1fs of audio buffered", captured.qsize() * 0.1
                )

                chunks_sent = 0

                async def send_audio() -> None:
                    nonlocal chunks_sent, audio_ended_at
                    try:
                        while (chunk := await captured.get()) is not None:
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
                        await websocket.send(
                            json.dumps({"realtimeInput": {"audioStreamEnd": True}})
                        )
                        audio_ended_at = loop.time()

                sender = asyncio.create_task(send_audio())
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                        except TimeoutError:
                            if audio_ended_at is None:
                                continue
                            now = loop.time()
                            # Live audio: once Gemini has caught up and gone quiet, with
                            # no speech in progress and no unfinalized text, we're done.
                            # "Caught up" needs a reply after the audio ended unless the
                            # transcript was already final: audio queued during the
                            # handshake can reach Gemini in one burst just before the
                            # end, and Gemini may not have answered it yet. A file is
                            # sent faster than real time, so keep waiting there.
                            caught_up = settled or last_message_at > audio_ended_at
                            quiet_for = now - max(last_message_at, audio_ended_at)
                            if (
                                not stop_on_eof
                                and caught_up
                                and not speaking
                                and not interim_text
                                and quiet_for >= QUIET_SECONDS
                            ):
                                log.info("Gemini idle after audio ended; finishing")
                                break
                            if now - audio_ended_at >= self.settings.final_timeout_seconds:
                                log.info(
                                    "No final response within %ss after audio ended",
                                    self.settings.final_timeout_seconds,
                                )
                                break
                            continue
                        last_message_at = loop.time()
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

                        activity = _text_at(response, "voiceActivity", "type")
                        if activity:
                            speaking = activity == "ACTIVITY_START"
                            if speaking:
                                settled = False

                        content = response.get("serverContent", {})
                        incoming_interim = _text_at(
                            content, "interimInputTranscription", "text"
                        )
                        incoming_final = _text_at(content, "inputTranscription", "text")

                        if incoming_interim and incoming_interim != interim_text:
                            interim_text = incoming_interim
                            settled = False
                            if on_interim:
                                await on_interim(interim_text)

                        if incoming_final:
                            log.info("Final transcript segment: %r", incoming_final)
                            final_text = merge_transcript(final_text, incoming_final)
                            interim_text = ""
                            if on_final:
                                await on_final(final_text)

                        if content.get("turnComplete") or content.get("generationComplete"):
                            if audio_ended_at is not None:
                                log.info("Gemini turn complete")
                                break
                            settled = not interim_text
                finally:
                    if not sender.done():
                        stop_event.set()
                    await sender
        except GeminiTranscriptionError:
            raise
        except Exception as error:
            log.error("Transcription session failed: %s", self._safe_error(error))
            raise GeminiTranscriptionError(self._safe_error(error)) from error
        finally:
            # Also stops the recorder when connecting to Gemini failed.
            if not capture.done():
                stop_event.set()
            await capture

        log.info("Transcription result: %r", final_text.strip())
        return final_text.strip()

    async def _capture(
        self,
        audio: AudioProcess,
        captured: asyncio.Queue[bytes | None],
        stop_event: asyncio.Event,
        stop_on_eof: bool,
    ) -> None:
        """Read audio into memory until stopped; None marks the end."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            while not stop_event.is_set():
                if loop.time() - started >= self.settings.max_duration_seconds:
                    log.warning("Max duration reached, stopping audio")
                    stop_event.set()
                    break
                chunk = await audio.read_chunk()
                if not chunk:
                    log.info("Audio source returned EOF")
                    if stop_on_eof:
                        stop_event.set()
                    break
                captured.put_nowait(chunk)
        finally:
            await audio.stop()
            captured.put_nowait(None)

    @staticmethod
    def _response_error(response: dict[str, Any]) -> str:
        error = response.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or error)
        return f"Unexpected Gemini response: {response}"
