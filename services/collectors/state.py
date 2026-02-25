"""Persists log file read positions across restarts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

import structlog

log = structlog.get_logger()

STATE_DIR = os.environ.get("COLLECTOR_STATE_DIR", "/data/collector-state")
STATE_FILE = os.path.join(STATE_DIR, "log_positions.json")

# M7: Periodic save settings
SAVE_INTERVAL_SECONDS = 30
SAVE_EVENT_THRESHOLD = 50


class LogPosition:
    """Tracks inode and byte offset for a single log file."""

    def __init__(self, inode: int = 0, offset: int = 0) -> None:
        self.inode = inode
        self.offset = offset

    def to_dict(self) -> dict:
        return {"inode": self.inode, "offset": self.offset}

    @classmethod
    def from_dict(cls, data: dict) -> LogPosition:
        return cls(inode=data.get("inode", 0), offset=data.get("offset", 0))


class CollectorState:
    """Manages resume state for all log collectors across restarts.
    
    M7: Saves periodically (every 30s or every 50 events) to prevent data loss.
    """

    def __init__(self) -> None:
        self._positions: Dict[str, LogPosition] = {}
        self._last_save_time: float = time.time()
        self._events_since_save: int = 0
        self._load()

    def _load(self) -> None:
        """Load saved positions from disk."""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                for filename, pos_data in data.items():
                    self._positions[filename] = LogPosition.from_dict(pos_data)
                log.info("state_loaded", files=len(self._positions))
            else:
                log.info("state_file_not_found", path=STATE_FILE)
        except Exception as e:
            log.error("state_load_error", error=str(e))
            self._positions = {}

    def save(self) -> None:
        """Persist positions to disk."""
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            data = {k: v.to_dict() for k, v in self._positions.items()}
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
            self._last_save_time = time.time()
            self._events_since_save = 0
        except Exception as e:
            log.error("state_save_error", error=str(e))

    def maybe_save(self) -> None:
        """M7: Save if enough time or events have elapsed."""
        self._events_since_save += 1
        now = time.time()
        if (
            self._events_since_save >= SAVE_EVENT_THRESHOLD
            or (now - self._last_save_time) >= SAVE_INTERVAL_SECONDS
        ):
            self.save()

    def get_position(self, filename: str) -> Optional[LogPosition]:
        """Get the saved position for a log file."""
        return self._positions.get(filename)

    def set_position(self, filename: str, inode: int, offset: int) -> None:
        """Update the position for a log file."""
        self._positions[filename] = LogPosition(inode=inode, offset=offset)
        self.maybe_save()

    def reset(self, filename: str) -> None:
        """Reset position for a file (e.g., when inode changes = file rotated)."""
        if filename in self._positions:
            del self._positions[filename]
