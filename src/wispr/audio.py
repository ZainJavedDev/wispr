from __future__ import annotations

import array
import asyncio
import logging
import math
import shutil
import wave
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)


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


class AudioProcess:
    """Async raw-PCM source backed by a recorder or ffmpeg process.

    Expects 16 kHz mono s16le. Optionally saves the stream to a WAV file.
    """

    def __init__(self, command: Sequence[str], record_path: Path | None = None) -> None:
        self.command = list(command)
        self.record_path = record_path
        self.process: asyncio.subprocess.Process | None = None
        self._wav: wave.Wave_write | None = None
        self.bytes_read = 0
        self.peak = 0
        self._sum_squares = 0

    async def start(self) -> None:
        log.info("Starting recorder: %s", " ".join(self.command))
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self.process.stdout is None:
            raise AudioCaptureError("Recorder did not expose an audio stream")
        log.info("Recorder started (pid %s)", self.process.pid)
        if self.record_path:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            self._wav = wave.open(str(self.record_path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(16000)
            log.info("Saving recording to %s", self.record_path)

    async def read_chunk(self, size: int = 3200) -> bytes:
        if self.process is None or self.process.stdout is None:
            raise AudioCaptureError("Recorder has not been started")
        try:
            chunk = await self.process.stdout.readexactly(size)
        except asyncio.IncompleteReadError as error:
            chunk = error.partial
            log.warning(
                "Recorder stream ended after %.2fs of audio (exit code %s)",
                self.bytes_read / 32000,
                self.process.returncode,
            )
        samples = array.array("h", chunk[: len(chunk) // 2 * 2])
        if samples:
            chunk_peak = max(abs(sample) for sample in samples)
            self.peak = max(self.peak, chunk_peak)
            self._sum_squares += sum(sample * sample for sample in samples)
            if self.bytes_read // 32000 != (self.bytes_read + len(chunk)) // 32000:
                log.debug("Audio at %.0fs: chunk peak %s", self.bytes_read / 32000, chunk_peak)
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
            f"{self.bytes_read / 32000:.2f}s audio, peak {self.peak} ({dbfs(self.peak)}), "
            f"rms {rms:.0f} ({dbfs(rms)})"
        )

    async def stop(self) -> None:
        if self._wav:
            self._wav.close()
            self._wav = None
            log.info("Recording saved: %s", self.record_path)
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
