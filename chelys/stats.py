"""Cheap local Telegram telemetry; never part of model context."""

from pathlib import Path
import json
import math
import os
import tempfile
import time

CPU_CACHE_PATH = Path("/data/local/tmp/chelys-telemetry.json")
DEFAULT_FIELDS = ("mem", "cpu", "bat")
ALLOWED_FIELDS = DEFAULT_FIELDS + ("swap", "up", "disk")


def _compact(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    if text in ("0", "-0"):
        return "0"
    return text


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False



class StatsPreferences:
    """Per-chat overrides. Commit memory only after the atomic disk write succeeds."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "telegram_stats.json"
        self.error = ""
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("invalid chat preferences")
            self.values = {}
            for key, value in values.items():
                if not key.isascii() or not key.isdigit():
                    raise ValueError("invalid chat ID")
                if type(value) is bool:  # Legacy mapping: preserve every enabled flag.
                    value = {"enabled": value, "fields": list(DEFAULT_FIELDS)}
                if (not isinstance(value, dict) or set(value) != {"enabled", "fields"}
                        or type(value["enabled"]) is not bool
                        or not isinstance(value["fields"], list)
                        or any(type(f) is not str or f not in ALLOWED_FIELDS for f in value["fields"])
                        or len(value["fields"]) != len(set(value["fields"]))):
                    raise ValueError("invalid chat preferences")
                self.values[key] = value
        except FileNotFoundError:
            self.values = {}
        except (OSError, ValueError) as e:
            self.values = {}
            self.error = f"Preferences unavailable ({type(e).__name__}); using default on."

    def enabled(self, chat_id: int) -> bool:
        return self.values.get(str(chat_id), {}).get("enabled", True)

    def fields(self, chat_id: int) -> tuple[str, ...]:
        return tuple(self.values.get(str(chat_id), {}).get("fields", DEFAULT_FIELDS))

    def set(self, chat_id: int, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self._save(chat_id, enabled, self.fields(chat_id))

    def update(self, chat_id: int, action: str, fields) -> None:
        fields = tuple(fields)
        if (action not in ("add", "remove", "reset")
                or (action == "reset" and fields)
                or (action != "reset" and not fields)
                or any(f not in ALLOWED_FIELDS for f in fields)):
            raise ValueError("invalid stats command or fields")
        current = list(self.fields(chat_id))
        if action == "reset":
            self._save(chat_id, True, DEFAULT_FIELDS)
            return
        if action == "add":
            current.extend(f for f in fields if f not in current)
        else:
            current = [f for f in current if f not in fields]
        current = [f for f in DEFAULT_FIELDS if f in current] + [f for f in current if f not in DEFAULT_FIELDS]
        self._save(chat_id, self.enabled(chat_id), current)

    def _save(self, chat_id: int, enabled: bool, fields) -> None:
        values = {**self.values, str(chat_id): {"enabled": enabled, "fields": list(fields)}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".telegram_stats-", delete=False) as f:
                temp = Path(f.name)
                os.chmod(temp, 0o600)
                json.dump(values, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.values = values
        self.error = ""


class RuntimeStats:
    """Five-second monotonic cache, including failures; no polling worker."""

    def __init__(self, workdir: str | Path | None = None):
        self.workdir = Path(workdir) if workdir is not None else Path.home()
        self._sampled_at: float | None = None
        self._metrics: dict[str, str] = {}
        self._details: list[str] = []
        self._disk_at: float | None = None
        self._disk_detail = ""
        self._cpu_timestamp: float | None = None

    def _sample(self) -> None:
        now = time.monotonic()
        cpu_fresh = self._cpu_timestamp is None or -5 <= time.time() - self._cpu_timestamp <= 30
        if self._sampled_at is not None and now - self._sampled_at < 5 and cpu_fresh:
            return
        self._sampled_at = now
        values = {}
        read_error = ""
        keys = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, _, value = line.partition(":")
                if key not in keys:
                    continue
                fields = value.split()
                parsed = None
                if (len(fields) == 2 and fields[1] == "kB" and fields[0].isascii()
                        and fields[0].isdigit() and len(fields[0]) <= 20):
                    counter = int(fields[0])
                    if counter <= 2**64 - 1:
                        parsed = counter
                # Duplicate or malformed counters cannot be trusted.
                values[key] = None if key in values else parsed
        except (OSError, ValueError) as e:
            read_error = f"cannot read /proc/meminfo ({type(e).__name__})"
        metrics, details = {}, []
        scale, unit = 1024**2, "G"
        for label, total_key, free_key in (("RAM", "MemTotal", "MemAvailable"),
                                          ("Swap", "SwapTotal", "SwapFree")):
            total, free = values.get(total_key), values.get(free_key)
            if read_error or total is None or free is None or not 0 <= free <= total or (label == "RAM" and total == 0):
                details.append(f"{label} unavailable: {read_error or 'missing, malformed or inconsistent ' + total_key + '/' + free_key}.")
                continue
            tag = "mem" if label == "RAM" else "swap"
            metrics[tag] = f"{tag} {_compact((total - free) / scale)}/{_compact(total / scale)}{unit}"
            details.append(f"{total_key}={total} kB; {free_key}={free} kB.")
        cpu_parts = []
        battery_parts = []
        self._cpu_timestamp = None
        try:
            sample = json.loads(CPU_CACHE_PATH.read_text(encoding="ascii"))
            if not isinstance(sample, dict) or type(sample.get("version")) is not int or sample["version"] != 1:
                raise ValueError("unsupported cache")
            timestamp = sample.get("timestamp")
            if not _finite_number(timestamp):
                raise ValueError("invalid timestamp")
            age = time.time() - timestamp
            if not -5 <= age <= 30:
                raise ValueError("stale or future cache")
            self._cpu_timestamp = timestamp
            for key, label, unit in (("cpu_percent", "CPU", "%"), ("cpu_temp_c", "CPU temp", "°C")):
                value = sample.get(key)
                low, high = (0, 100) if key == "cpu_percent" else (-20, 150)
                if _finite_number(value) and low <= value <= high:
                    cpu_parts.append(_compact(value) + "%" if key == "cpu_percent" else f"{value:.0f}°C")
                    details.append(f"{key}={value} {unit}.")
                else:
                    details.append(f"{label} unavailable: missing or invalid {key}.")
            details.append(f"CPU source: optional privileged cache {CPU_CACHE_PATH}; age {age:.0f}s. CPU temperature: type 0/AP, not battery.")
            for key, low, high, unit in (("battery_percent", 0, 100, "%"),
                                         ("battery_temp_c", -20, 100, "°C")):
                value = sample.get(key)
                if _finite_number(value) and low <= value <= high:
                    battery_parts.append(f"{value:.0f}{unit}")
                    details.append(f"{key}={value} {unit}.")
                else:
                    details.append(f"Battery unavailable: missing or invalid {key}.")
            details.append("Battery source: dumpsys battery via optional privileged cache; independent of CPU thermal sensor.")
            uptime = sample.get("uptime_seconds")
            if _finite_number(uptime) and uptime >= 0:
                minutes = int(uptime) // 60
                hours, minute = divmod(minutes, 60)
                metrics["up"] = f"up {hours}h{minute:02d}"
                details.append(f"uptime_seconds={uptime} s; source: device uptime from optional privileged cache (not process lifetime).")
            else:
                details.append("Uptime unavailable: missing or invalid uptime_seconds.")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            details.append("CPU unavailable: optional privileged cache missing, unreadable, malformed or stale.")
            details.append("Battery and uptime unavailable: optional privileged cache missing, unreadable, malformed or stale.")
        if cpu_parts:
            metrics["cpu"] = "cpu " + " - ".join(cpu_parts)
        if battery_parts:
            metrics["bat"] = "bat " + " - ".join(battery_parts)
        # Disk has its own lazy cache so changing field selection is immediate.
        if "disk" in self._metrics:
            metrics["disk"] = self._metrics["disk"]
        self._metrics = metrics
        self._details = details

    def _sample_disk(self) -> None:
        now = time.monotonic()
        if self._disk_at is not None and now - self._disk_at < 5:
            return
        self._disk_at = now
        self._metrics.pop("disk", None)
        try:
            fs = os.statvfs(self.workdir)
            if fs.f_frsize <= 0 or fs.f_blocks <= 0 or not 0 <= fs.f_bfree <= fs.f_blocks:
                raise ValueError("inconsistent filesystem counters")
            used = (fs.f_blocks - fs.f_bfree) * fs.f_frsize
            total = fs.f_blocks * fs.f_frsize
            self._metrics["disk"] = f"disk {_compact(used / 1024**3)}/{_compact(total / 1024**3)}G"
            self._disk_detail = (f"Disk source: os.statvfs({self.workdir}); used = (f_blocks - f_bfree) * f_frsize = {used} bytes; "
                                 f"total = f_blocks * f_frsize = {total} bytes (actual containing filesystem).")
        except (OSError, ValueError, OverflowError) as e:
            self._disk_detail = f"Disk unavailable: os.statvfs({self.workdir}) ({type(e).__name__})."

    def footer(self, fields=DEFAULT_FIELDS) -> str:
        if not fields:
            return ""
        self._sample()
        if "disk" in fields:
            self._sample_disk()
        return " | ".join(self._metrics[f] for f in fields if f in self._metrics)

    def info(self, fields=DEFAULT_FIELDS) -> str:
        self._sample()
        self._sample_disk()
        footer = " | ".join(self._metrics[f] for f in fields if f in self._metrics)
        return "\n".join(([footer] if footer else []) + [
            "Selected fields: " + (", ".join(fields) or "none"),
            "RAM used = MemTotal - MemAvailable; total = MemTotal. Source: /proc/meminfo.",
            "Swap used = SwapTotal - SwapFree; total = SwapTotal. Source: /proc/meminfo.",
            "G = GiB (2^30 bytes); meminfo kB = KiB (2^10 bytes). Ratios and CPU percent: at most one decimal; temperatures/battery percent: rounded integers.",
            "Uptime: whole elapsed hours and minutes, minutes truncated (15h10 = 15 hours 10 minutes).",
            "Read cache: 5s monotonic; privileged cache age limit: 30s, future tolerance: 5s.",
        ] + self._details + [self._disk_detail])
