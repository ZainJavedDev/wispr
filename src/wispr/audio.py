from __future__ import annotations

import array
import asyncio
import logging
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
# The daemon runs windowless on Windows; keep child consoles from flashing up.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class AudioCaptureError(RuntimeError):
    pass


def microphone_command(audio_source: str | None = None) -> list[str]:
    if shutil.which("pw-record"):
        command = [
            "pw-record",
            "--format=s16",
            "--rate=16000",
            "--channels=1",
            "--latency=100ms",
        ]
        if audio_source:
            command.append(f"--target={audio_source}")
        return [*command, "-"]

    if shutil.which("parec"):
        command = [
            "parec",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
        ]
        if audio_source:
            command.append(f"--device={audio_source}")
        return command

    if shutil.which("arecord"):
        command = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"]
        if audio_source:
            command.extend(["-D", audio_source])
        return command

    raise AudioCaptureError("No supported recorder found (pw-record, parec, or arecord)")


def microphone_source(
    audio_source: str | None = None, record_path: Path | None = None
) -> AudioSource:
    if sys.platform == "win32":
        return MicrophoneAudio(audio_source, record_path)
    return AudioProcess(microphone_command(audio_source), record_path)


def file_command(path: Path) -> list[str]:
    if not path.is_file():
        raise AudioCaptureError(f"Audio file does not exist: {path}")
    if not shutil.which("ffmpeg"):
        raise AudioCaptureError("ffmpeg is required to transcribe audio files")
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        "16000",
        "pipe:1",
    ]


def file_source(path: Path) -> AudioSource:
    """Decode with ffmpeg when available; plain PCM WAV files work without it."""
    if not shutil.which("ffmpeg") and path.suffix.lower() == ".wav":
        if not path.is_file():
            raise AudioCaptureError(f"Audio file does not exist: {path}")
        return WavFileAudio(path)
    return AudioProcess(file_command(path))


def default_input_device() -> str:
    if sys.platform == "win32":
        try:
            import sounddevice

            device = _windows_input_device(None)
            return sounddevice.query_devices(device, "input")["name"]
        except Exception as error:
            return f"unknown ({error})"
    if not shutil.which("pactl"):
        return "unknown (pactl not installed)"
    result = subprocess.run(
        ["pactl", "get-default-source"], capture_output=True, text=True, timeout=2
    )
    return result.stdout.strip() or result.stderr.strip()


def _windows_input_device(audio_source: str | None) -> int | str | None:
    """Resolve WISPR_AUDIO_SOURCE (device index or name substring) for sounddevice.

    Without one, use the MME sound mapper, which follows the current Windows
    default microphone even when it changes while the daemon is running.
    """
    import sounddevice

    if audio_source:
        return int(audio_source) if audio_source.isdigit() else audio_source
    for index, device in enumerate(sounddevice.query_devices()):
        if device["max_input_channels"] and device["name"].startswith(
            "Microsoft Sound Mapper - Input"
        ):
            return index
    return None


def resample_pcm16(pcm: bytes, rate: int, channels: int) -> bytes:
    """Downmix to mono and linearly resample s16le audio to 16 kHz."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if channels > 1:
        samples = array.array(
            "h",
            (
                sum(samples[i : i + channels]) // channels
                for i in range(0, len(samples) - channels + 1, channels)
            ),
        )
    if rate == SAMPLE_RATE or not samples:
        return samples.tobytes()
    count = int(len(samples) * SAMPLE_RATE / rate)
    step = rate / SAMPLE_RATE
    last = len(samples) - 1
    resampled = array.array("h")
    for index in range(count):
        position = index * step
        left = int(position)
        right = min(left + 1, last)
        fraction = position - left
        resampled.append(int(samples[left] + (samples[right] - samples[left]) * fraction))
    return resampled.tobytes()


class AudioSource:
    """Async 16 kHz mono s16le source with level tracking.

    Optionally saves the stream to a WAV file.
    """

    def __init__(self, record_path: Path | None = None) -> None:
        self.record_path = record_path
        self._wav: wave.Wave_write | None = None
        self.bytes_read = 0
        self.peak = 0
        self._sum_squares = 0

    async def start(self) -> None:
        raise NotImplementedError

    async def read_chunk(self, size: int = 3200) -> bytes:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    def _open_recording(self) -> None:
        if not self.record_path:
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.record_path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)
        log.info("Saving recording to %s", self.record_path)

    def _close_recording(self) -> None:
        if self._wav:
            self._wav.close()
            self._wav = None
            log.info("Recording saved: %s", self.record_path)

    def _track(self, chunk: bytes) -> bytes:
        samples = array.array("h", chunk[: len(chunk) // 2 * 2])
        if samples:
            chunk_peak = max(abs(sample) for sample in samples)
            self.peak = max(self.peak, chunk_peak)
            self._sum_squares += sum(sample * sample for sample in samples)
            if (
                self.bytes_read // BYTES_PER_SECOND
                != (self.bytes_read + len(chunk)) // BYTES_PER_SECOND
            ):
                log.debug(
                    "Audio at %.0fs: chunk peak %s",
                    self.bytes_read / BYTES_PER_SECOND,
                    chunk_peak,
                )
        self.bytes_read += len(chunk)
        if self._wav:
            self._wav.writeframes(chunk)
        return chunk

    def level_summary(self) -> str:
        sample_count = self.bytes_read // 2
        rms = math.sqrt(self._sum_squares / sample_count) if sample_count else 0.0

        def dbfs(value: float) -> str:
            return f"{20 * math.log10(value / 32768):.1f} dBFS" if value else "-inf dBFS"

        return (
            f"{self.bytes_read / BYTES_PER_SECOND:.2f}s audio, "
            f"peak {self.peak} ({dbfs(self.peak)}), rms {rms:.0f} ({dbfs(rms)})"
        )


class AudioProcess(AudioSource):
    """Raw-PCM source backed by a recorder or ffmpeg process."""

    def __init__(self, command: Sequence[str], record_path: Path | None = None) -> None:
        super().__init__(record_path)
        self.command = list(command)
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        log.info("Starting recorder: %s", " ".join(self.command))
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **NO_WINDOW,
        )
        if self.process.stdout is None:
            raise AudioCaptureError("Recorder did not expose an audio stream")
        log.info("Recorder started (pid %s)", self.process.pid)
        self._open_recording()

    async def read_chunk(self, size: int = 3200) -> bytes:
        if self.process is None or self.process.stdout is None:
            raise AudioCaptureError("Recorder has not been started")
        try:
            chunk = await self.process.stdout.readexactly(size)
        except asyncio.IncompleteReadError as error:
            chunk = error.partial
            log.warning(
                "Recorder stream ended after %.2fs of audio (exit code %s)",
                self.bytes_read / BYTES_PER_SECOND,
                self.process.returncode,
            )
        return self._track(chunk)

    async def stop(self) -> None:
        self._close_recording()
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.5)
            except TimeoutError:
                log.warning("Recorder did not exit after SIGTERM, killing it")
                self.process.kill()
                await self.process.wait()
        stderr = await self.error_text()
        log.info(
            "Recorder exited with code %s; %s", self.process.returncode, self.level_summary()
        )
        if stderr:
            log.info("Recorder stderr: %s", stderr)
        self.process = None

    async def error_text(self) -> str:
        if self.process is None or self.process.stderr is None:
            return ""
        data = await self.process.stderr.read()
        return data.decode("utf-8", errors="replace").strip()


class MicrophoneAudio(AudioSource):
    """Microphone capture through PortAudio (sounddevice), used on Windows."""

    def __init__(
        self, audio_source: str | None = None, record_path: Path | None = None
    ) -> None:
        super().__init__(record_path)
        self.audio_source = audio_source
        self._stream = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._buffer = bytearray()
        self._ended = False

    async def start(self) -> None:
        import sounddevice

        loop = asyncio.get_running_loop()

        def callback(data, frames, time, status) -> None:  # runs on PortAudio's thread
            if status:
                log.debug("Audio status: %s", status)
            loop.call_soon_threadsafe(self._queue.put_nowait, bytes(data))

        try:
            self._stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=SAMPLE_RATE // 10,
                device=_windows_input_device(self.audio_source),
                callback=callback,
            )
            self._stream.start()
        except Exception as error:
            raise AudioCaptureError(f"Could not open microphone: {error}") from error
        log.info(
            "Recording from %s",
            sounddevice.query_devices(self._stream.device, "input")["name"],
        )
        self._open_recording()

    async def read_chunk(self, size: int = 3200) -> bytes:
        if self._stream is None:
            raise AudioCaptureError("Recorder has not been started")
        while len(self._buffer) < size and not self._ended:
            data = await self._queue.get()
            if data is None:
                self._ended = True
            else:
                self._buffer += data
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return self._track(chunk)

    async def stop(self) -> None:
        self._close_recording()
        if self._stream is None:
            return
        await asyncio.to_thread(self._close_stream)
        self._queue.put_nowait(None)
        log.info("Recorder stopped; %s", self.level_summary())

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()


class WavFileAudio(AudioSource):
    """Reads a 16-bit PCM WAV file, converted to 16 kHz mono."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self._pcm = b""
        self._offset = 0

    async def start(self) -> None:
        with wave.open(str(self.path), "rb") as source:
            if source.getsampwidth() != 2:
                raise AudioCaptureError(
                    "Only 16-bit PCM WAV files are supported without ffmpeg"
                )
            raw = source.readframes(source.getnframes())
            self._pcm = resample_pcm16(raw, source.getframerate(), source.getnchannels())
        log.info("Loaded %s (%.2fs)", self.path, len(self._pcm) / BYTES_PER_SECOND)

    async def read_chunk(self, size: int = 3200) -> bytes:
        chunk = self._pcm[self._offset : self._offset + size]
        self._offset += len(chunk)
        return self._track(chunk)

    async def stop(self) -> None:
        log.info("File source finished; %s", self.level_summary())
