"""Settings, paths and the API key. Everything has a sane default; config.toml is optional."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def _config_dir() -> Path:
    return Path(os.environ.get("CHELYS_CONFIG_DIR", Path.home() / ".config" / "chelys"))


def _data_dir() -> Path:
    return Path(os.environ.get("CHELYS_DATA_DIR", Path.home() / ".local" / "share" / "chelys"))


@dataclass
class Settings:
    # Provider
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking: bool = False
    max_tokens: int = 2048
    request_timeout: float = 60.0
    # Loop limits
    max_iterations: int = 8
    tool_output_chars: int = 6000
    shell_timeout: float = 30.0
    read_file_bytes: int = 65536
    # How much past conversation is sent to the model, in characters (~4 chars per token).
    history_chars: int = 40000
    # Where file tools start and shell commands run.
    workdir: str = field(default_factory=lambda: str(Path.home()))
    config_dir: Path = field(default_factory=_config_dir)
    data_dir: Path = field(default_factory=_data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.sqlite3"

    @property
    def persona_path(self) -> Path:
        return self.config_dir / "persona.md"


class ConfigError(Exception):
    pass


def load_settings() -> Settings:
    s = Settings()
    path = s.config_dir / "config.toml"
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"could not read {path}: {e}") from e
        known = {f.name: f for f in fields(Settings)}
        for key, value in data.items():
            if key not in known or key in ("config_dir", "data_dir"):
                raise ConfigError(f"unknown setting {key!r} in {path}")
            current = getattr(s, key)
            ok = type(value) is type(current) or (type(current) is float and type(value) is int)
            if not ok:
                raise ConfigError(f"setting {key!r} in {path} should be {type(current).__name__}")
            setattr(s, key, type(current)(value))
    return s


def load_api_key(s: Settings) -> str:
    """Env var wins, then the key file. Never logged or printed."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    path = s.config_dir / "deepseek.key"
    try:
        key = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = ""
    except OSError as e:
        raise ConfigError(f"could not read key file {path}: {e}") from e
    if not key:
        raise ConfigError(
            f"no DeepSeek API key. Put it in {path} (chmod 600) or set DEEPSEEK_API_KEY."
        )
    return key
