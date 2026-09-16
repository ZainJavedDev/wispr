import array
import asyncio
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from wispr.audio import AudioProcess


class AudioProcessTests(unittest.TestCase):
    def test_records_wav_and_tracks_peak(self) -> None:
        pcm = array.array("h", [0, 1000, -2000, 500] * 1000).tobytes()
        script = f"import sys; sys.stdout.buffer.write({pcm!r})"

        async def run(path: Path) -> AudioProcess:
            audio = AudioProcess([sys.executable, "-c", script], path)
            await audio.start()
            while await audio.read_chunk():
                pass
            await audio.stop()
            return audio

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug" / "take.wav"
            audio = asyncio.run(run(path))
            self.assertEqual(audio.peak, 2000)
            self.assertEqual(audio.bytes_read, len(pcm))
            with wave.open(str(path), "rb") as recording:
                self.assertEqual(recording.getframerate(), 16000)
                self.assertEqual(recording.readframes(recording.getnframes()), pcm)


if __name__ == "__main__":
    unittest.main()
