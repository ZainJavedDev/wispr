#!/usr/bin/python3
"""Send Shift+Insert through GNOME Mutter's session-scoped input API."""

from __future__ import annotations

from gi.repository import Gio, GLib


DESTINATION = "org.gnome.Mutter.RemoteDesktop"
ROOT_PATH = "/org/gnome/Mutter/RemoteDesktop"
ROOT_INTERFACE = "org.gnome.Mutter.RemoteDesktop"
SESSION_INTERFACE = "org.gnome.Mutter.RemoteDesktop.Session"
SHIFT_L = 0xFFE1
INSERT = 0xFF63


def call(
    bus: Gio.DBusConnection,
    path: str,
    interface: str,
    method: str,
    parameters: GLib.Variant | None = None,
    reply_type: GLib.VariantType | None = None,
) -> GLib.Variant:
    return bus.call_sync(
        DESTINATION,
        path,
        interface,
        method,
        parameters,
        reply_type,
        Gio.DBusCallFlags.NONE,
        5000,
        None,
    )


def main() -> None:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    response = call(
        bus,
        ROOT_PATH,
        ROOT_INTERFACE,
        "CreateSession",
        reply_type=GLib.VariantType.new("(o)"),
    )
    session_path = response.unpack()[0]

    try:
        call(bus, session_path, SESSION_INTERFACE, "Start")
        for keysym, pressed in (
            (SHIFT_L, True),
            (INSERT, True),
            (INSERT, False),
            (SHIFT_L, False),
        ):
            call(
                bus,
                session_path,
                SESSION_INTERFACE,
                "NotifyKeyboardKeysym",
                GLib.Variant("(ub)", (keysym, pressed)),
            )
    finally:
        try:
            call(bus, session_path, SESSION_INTERFACE, "Stop")
        except GLib.Error:
            pass


if __name__ == "__main__":
    main()

