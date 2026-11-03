"""Diagnostics: the accessory tree each device was turned into, with its IDs."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HapBridgeConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HapBridgeConfigEntry
) -> dict[str, Any]:
    return {
        "options": dict(entry.options),
        "bridge": entry.runtime_data.diagnostics(),
    }
