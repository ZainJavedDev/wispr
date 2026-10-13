from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .audio import default_input_device, file_source
from .config import PROJECT_ROOT, Settings, default_state_dir
from .daemon import WisprDaemon, send_command
from .gemini import GeminiLiveTranscriber
from .output import paste_backend

WINDOWS = sys.platform == "win32"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wispr", description="System-wide Gemini Live dictation"
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("daemon", help="run the background dictation service")
    for command in ("toggle", "start", "stop", "cancel", "status", "quit"):
        subparsers.add_parser(command, help=f"{command} dictation")

    doctor = subparsers.add_parser("doctor", help="check local and Gemini setup")
    doctor.add_argument(
        "--check-api", action="store_true", help="open an authenticated Gemini session"
    )

    transcribe = subparsers.add_parser(
        "transcribe-file", help="transcribe an existing audio file"
    )
    transcribe.add_argument("path", type=Path)
    return parser


async def _doctor(settings: Settings, check_api: bool) -> int:
    if WINDOWS:
        checks = {
            "GEMINI_API_KEY": "configured",
            "platform": "windows",
            "audio": default_input_device(),
            "clipboard": "win32",
            "paste": paste_backend(),
            "hotkey": settings.hotkey or "disabled",
            "control": f"127.0.0.1:{settings.control_port}",
            "log": str(settings.state_dir / "wispr.log"),
            "file decoding": "ffmpeg" if shutil.which("ffmpeg") else "wav only",
            "model": settings.model,
        }
    else:
        checks = {
            "GEMINI_API_KEY": "configured",
            "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
            "audio": next(
                (tool for tool in ("pw-record", "parec", "arecord") if shutil.which(tool)),
                "missing",
            ),
            "clipboard": next(
                (tool for tool in ("wl-copy", "xclip", "xsel") if shutil.which(tool)),
                "missing",
            ),
            "paste": paste_backend(),
            "socket": str(settings.socket_path),
            "model": settings.model,
        }
    print(json.dumps(checks, indent=2))
    if check_api:
        await GeminiLiveTranscriber(settings).check_connection()
        print("Gemini connection: OK")
    return 0


async def _transcribe_file(settings: Settings, path: Path) -> int:
    stop_event = asyncio.Event()
    transcript = await GeminiLiveTranscriber(settings).transcribe(
        file_source(path), stop_event, stop_on_eof=True
    )
    print(transcript)
    return 0 if transcript else 2


def _configure_logging(settings: Settings, command: str) -> None:
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # None under pythonw/wisprw.exe
        handlers.append(logging.StreamHandler())
    if WINDOWS and command == "daemon":
        # No journald on Windows; keep a small rotating log instead.
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                settings.state_dir / "wispr.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    _configure_logging(settings, args.command)
    # websockets logs the request URI, which contains the API key.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.command == "daemon":
        await WisprDaemon(settings).serve()
        return 0
    if args.command == "doctor":
        return await _doctor(settings, args.check_api)
    if args.command == "transcribe-file":
        return await _transcribe_file(settings, args.path)

    response = await send_command(settings, args.command)
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


def _report_windowless_error(message: str) -> None:
    """Without a console, a startup failure would otherwise vanish silently."""
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "wispr.log"
    line = f"ERROR wispr: {message}"
    # The scheduled-task watchdog retries every few minutes; toast only once.
    repeated = log_path.is_file() and log_path.read_text(
        encoding="utf-8", errors="replace"
    ).rstrip().endswith(line)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    if WINDOWS and not repeated:
        from .windows import toast_command

        subprocess.run(
            toast_command("Wispr failed to start", message[:240]),
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_main_async(args)))
    except (ValueError, RuntimeError) as error:
        if sys.stderr is not None:
            print(f"wispr: {error}", file=sys.stderr)
        else:
            _report_windowless_error(str(error))
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130)
