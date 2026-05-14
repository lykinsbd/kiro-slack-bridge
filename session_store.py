"""Persistent mapping of Slack thread_ts to ACP session IDs."""

import json
import logging
import time
from pathlib import Path
from collections import OrderedDict

logger = logging.getLogger(__name__)


class SessionStore:
    """Maps Slack thread_ts → ACP session_id with LRU eviction and persistence."""

    def __init__(self, path: Path, max_sessions: int = 100):
        self._path = path
        self._max = max_sessions
        self._sessions: OrderedDict[str, dict] = OrderedDict()
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                for entry in data:
                    self._sessions[entry["thread_ts"]] = entry
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupt session store, starting fresh")
                self._sessions.clear()

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(list(self._sessions.values()), indent=2))

    def get(self, thread_ts: str) -> str | None:
        """Get ACP session_id for a thread, or None if not mapped."""
        entry = self._sessions.get(thread_ts)
        if entry:
            self._sessions.move_to_end(thread_ts)
            return entry["session_id"]
        return None

    def put(self, thread_ts: str, session_id: str, cwd: str):
        """Store a thread→session mapping."""
        self._sessions[thread_ts] = {
            "thread_ts": thread_ts,
            "session_id": session_id,
            "cwd": cwd,
            "created_at": time.time(),
            "last_used": time.time(),
        }
        self._sessions.move_to_end(thread_ts)
        # Evict oldest if over limit
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)
        self._save()

    def touch(self, thread_ts: str):
        """Update last_used timestamp."""
        if thread_ts in self._sessions:
            self._sessions[thread_ts]["last_used"] = time.time()
            self._sessions.move_to_end(thread_ts)
            self._save()

    def remove(self, thread_ts: str):
        """Remove a mapping."""
        self._sessions.pop(thread_ts, None)
        self._save()

    def __len__(self) -> int:
        return len(self._sessions)
