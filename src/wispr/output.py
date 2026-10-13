from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .audio import NO_WINDOW

WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class DeliveryResult:
    copied: bool
    pasted: bool
    message: str


def paste_backend() -> str:
    if WINDOWS:
        return "sendinput"
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if session == "wayland" and shutil.which("ydotool"):
        return "ydotool"
    if session == "wayland" and "gnome" in desktop and Path("/usr/bin/python3").is_file():
        return "gnome-remote-desktop"
    if session != "wayland" and shutil.which("xdotool"):
        return "xdotool"
    return "clipboard-only"


async def _run_with_input(command: list[str], value: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **NO_WINDOW,
        )
        await process.communicate(value.encode("utf-8"))
        return process.returncode == 0
    except OSError:
        return False


async def _run(command: list[str]) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **NO_WINDOW,
        )
        return await process.wait() == 0
    except OSError:
        return False


_toaster: asyncio.subprocess.Process | None = None


async def start_notifications() -> None:
    """Start the Windows toast helper ahead of time; it takes ~2s to load."""
    global _toaster
    if not WINDOWS or (_toaster and _toaster.returncode is None):
        return
    from .windows import toast_server_command

    _toaster = await asyncio.create_subprocess_exec(
        *toast_server_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **NO_WINDOW,
    )


async def notify(summary: str, body: str = "") -> None:
    if WINDOWS:
        from .windows import toast_xml

        await start_notifications()
        assert _toaster and _toaster.stdin
        line = base64.b64encode(toast_xml(summary, body).encode("utf-8")) + b"\n"
        try:
            _toaster.stdin.write(line)
            await _toaster.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # helper died; the next notification restarts it
        return
    if not shutil.which("notify-send"):
        return
    command = ["notify-send", "--app-name=Wispr Gemini", summary]
    if body:
        command.append(body)
    await _run(command)


class TextOutput:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def deliver(self, text: str) -> DeliveryResult:
        if self.mode == "stdout":
            print(text, flush=True)
            return DeliveryResult(False, False, "Printed transcript")

        copied = await self._copy(text)
        if not copied:
            print(text, file=sys.stderr, flush=True)
            return DeliveryResult(False, False, "Clipboard unavailable; printed transcript")

        if self.mode == "clipboard":
            return DeliveryResult(True, False, "Transcript copied to clipboard")

        await asyncio.sleep(0.08)
        pasted = await self._paste()
        if pasted:
            return DeliveryResult(True, True, "Transcript pasted")
        return DeliveryResult(True, False, "Transcript copied; paste helper unavailable")

    async def _copy(self, text: str) -> bool:
        if WINDOWS:
            from .windows import set_clipboard_text

            return await asyncio.to_thread(set_clipboard_text, text)
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session == "wayland" and shutil.which("wl-copy"):
            return await _run_with_input(["wl-copy", "--type", "text/plain;charset=utf-8"], text)
        if shutil.which("xclip"):
            return await _run_with_input(
                ["xclip", "-selection", "clipboard", "-in"], text
            )
        if shutil.which("xsel"):
            return await _run_with_input(["xsel", "--clipboard", "--input"], text)
        return False

    async def _paste(self) -> bool:
        backend = paste_backend()
        if backend == "sendinput":
            from .windows import send_paste

            # Ctrl+V works in Windows GUI apps and modern terminals alike.
            return await asyncio.to_thread(send_paste)
        if backend == "ydotool":
            # Shift+Insert works in terminals and conventional GUI text fields.
            return await _run(
                ["ydotool", "key", "42:1", "110:1", "110:0", "42:0"]
            )
        if backend == "gnome-remote-desktop":
            helper = Path(__file__).with_name("gnome_paste.py")
            return await _run(["/usr/bin/python3", str(helper)])
        if backend == "xdotool":
            return await _run(["xdotool", "key", "--clearmodifiers", "shift+Insert"])
        return False
