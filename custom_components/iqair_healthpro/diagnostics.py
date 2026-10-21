"""Diagnostics download: decoded state plus the raw dataPoints."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import IQAirConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IQAirConfigEntry
) -> dict[str, Any]:
    c = entry.runtime_data
    return {
        "options": dict(entry.options),
        "connected": c.connected,
        "last_update": c.last_update,
        "state": {k: list(v) if isinstance(v, tuple) else v for k, v in c.data.items()},
        "raw_datapoints": {f"0x{k:04x}": v.hex() for k, v in sorted(c.raw.items())},
    }
