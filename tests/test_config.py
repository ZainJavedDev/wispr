import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wispr.config import Settings, _parse_env_file


class ConfigTests(unittest.TestCase):
    def test_parse_env_file_supports_quotes_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\nGEMINI_API_KEY='secret'\nexport WISPR_OUTPUT=clipboard\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _parse_env_file(path),
                {"GEMINI_API_KEY": "secret", "WISPR_OUTPUT": "clipboard"},
            )

    def test_settings_validate_output_mode(self) -> None:
        environment = {"GEMINI_API_KEY": "secret", "WISPR_OUTPUT": "invalid"}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "WISPR_OUTPUT"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()

