import asyncio
import json
import time
import unittest
from unittest.mock import patch

from wispr.config import Settings
from wispr.gemini import GeminiLiveTranscriber, merge_transcript


class FakeAudio:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def read_chunk(self) -> bytes:
        await asyncio.sleep(0.01)
        return b"\0" * 3200


class FakeWebSocket:
    """Replays the message order seen in production: the final transcript,
    generationComplete and ACTIVITY_END arrive before the audio is marked
    finished, and nothing follows them (no turnComplete)."""

    def __init__(self) -> None:
        self.messages = [
            {"serverContent": {"inputTranscription": {"text": "Hello there."}}},
            {"serverContent": {"generationComplete": True}},
            {"serverContent": {}, "voiceActivity": {"type": "ACTIVITY_END"}},
        ]
        self.setup_sent = False

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def send(self, message: str) -> None:
        pass

    async def recv(self) -> str:
        if not self.setup_sent:
            self.setup_sent = True
            return json.dumps({"setupComplete": {}})
        if self.messages:
            return json.dumps(self.messages.pop(0))
        await asyncio.Event().wait()  # Gemini goes quiet
        return ""


class TranscribeFinishTests(unittest.TestCase):
    def test_finishes_promptly_when_gemini_goes_quiet(self) -> None:
        async def run() -> tuple[str, float]:
            stop = asyncio.Event()
            transcriber = GeminiLiveTranscriber(Settings(api_key="k"))
            with patch("websockets.connect", lambda *a, **k: FakeWebSocket()):
                task = asyncio.create_task(transcriber.transcribe(FakeAudio(), stop))
                await asyncio.sleep(0.1)
                started = time.monotonic()
                stop.set()
                text = await task
            return text, time.monotonic() - started

        text, elapsed = asyncio.run(run())
        self.assertEqual(text, "Hello there.")
        self.assertLess(elapsed, 2)  # was final_timeout_seconds (8s)


class TranscriptMergeTests(unittest.TestCase):
    def test_merges_distinct_final_segments(self) -> None:
        self.assertEqual(merge_transcript("hello", "world"), "hello world")

    def test_replaces_with_cumulative_transcript(self) -> None:
        self.assertEqual(merge_transcript("hello", "hello world"), "hello world")

    def test_ignores_duplicate_segment(self) -> None:
        self.assertEqual(merge_transcript("hello world", "world"), "hello world")


if __name__ == "__main__":
    unittest.main()

