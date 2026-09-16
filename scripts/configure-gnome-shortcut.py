#!/usr/bin/env python3
from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path


SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
KEY = "custom-keybindings"
SHORTCUT_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/wispr/"
SHORTCUT_SCHEMA = (
    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"
    + SHORTCUT_PATH
)


def gsettings(*arguments: str) -> str:
    result = subprocess.run(
        ["gsettings", *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def main() -> None:
    if not shutil.which("gsettings"):
        raise SystemExit("gsettings is unavailable; configure the shortcut manually")

    raw_paths = gsettings("get", SCHEMA, KEY)
    paths = ast.literal_eval(raw_paths)
    if SHORTCUT_PATH not in paths:
        paths.append(SHORTCUT_PATH)
        gsettings("set", SCHEMA, KEY, repr(paths))

    command = str(Path.home() / ".local/bin/wispr") + " toggle"
    gsettings("set", SHORTCUT_SCHEMA, "name", "Wispr Dictation")
    gsettings("set", SHORTCUT_SCHEMA, "command", command)
    gsettings("set", SHORTCUT_SCHEMA, "binding", "<Ctrl><Alt>space")
    print(f"Configured Ctrl+Alt+Space to run: {command}")


if __name__ == "__main__":
    main()

