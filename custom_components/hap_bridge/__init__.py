"""HAP Bridge: a HomeKit bridge whose accessories integrations can describe."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started

from .bridge import HapBridge

_LOGGER = logging.getLogger(__name__)

type HapBridgeConfigEntry = ConfigEntry[HapBridge]


async def async_setup_entry(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> bool:
    bridge = HapBridge(hass, entry)
    entry.runtime_data = bridge

    async def _start(_hass: HomeAssistant) -> None:
        # Wait for startup so every integration's entities and states exist.
        try:
            await bridge.async_start()
        except Exception:
            _LOGGER.exception("HAP bridge %s failed to start", entry.title)

    entry.async_on_unload(async_at_started(hass, _start))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return True


async def _async_reload(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
