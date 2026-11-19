"""last-sent times on disk, so a restart doesn't re-alert every open condition."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# a key not seen for this long is a condition that went away
_KEEP = timedelta(days=90)


class SentStore:
    """channel name -> event key -> last delivery time, kept in one json file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, datetime]] = self._load()

    def _load(self) -> dict[str, dict[str, datetime]]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # a broken file costs one round of repeats, not the agent
            log.warning("ignoring unreadable alert state %s: %s", self.path, exc)
            return {}
        cutoff = datetime.now(UTC) - _KEEP
        data: dict[str, dict[str, datetime]] = {}
        for channel, keys in (raw.items() if isinstance(raw, dict) else ()):
            if not isinstance(keys, dict):
                continue
            for key, stamp in keys.items():
                try:
                    when = datetime.fromisoformat(stamp)
                except (TypeError, ValueError):
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                if when >= cutoff:
                    data.setdefault(channel, {})[key] = when
        return data

    def for_channel(self, channel: str) -> dict[str, datetime]:
        return dict(self._data.get(channel, {}))

    def mark(self, channel: str, key: str, when: datetime) -> None:
        self._data.setdefault(channel, {})[key] = when
        self._save()

    def _save(self) -> None:
        payload = {
            ch: {k: t.isoformat() for k, t in keys.items()} for ch, keys in self._data.items()
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)  # never a half-written file
        except OSError as exc:
            # delivery already happened, losing the stamp only means a repeat later
            log.warning("could not save alert state %s: %s", self.path, exc)
