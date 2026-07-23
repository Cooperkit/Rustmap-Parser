"""Shared filtering for configurable position representations."""

from __future__ import annotations

from .config import TransformOptions


POSITION_FIELD_NAMES = ("local_position", "position", "map_position")


def exported_position_fields(options: TransformOptions) -> list[str]:
    """Return enabled position field names in stable schema order."""
    return [name for name in POSITION_FIELD_NAMES if getattr(options, name)]


def strip_disabled_position_fields(value, options: TransformOptions) -> None:
    """Recursively remove disabled exact-name position fields in place."""
    enabled = set(exported_position_fields(options))
    _strip_disabled_position_fields(value, enabled)


def _strip_disabled_position_fields(value, enabled: set[str]) -> None:
    if isinstance(value, dict):
        for name in POSITION_FIELD_NAMES:
            if name not in enabled:
                value.pop(name, None)
        for child in value.values():
            _strip_disabled_position_fields(child, enabled)
    elif isinstance(value, list):
        for child in value:
            _strip_disabled_position_fields(child, enabled)
