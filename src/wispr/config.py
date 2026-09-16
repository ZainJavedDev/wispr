from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOCKET_NAME = "wispr-gemini.sock"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse the small KEY=VALUE subset needed by this app."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_project_env(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    for key, value in _parse_env_file(env_path).items():
        os.environ.setdefault(key, value)


def default_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / DEFAULT_SOCKET_NAME
    return Path("/tmp") / f"wispr-gemini-{os.getuid()}.sock"


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str = "gemini-3.5-transcribe-live"
    transcription_mode: str = "SMART"
    language_codes: tuple[str, ...] = ()
    output_mode: str = "paste"
    audio_source: str | None = None
    debug: bool = False
    recordings_dir: Path | None = None
    socket_path: Path = Path("/tmp/wispr-gemini.sock")
    final_timeout_seconds: float = 8.0
    max_duration_seconds: int = 590

    @classmethod
    def from_env(cls) -> "Settings":
        load_project_env()
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is missing. Add it to .env or the service environment."
            )

        mode = os.environ.get("WISPR_TRANSCRIPTION_MODE", "SMART").upper()
        if mode not in {"SMART", "VERBATIM"}:
            raise ValueError("WISPR_TRANSCRIPTION_MODE must be SMART or VERBATIM")

        output_mode = os.environ.get("WISPR_OUTPUT", "paste").lower()
        if output_mode not in {"paste", "clipboard", "stdout"}:
            raise ValueError("WISPR_OUTPUT must be paste, clipboard, or stdout")

        languages = tuple(
            item.strip()
            for item in os.environ.get("WISPR_LANGUAGE_CODES", "").split(",")
            if item.strip()
        )
        audio_source = os.environ.get("WISPR_AUDIO_SOURCE", "").strip() or None
        socket_override = os.environ.get("WISPR_SOCKET_PATH", "").strip()
        debug = os.environ.get("WISPR_DEBUG", "").strip().lower() in {"1", "true", "yes"}
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")

        return cls(
            api_key=api_key,
            model=os.environ.get(
                "WISPR_GEMINI_MODEL", "gemini-3.5-transcribe-live"
            ).strip(),
            transcription_mode=mode,
            language_codes=languages,
            output_mode=output_mode,
            audio_source=audio_source,
            debug=debug,
            recordings_dir=state_home / "wispr" / "recordings" if debug else None,
            socket_path=Path(socket_override) if socket_override else default_socket_path(),
            final_timeout_seconds=float(
                os.environ.get("WISPR_FINAL_TIMEOUT_SECONDS", "8")
            ),
            max_duration_seconds=int(os.environ.get("WISPR_MAX_DURATION_SECONDS", "590")),
        )

