"""Settings, paths and the API key. Everything has a sane default; config.toml is optional."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def _migrate(old: Path, new: Path) -> Path:
    """The project was called Chelys. Move a Chelys-era folder to its Chelus name once and
    leave a symlink behind, so skills and scripts that name the old path keep working.
    If the move can't happen, keep using the old folder rather than starting empty."""
    if new.exists() or not old.is_dir() or old.is_symlink():
        return new if new.exists() or not old.is_dir() else old
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
    except OSError:
        return new if new.exists() else old  # another process won the race, or a cross-device move
    try:
        old.symlink_to(new, target_is_directory=True)
    except OSError:
        pass
    return new


def _dir(env: str, parent: Path) -> Path:
    for name in (f"CHELUS_{env}", f"CHELYS_{env}"):  # the old variable still works
        if os.environ.get(name):
            return Path(os.environ[name])
    return _migrate(parent / "chelys", parent / "chelus")


def _config_dir() -> Path:
    return _dir("CONFIG_DIR", Path.home() / ".config")


def _data_dir() -> Path:
    return _dir("DATA_DIR", Path.home() / ".local" / "share")


def _cache_dir() -> Path:
    """Throwaway files the model may read back (saved tool output). Not off limits."""
    return Path(os.environ.get("CHELUS_CACHE_DIR") or Path.home() / ".cache" / "chelus")


@dataclass
class Settings:
    # "full": every tool and the whole prompt, for capable (cloud) models. "local": a small
    # model on the phone itself through llama-server: five core tools with short schemas, a
    # short prompt, no skills, memory, notes, web or cron tools, and no background reviews.
    # Settings that local mode needs to differ get LOCAL_DEFAULTS unless config.toml sets them.
    profile: str = "full"
    # Provider. "deepseek" sends DeepSeek's thinking switch and replays reasoning_content;
    # "openai" is any other OpenAI-compatible chat-completions API (OpenRouter, Groq,
    # llama-server) and sends only the standard fields.
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    # Where the key comes from: this environment variable, else this file in config_dir.
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_key_file: str = "deepseek.key"
    # DeepSeek's thinking switch; on the local profile, llama-server's (Qwen's enable_thinking).
    thinking: bool = False
    reasoning_effort: str = "high"  # DeepSeek's default; used only when thinking is enabled
    # Sampling temperature sent with each request; below 0 sends none (the provider's default).
    temperature: float = -1.0
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
    # Longer tool output is saved to a file the model pages with read_file (spill.py).
    tool_output_chars: int = 6000
    # run_shell's foreground limit and default. A longer run goes to the background (jobs.py).
    shell_timeout: float = 120.0
    read_file_bytes: int = 65536  # no longer used (read_file pages by lines); old configs still load
    # web_fetch and web_search (owner only, no approval; web.py). web_chars is how much of a
    # page the model sees at once; the rest is saved for paging.
    web: bool = True
    web_chars: int = 8000
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
    # by Chelus without asking (Hermes's default; the reply shows each write). Limits are in
    # characters (Hermes: 2,200 and 1,375). The memory review runs after this many owner
    # messages (Hermes: 10); 0 turns it off.
    memory: bool = True
    memory_char_limit: int = 2200
    user_char_limit: int = 1375
    memory_review_interval: int = 10
    # Full-text search over past conversations (owner only; needs SQLite with FTS5).
    session_search: bool = True
    # Notes (owner only; docs/NOTES.md): a folder of linked Markdown notes Chelus searches,
    # reads and writes, in the format Obsidian and other editors read. notes_dir "" means
    # <data_dir>/notes; ~ is expanded, and a relative path starts at workdir. A folder in
    # shared storage lets other apps (Markor, Syncthing) open it, and read it.
    notes: bool = True
    notes_dir: str = ""
    # Where file tools start and shell commands run.
    workdir: str = field(default_factory=lambda: str(Path.home()))
    config_dir: Path = field(default_factory=_config_dir)
    data_dir: Path = field(default_factory=_data_dir)
    cache_dir: Path = field(default_factory=_cache_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.sqlite3"

    @property
    def persona_path(self) -> Path:
        return self.config_dir / "persona.md"

    @property
    def persona_local_path(self) -> Path:
        return self.config_dir / "persona-local.md"

    @property
    def notes_root(self) -> Path:
        raw = self.notes_dir.strip()
        if not raw:
            return self.data_dir / "notes"
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else Path(self.workdir) / p


class ConfigError(Exception):
    pass


# profile = "local" changes these defaults; a value set in config.toml still wins. A phone
# reads its prompt at about 9 tokens a second, so history and tool output are kept small,
# waits are long, and compaction (a long summary call on the same slow model) is off. A
# local server needs no key.
LOCAL_DEFAULTS = {
    "provider": "openai",
    "base_url": "http://127.0.0.1:8081/v1",
    "model": "local",
    # Thinking on Qwen3.5-0.8B: 22 of 30 checks passed on bench scenarios never used for tuning,
    # against 15 of 50 without it, for about 190 output tokens a model call instead of 27
    # (some 35 s more per call on the phone). Temperature follows it (apply_profile).
    "thinking": True,
    "api_key_env": "",
    "api_key_file": "local.key",
    "max_tokens": 1536,
    "max_tokens_limit": 2048,
    "request_timeout": 900.0,
    "request_deadline": 1800.0,
    "api_max_retries": 1,
    "autorecover_cycles": 0,
    "compaction": False,
    "max_iterations": 6,
    "tool_output_chars": 2000,
    "history_chars": 8000,
}


def load_settings() -> Settings:
    s = Settings()
    path = s.config_dir / "config.toml"
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"could not read {path}: {e}") from e
        known = {f.name: f for f in fields(Settings)}
        for key, value in data.items():
            if key not in known or key in ("config_dir", "data_dir", "cache_dir"):
                raise ConfigError(f"unknown setting {key!r} in {path}")
            current = getattr(s, key)
            ok = type(value) is type(current) or (type(current) is float and type(value) is int)
            if not ok:
                raise ConfigError(f"setting {key!r} in {path} should be {type(current).__name__}")
            setattr(s, key, type(current)(value))
    if s.profile not in ("full", "local"):
        raise ConfigError(f"profile in {path} must be \"full\" or \"local\"")
    apply_profile(s, set(data))
    if s.skill_approval not in ("ask", "off"):
        raise ConfigError(f"skill_approval in {path} must be \"ask\" or \"off\"")
    if s.provider not in ("deepseek", "openai"):
        raise ConfigError(f"provider in {path} must be \"deepseek\" or \"openai\"")
    if not s.api_key_file or Path(s.api_key_file).name != s.api_key_file:
        raise ConfigError(f"api_key_file in {path} must be a plain file name in {s.config_dir}")
    if s.reasoning_effort not in ("low", "high", "max"):
        raise ConfigError(f"reasoning_effort in {path} must be \"low\", \"high\" or \"max\"")
    if s.notes_dir.strip():
        # The notes folder is off limits to the file tools, so it must not swallow the
        # working directory or Chelus's own folders.
        root = os.path.realpath(s.notes_root)
        for other in (s.workdir, s.config_dir, Path.home()):
            o = os.path.realpath(other)
            try:
                inside = os.path.commonpath([os.path.normcase(o), os.path.normcase(root)]) == os.path.normcase(root)
            except ValueError:
                inside = False
            if inside:
                raise ConfigError(f"notes_dir in {path} must be its own folder, not {other} or a folder that holds it")
    return s


def apply_profile(s: Settings, given: set[str]) -> None:
    """Fill in the local profile's defaults for every setting not in `given`."""
    if s.profile == "local":
        for key, value in LOCAL_DEFAULTS.items():
            if key not in given:
                setattr(s, key, value)
        if "temperature" not in given:
            # Qwen's advice for thinking; without it, low was best for tool choice on
            # Qwen3.5-0.8B (53 of 64 first calls right at 0.2, 46 at 0.7).
            s.temperature = 0.6 if s.thinking else 0.2


def load_api_key(s: Settings) -> str:
    """Env var wins, then the key file. Never logged or printed. The local profile may run
    without a key (llama-server needs none unless started with --api-key): "" then."""
    key = os.environ.get(s.api_key_env, "").strip() if s.api_key_env else ""
    if key:
        return key
    path = s.config_dir / s.api_key_file
    try:
        key = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = ""
    except OSError as e:
        raise ConfigError(f"could not read key file {path}: {e}") from e
    if not key and s.profile == "local":
        return ""
    if not key:
        hint = f" or set {s.api_key_env}" if s.api_key_env else ""
        raise ConfigError(f"no API key. Put it in {path} (chmod 600){hint}.")
    return key
