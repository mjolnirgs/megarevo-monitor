"""
Shared paths, config loading, and logging setup for megarevo-monitor.

Everything that writes to disk resolves under /var/lib/megarevo-monitor
(state, sqlite, discover dumps) or /var/log/megarevo-monitor (rotated
logs) — both expected to be the USB SSD mount, not the SD card. Nothing
in here writes anywhere else.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

VAR_DIR = Path(os.environ.get("MEGAREVO_VAR_DIR", "/var/lib/megarevo-monitor"))
LOG_DIR = Path(os.environ.get("MEGAREVO_LOG_DIR", "/var/log/megarevo-monitor"))
DISCOVER_DIR = VAR_DIR / "discover"
STATE_FILE = VAR_DIR / "state.json"
# Read by NUT's dummy-ups driver in "dummy" mode (a plain data file, not a
# .seq sequence file) — the driver watches this file's mtime and reloads
# whenever it changes, letting NUT treat this poller's telemetry as if it
# came from a real UPS talking to upsd. Lives under VAR_DIR like
# STATE_FILE so it's covered by the same systemd ReadWritePaths scoping.
NUT_DATA_FILE = VAR_DIR / "nut-dummy.dev"
CONFIG_FILE = Path(os.environ.get("MEGAREVO_CONFIG", "/etc/megarevo-monitor/config.yaml"))


def ensure_dirs() -> None:
    for d in (VAR_DIR, LOG_DIR, DISCOVER_DIR):
        d.mkdir(parents=True, exist_ok=True, mode=0o750)


def setup_logging(name: str, level: str = "INFO") -> logging.Logger:
    """Rotating file handler under LOG_DIR, plus stderr for interactive/journald use."""
    ensure_dirs()
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:
        return logger  # already configured (e.g. re-imported)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (write to temp file in same dir, then rename) so a
    reader (e.g. the Nagios check script) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Same atomic-write rationale as atomic_write_json, for a plain-text
    consumer (the NUT dummy-ups data file). mode defaults to world-readable
    since tempfile.mkstemp's default (0600) would leave it unreadable by
    the `nut` user the driver normally runs as, and this data isn't
    sensitive — it's the same telemetry already in state.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix or ".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@dataclass
class SerialConfig:
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 2.0


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found at {path}. Copy etc/config.example.yaml there and fill it in "
            f"(or set MEGAREVO_CONFIG to point elsewhere)."
        )
    with open(path) as f:
        return yaml.safe_load(f)


def serial_config_from(cfg: dict[str, Any]) -> SerialConfig:
    s = cfg.get("serial", {})
    return SerialConfig(
        port=s["port"],
        baudrate=s.get("baudrate", 9600),
        parity=s.get("parity", "N"),
        stopbits=s.get("stopbits", 1),
        bytesize=s.get("bytesize", 8),
        timeout=s.get("timeout", 2.0),
    )
