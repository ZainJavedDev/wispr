import array
import asyncio
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from wispr.audio import WavFileAudio, resample_pcm16


class ResampleTests(unittest.TestCase):
    def test_downmixes_and_resamples_to_16k(self) -> None:
        stereo_48k = array.array("h", [1000, 3000] * 4800).tobytes()
        mono_16k = array.array("h", resample_pcm16(stereo_48k, 48000, 2))
        self.assertEqual(len(mono_16k), 1600)
        self.assertTrue(all(sample == 2000 for sample in mono_16k))

    def test_wav_file_source_reads_converted_pcm(self) -> None:
        async def read_all(path: Path) -> bytes:
            audio = WavFileAudio(path)
            await audio.start()
            data = b""
            while chunk := await audio.read_chunk():
                data += chunk
            await audio.stop()
            return data

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "take.wav"
            with wave.open(str(path), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(8000)
                recording.writeframes(array.array("h", [500] * 8000).tobytes())
            data = asyncio.run(read_all(path))
        self.assertEqual(len(data), 32000)


@unittest.skipUnless(sys.platform == "win32", "Win32 APIs")
class WindowsTests(unittest.TestCase):
    def test_parse_hotkey(self) -> None:
        from wispr.windows import MOD_ALT, MOD_CONTROL, MOD_WIN, parse_hotkey

        self.assertEqual(parse_hotkey("Win+Alt+Space"), (MOD_WIN | MOD_ALT, 0x20))
        self.assertEqual(parse_hotkey("super+ctrl+F9"), (MOD_WIN | MOD_CONTROL, 0x78))
        self.assertEqual(parse_hotkey("ctrl+alt+d"), (MOD_CONTROL | MOD_ALT, ord("D")))
        with self.assertRaises(ValueError):
            parse_hotkey("hyper+space")

    def test_clipboard_round_trip(self) -> None:
        from wispr.windows import get_clipboard_text, set_clipboard_text

        previous = get_clipboard_text()
        try:
            self.assertTrue(set_clipboard_text("Wispr — ünïcode test"))
            self.assertEqual(get_clipboard_text(), "Wispr — ünïcode test")
        finally:
            if previous is not None:
                set_clipboard_text(previous)

    def test_toast_command_escapes_markup_and_quotes(self) -> None:
        import base64

        from wispr.windows import toast_command

        command = toast_command("It's <done>", "a & b")
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertIn("It''s &lt;done&gt;", script)
        self.assertIn("a &amp; b", script)


if __name__ == "__main__":
    unittest.main()
