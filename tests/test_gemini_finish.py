import asyncio
import base64
import json
import time
import unittest
from unittest.mock import patch

from wispr.config import Settings
from wispr.gemini import GeminiLiveTranscriber, GeminiTranscriptionError


class FakeAudio:
    """100 ms chunks, each tagged with its 1-based index."""

    def __init__(self, chunks: int = 5) -> None:
        self.chunks = chunks
        self.read = 0
        self.started_at: float | None = None
        self.stopped = False

    async def start(self) -> None:
        self.started_at = time.monotonic()

    async def read_chunk(self, size: int = 3200) -> bytes:
        await asyncio.sleep(0.02)
        if not self.chunks:
            return b""
        self.chunks -= 1
        self.read += 1
        return bytes([self.read % 256]) * size

    async def stop(self) -> None:
        self.stopped = True

    def level_summary(self) -> str:
        return ""


class FakeWebSocket:
    """Replays Gemini messages; a number in the list is a pause in seconds."""

    def __init__(self, messages: list, connect_delay: float = 0) -> None:
        self.messages = list(messages)
        self.resume_at: float | None = None
        self.connect_delay = connect_delay
        self.connected_at: float | None = None
        self.audio: list[bytes] = []

    async def __aenter__(self) -> "FakeWebSocket":
        await asyncio.sleep(self.connect_delay)
        self.connected_at = time.monotonic()
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def send(self, data: str) -> None:
        message = json.loads(data)
        chunk = message.get("realtimeInput", {}).get("audio")
        if chunk:
            self.audio.append(base64.b64decode(chunk["data"]))

    async def recv(self) -> str:
        # recv() is cancelled by the caller's polling timeout, so a pause is kept
        # as a deadline rather than a sleep that would restart on every call.
        while self.messages:
            message = self.messages[0]
            if isinstance(message, dict):
                self.messages.pop(0)
                return json.dumps(message)
            if self.resume_at is None:
                self.resume_at = time.monotonic() + message
            remaining = self.resume_at - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.messages.pop(0)
            self.resume_at = None
        await asyncio.Event().wait()  # Gemini has nothing more to say
        raise AssertionError("unreachable")


class FinishTests(unittest.TestCase):
    def transcribe(
        self, messages: list, socket: FakeWebSocket | None = None
    ) -> tuple[str, float]:
        settings = Settings(api_key="test", final_timeout_seconds=3)
        self.socket = socket or FakeWebSocket(messages)
        self.audio = FakeAudio()

        async def run() -> str:
            with patch("websockets.connect", return_value=self.socket):
                # Live dictation: the audio simply ends, as when the user stops.
                return await GeminiLiveTranscriber(settings).transcribe(
                    self.audio, asyncio.Event()
                )

        started = time.monotonic()
        text = asyncio.run(run())
        return text, time.monotonic() - started

    def test_audio_during_connection_is_buffered_and_sent(self) -> None:
        messages = [
            {"setupComplete": {}},
            {"serverContent": {"inputTranscription": {"text": "hi"}}},
            {"serverContent": {"generationComplete": True}},
        ]
        # The whole recording finishes before the slow connection opens.
        text, _ = self.transcribe([], FakeWebSocket(messages, connect_delay=0.3))
        self.assertEqual(text, "hi")
        self.assertLess(self.audio.started_at, self.socket.connected_at)
        self.assertEqual([chunk[0] for chunk in self.socket.audio], [1, 2, 3, 4, 5])

    def test_short_dictation_waits_for_gemini_to_answer_the_burst(self) -> None:
        # Stopped before Gemini was ready: all audio arrives at once and Gemini
        # needs a moment before it says anything. Silence then is not "done".
        text, _ = self.transcribe(
            [],
            FakeWebSocket(
                [
                    {"setupComplete": {}},
                    0.8,
                    {"serverContent": {"inputTranscription": {"text": "short"}}},
                    {"serverContent": {"generationComplete": True}},
                ],
                connect_delay=0.3,
            ),
        )
        self.assertEqual(text, "short")

    def test_connection_failure_stops_the_recorder(self) -> None:
        audio = FakeAudio(chunks=10_000)

        async def run() -> None:
            with patch("websockets.connect", side_effect=OSError("offline")):
                await GeminiLiveTranscriber(Settings(api_key="test")).transcribe(
                    audio, asyncio.Event()
                )

        with self.assertRaisesRegex(GeminiTranscriptionError, "offline"):
            asyncio.run(run())
        self.assertTrue(audio.stopped)

    def test_finishes_quickly_when_turn_completed_before_stop(self) -> None:
        text, elapsed = self.transcribe(
            [
                {"setupComplete": {}},
                {"serverContent": {"inputTranscription": {"text": "hello world"}}},
                {"serverContent": {"generationComplete": True}},
            ]
        )
        self.assertEqual(text, "hello world")
        self.assertLess(elapsed, 1.5)

    def test_waits_for_final_when_speech_is_still_pending(self) -> None:
        text, elapsed = self.transcribe(
            [
                {"setupComplete": {}},
                {"serverContent": {"inputTranscription": {"text": "hello"}}},
                {"serverContent": {"generationComplete": True}},
                {"serverContent": {"interimInputTranscription": {"text": "and more"}}},
            ]
        )
        self.assertEqual(text, "hello")
        self.assertGreaterEqual(elapsed, 3)


if __name__ == "__main__":
    unittest.main()
