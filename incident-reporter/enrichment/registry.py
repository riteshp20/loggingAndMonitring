"""Service registry — maps service names to on-call ownership metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """Loads a JSON file that maps service names to on-call info.

    Falls back to the ``"default"`` entry for unknown services, and returns
    ``None`` when neither the service nor a default entry exists.
    """

    def __init__(self, registry_path: str = "service_registry.json") -> None:
        self._data: dict = {}
        self._load(registry_path)

    def _load(self, path: str) -> None:
        try:
            text = Path(path).read_text(encoding="utf-8")
            self._data = json.loads(text)
            logger.info(
                "Service registry loaded: %d entries from %s",
                len(self._data),
                path,
            )
        except FileNotFoundError:
            logger.warning(
                "Service registry not found at %s; all lookups will return None",
                path,
            )
        except json.JSONDecodeError as exc:
            logger.error("Service registry JSON parse error in %s: %s", path, exc)

    def get_oncall_owner(self, service: str) -> Optional[dict]:
        """Return on-call info for *service*, falling back to ``"default"``."""
        return self._data.get(service) or self._data.get("default")

    def all_services(self) -> list[str]:
        """Return all registered service names (excludes the ``"default"`` key)."""
        return [k for k in self._data if k != "default"]
