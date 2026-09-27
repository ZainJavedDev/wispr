# Wispr Gemini

System-wide voice dictation for Ubuntu using Gemini Live Transcription. Press a
global shortcut, speak, press it again, and the finalized transcript is copied
and pasted into the focused application.

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
