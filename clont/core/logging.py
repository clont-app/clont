"""Logging setup for the daemon.

The verbosity of clont's *own* operational logging (auth, cycle progress,
errors) is driven by the `log_level` field in the YAML config — see
`core.config`. This is distinct from `channels.log.min_severity`, which gates
which detected *events* reach the log channel.
"""

from __future__ import annotations

import logging

# Valid `log_level` values, e.g. for config validation and error messages.
LEVEL_NAMES = ("debug", "info", "warning", "error", "critical")


def resolve_level(name: str) -> int:
    """Map a config level name ("info", "debug", ...) to a logging constant."""
    return logging.getLevelNamesMapping()[name.strip().upper()]


def configure(level: int = logging.INFO) -> None:
    """Install the root handler. Safe to call once at startup."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def set_level(level: int) -> None:
    """Adjust verbosity after `configure()` (e.g. once config is loaded)."""
    logging.getLogger().setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)