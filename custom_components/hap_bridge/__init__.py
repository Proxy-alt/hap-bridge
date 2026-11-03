"""HAP Bridge: a HomeKit bridge whose accessories integrations can describe."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started

from .bridge import HapBridge
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type HapBridgeConfigEntry = ConfigEntry[HapBridge]


async def async_setup_entry(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> bool:
    bridge = HapBridge(hass, entry)
    entry.runtime_data = bridge

    async def _start(_hass: HomeAssistant) -> None:
        # Wait for startup so every integration's entities and states exist.
        try:
            await bridge.async_start()
        except Exception as err:
            _LOGGER.exception("HAP bridge %s failed to start", entry.title)
            await bridge.async_stop()
            # The entry is already loaded by now, so a repair is the only place
            # the failure shows up outside the log.
            ir.async_create_issue(
                hass,
                DOMAIN,
                _issue_id(entry),
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="start_failed",
                translation_placeholders={"name": entry.title, "error": str(err) or repr(err)},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))

    entry.async_on_unload(async_at_started(hass, _start))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
    return True


def _issue_id(entry: ConfigEntry) -> str:
    return f"start_failed_{entry.entry_id}"


async def _async_reload(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
