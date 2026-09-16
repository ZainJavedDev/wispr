import os
import unittest
from unittest.mock import patch

from wispr.output import paste_backend


class OutputTests(unittest.TestCase):
    def test_gnome_wayland_uses_native_backend(self) -> None:
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("wispr.output.shutil.which", return_value=None):
                self.assertEqual(paste_backend(), "gnome-remote-desktop")


if __name__ == "__main__":
    unittest.main()
