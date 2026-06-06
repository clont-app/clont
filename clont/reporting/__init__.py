"""Output rendering for collector results (table / json / csv).

Consumes the shared types in `core.models`, `finops.models` and
`monitoring.models` so every domain renders through the same path.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


def render(rows: list[Any], fmt: OutputFormat = OutputFormat.TABLE) -> str:
    """Render a list of dataclass records in the requested format."""
    raise NotImplementedError
