from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .audio import AudioProcess, file_command
from .config import PROJECT_ROOT, Settings
from .daemon import WisprDaemon, send_command
from .gemini import GeminiLiveTranscriber
from .output import paste_backend


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wispr", description="System-wide Gemini Live dictation for Linux"
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
        AudioProcess(file_command(path)), stop_event, stop_on_eof=True
    )
    print(transcript)
    return 0 if transcript else 2


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # websockets logs the request URI, which contains the API key.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.command == "daemon":
        await WisprDaemon(settings).serve()
        return 0
    if args.command == "doctor":
        return await _doctor(settings, args.check_api)
    if args.command == "transcribe-file":
        return await _transcribe_file(settings, args.path)

    response = await send_command(settings.socket_path, args.command)
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_main_async(args)))
    except (ValueError, RuntimeError) as error:
        print(f"wispr: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130)
