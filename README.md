# Wispr Gemini

System-wide voice dictation for Ubuntu and Windows using Gemini Live
Transcription. Press a global shortcut, speak, press it again, and the finalized
transcript is copied and pasted into the focused application.

Windows setup is covered in [Windows](#windows) below.

## Requirements

- Python 3.11+
- PipeWire (`pw-record`), PulseAudio (`parec`), or ALSA (`arecord`)
- `wl-copy` on Wayland or `xclip`/`xsel` on X11
- GNOME Wayland uses Mutter's session-scoped keyboard interface for automatic
  paste. Other Wayland desktops can use `ydotool`.
- A Gemini API key in `.env`

```dotenv
GEMINI_API_KEY=your_key_here

# Optional settings
WISPR_TRANSCRIPTION_MODE=SMART
# restrict detection; stops Urdu coming out as Hindi
WISPR_LANGUAGE_CODES=en-US,ur-PK
WISPR_OUTPUT=paste
# PipeWire/Pulse source name; empty = system default
WISPR_AUDIO_SOURCE=
# verbose logs + save recordings (see Debugging)
WISPR_DEBUG=1
```

Do not commit `.env`. It is excluded by this repository's `.gitignore`.

## Install

```bash
chmod +x scripts/install-user.sh
./scripts/install-user.sh
```

The installer creates a project-local virtual environment, installs the `wispr`
command in `~/.local/bin`, and starts a user-level systemd service.

On Ubuntu GNOME, configure the default `Super+Alt+Space` shortcut with:

```bash
./scripts/configure-gnome-shortcut.py
```

Alternatively, open **Settings → Keyboard → View and Customize Shortcuts →
Custom Shortcuts** and add:

- Name: `Wispr Dictation`
- Command: `/home/zain-javed/.local/bin/wispr toggle`
- Shortcut: `Super+Alt+Space` (or any key you prefer)

## Commands

```bash
wispr doctor                 # verify local dependencies
wispr doctor --check-api     # also authenticate with Gemini
wispr toggle                 # start/stop microphone dictation
wispr status                 # show state and the current interim text
wispr cancel                 # discard the active recording
wispr transcribe-file audio.wav
```

The Live API limits a single transcription session to ten minutes, so the
daemon stops streaming after 590 seconds. Start a new dictation to continue.

## Output behavior

On GNOME Wayland, the transcript is placed on the clipboard and pasted using
Mutter's session-scoped keyboard interface. On other Wayland desktops, Wispr
uses `ydotool` when available and otherwise leaves the transcript on the
clipboard for manual paste.

Set `WISPR_OUTPUT=clipboard` to deliberately disable simulated paste, or
`WISPR_OUTPUT=stdout` for debugging.

## Privacy and cost

Audio is streamed directly from the local recorder to Google's Gemini Live API.
It is not written to disk. A WebSocket connection exists only during active
dictation, limiting both exposure and billable audio time.

## Debugging

With `WISPR_DEBUG=1`, the daemon logs every step, including each Gemini message
and audio levels, and saves each dictation as a 16 kHz WAV in
`~/.local/state/wispr/recordings/`. These recordings **are** written to disk, so
turn debug off when you're done.

```bash
journalctl --user -u wispr -f                           # live logs
pw-play ~/.local/state/wispr/recordings/<file>.wav      # hear what Gemini got
```

## Windows

Requires Windows 10/11 and Python 3.11+ (the `py` launcher from python.org).
Install from PowerShell in the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

The installer creates `.venv`, adds a `wispr` command in
`%USERPROFILE%\.local\bin` (and puts that folder on your user PATH), and
registers a **Wispr Dictation** scheduled task that starts the daemon at logon.
If `.env` has no `GEMINI_API_KEY` yet, it creates the file; add the key and run
`Start-ScheduledTask -TaskName 'Wispr Dictation'`.

The daemon registers the global hotkey itself. The default is **Win+Alt+Space**,
matching Super+Alt+Space on Linux; no shortcut configuration is needed. To change
it, set `WISPR_HOTKEY` in `.env` (for example `ctrl+alt+space` or `win+shift+d`)
and restart the task.

Windows-specific settings:

```dotenv
WISPR_HOTKEY=win+alt+space   # empty disables the built-in hotkey
WISPR_AUDIO_SOURCE=          # device index or name substring; empty = Windows default mic
WISPR_CONTROL_PORT=47281     # localhost port used by `wispr toggle` etc.
```

How it differs from Linux:

- **Service**: a Windows service runs in session 0 and cannot reach your
  microphone, clipboard, or keyboard, so the daemon is a per-user scheduled task
  (the equivalent of the systemd user service). It runs `wisprw.exe`, so no
  console window appears.
- **Audio** is captured with `sounddevice` (PortAudio). By default it records
  from the Windows default microphone and follows changes to that default.
- **Paste** puts the transcript on the clipboard and sends Ctrl+V. Windows does
  not allow simulated input into windows running as administrator, so in those
  windows the transcript stays on the clipboard for you to paste.
- **Control**: commands go over `127.0.0.1` with a per-session token stored in
  `%LOCALAPPDATA%\wispr\control-token`, instead of a Unix socket.
- **Logs** go to `%LOCALAPPDATA%\wispr\wispr.log`; debug recordings go to
  `%LOCALAPPDATA%\wispr\recordings\`.
- `wispr transcribe-file` uses ffmpeg when it is installed; without it, it reads
  16-bit PCM `.wav` files directly.

Managing the task:

```powershell
Get-Content "$env:LOCALAPPDATA\wispr\wispr.log" -Wait -Tail 20   # live logs
Stop-ScheduledTask  -TaskName 'Wispr Dictation'
Start-ScheduledTask -TaskName 'Wispr Dictation'
powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
```
