from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeliveryResult:
    copied: bool
    pasted: bool
    message: str


def paste_backend() -> str:
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
        )
        return await process.wait() == 0
    except OSError:
        return False


async def notify(summary: str, body: str = "") -> None:
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
