"""Exception hierarchy for clont."""

from __future__ import annotations


class ClontError(Exception):
    """Base class for all clont errors."""


class ConfigError(ClontError):
    """Raised when a profile / configuration is missing or invalid."""


class AuthError(ClontError):
    """Raised when a provider cannot authenticate or assume its RO role."""


class PreflightError(ClontError):
    """Raised when required read-only permissions are missing."""