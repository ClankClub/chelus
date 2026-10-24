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
    request_timeout: float = 60.0      # longest wait for any single socket read
    request_deadline: float = 300.0    # longest a whole model request may take
    # Transient failures (429, 5xx, timeouts, network) are retried this many times.
    api_max_retries: int = 3
    # Ceiling for the boosted max_tokens used when a tool call is cut off (Hermes: 32768 when the
    # model limit is unknown; DeepSeek accepted up to 393216 in a live test on 2026-10-24).
    max_tokens_limit: int = 32768
    # Telegram only: after retries run out on an outage (5xx, timeout, network), wait and try
    # again this many times with a countdown in the chat (Hermes's auto-recovery). 0 disables.
    autorecover_cycles: int = 5
    # Fold older conversation into a summary instead of letting it fall out of the window.
    compaction: bool = True
    # Loop limits
    max_iterations: int = 8
    tool_output_chars: int = 6000
    shell_timeout: float = 30.0
    read_file_bytes: int = 65536
    # How much past conversation is sent to the model, in characters (~4 chars per token).
    history_chars: int = 40000
    # Skills. "ask": every skill write needs the owner's OK (once, this conversation, or always
    # for that skill), and background-review changes wait as pending. "off": writes apply
    # directly, as in Hermes's default.
    skill_approval: str = "ask"
    # Run the background skill review after this many tool-calling model steps (Hermes: 10).
    # 0 turns it off.
    review_interval: int = 10
    # Weekly cleanup of skills the background review created: stale after 14 idle days,
    # archived after 30 (Hermes's curator, without its LLM pass).
    curator: bool = True
    # Memory (owner only): two small files of facts in every conversation's prompt, written
    # by Chelys without asking (Hermes's default; the reply shows each write). Limits are in
    # characters (Hermes: 2,200 and 1,375). The memory review runs after this many owner
    # messages (Hermes: 10); 0 turns it off.
    memory: bool = True
    memory_char_limit: int = 2200
    user_char_limit: int = 1375
    memory_review_interval: int = 10
    # Full-text search over past conversations (owner only; needs SQLite with FTS5).
    session_search: bool = True
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
    if s.skill_approval not in ("ask", "off"):
        raise ConfigError(f"skill_approval in {path} must be \"ask\" or \"off\"")
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
