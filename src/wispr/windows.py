"""Win32 integration: clipboard, simulated paste, global hotkey, and toasts."""

from __future__ import annotations

import base64
import ctypes
import logging
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56

MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}
NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "pause": 0x13,
    "capslock": 0x14,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
}

ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member and fixes the struct size SendInput expects.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _open_clipboard() -> bool:
    # Another application may briefly hold the clipboard open.
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.025)
    return False


def set_clipboard_text(text: str) -> bool:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not _open_clipboard():
        log.error("Could not open the clipboard (error %s)", ctypes.get_last_error())
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            # Ownership only transfers to the system on success.
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def wait_for_modifiers_released(timeout: float = 2.0) -> None:
    """Wait until the user lets go of the hotkey so Ctrl+V isn't Win+Alt+Ctrl+V."""
    deadline = time.monotonic() + timeout
    keys = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(key) & 0x8000 for key in keys):
            return
        time.sleep(0.02)
    log.warning("Modifier keys still held after %.1fs; pasting anyway", timeout)


def _key(vk: int, up: bool = False) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if up else 0)),
    )


def send_paste() -> bool:
    wait_for_modifiers_released()
    inputs = (INPUT * 4)(
        _key(VK_CONTROL), _key(VK_V), _key(VK_V, up=True), _key(VK_CONTROL, up=True)
    )
    sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        log.error("SendInput sent %s of %s events (error %s)", sent, len(inputs), ctypes.get_last_error())
    return sent == len(inputs)


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse e.g. "win+alt+space" into RegisterHotKey modifiers and virtual key."""
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if not parts:
        raise ValueError("WISPR_HOTKEY is empty")
    *modifier_names, key_name = parts
    modifiers = 0
    for name in modifier_names:
        if name not in MODIFIERS:
            raise ValueError(f"Unknown hotkey modifier: {name!r}")
        modifiers |= MODIFIERS[name]
    if key_name in NAMED_KEYS:
        vk = NAMED_KEYS[key_name]
    elif len(key_name) == 1 and key_name.isalnum():
        vk = ord(key_name.upper())
    else:
        raise ValueError(f"Unknown hotkey key: {key_name!r}")
    return modifiers, vk


class HotkeyListener:
    """Registers a system-wide hotkey on a dedicated message-loop thread."""

    HOTKEY_ID = 1

    def __init__(self, spec: str, callback: Callable[[], None]) -> None:
        self.spec = spec
        self.modifiers, self.vk = parse_hotkey(spec)
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="wispr-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self._error:
            raise RuntimeError(self._error)

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(2)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(
            None, self.HOTKEY_ID, self.modifiers | MOD_NOREPEAT, self.vk
        ):
            self._error = (
                f"Could not register hotkey {self.spec!r} (error {ctypes.get_last_error()}); "
                "another program or Wispr instance may already use it"
            )
            self._ready.set()
            return
        log.info("Registered global hotkey %s", self.spec)
        self._ready.set()
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == self.HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Hotkey callback failed")
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)


# Toasts need a registered AppUserModelID; PowerShell's is present on every install.
_TOAST_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
)
_TOAST_SETUP = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}')
function Show-Toast([string] $text) {
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($text)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    $toast.Tag = 'wispr'
    $toast.Group = 'wispr'
    $toast.ExpirationTime = [DateTimeOffset]::Now.AddSeconds(15)
    $notifier.Show($toast)
}
""".replace("{app_id}", _TOAST_APP_ID)

# Long-lived variant: one base64 toast XML per stdin line. Starting PowerShell
# takes ~2s, so the daemon keeps this running and toasts appear immediately.
_TOAST_SERVER_SCRIPT = _TOAST_SETUP + """
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try { Show-Toast ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))) } catch {}
}
"""


def _powershell(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def toast_xml(summary: str, body: str = "") -> str:
    lines = f"<text>{escape(summary)}</text>"
    if body:
        lines += f"<text>{escape(body)}</text>"
    return (
        '<toast><visual><binding template="ToastGeneric">'
        f"{lines}</binding></visual><audio silent=\"true\"/></toast>"
    )


def toast_command(summary: str, body: str = "") -> list[str]:
    """One-shot toast, for when no daemon is running to show it."""
    xml = toast_xml(summary, body).replace("'", "''")
    return _powershell(_TOAST_SETUP + f"Show-Toast '{xml}'\n")


def toast_server_command() -> list[str]:
    return _powershell(_TOAST_SERVER_SCRIPT)
