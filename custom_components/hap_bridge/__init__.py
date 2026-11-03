"""HAP Bridge: a HomeKit bridge whose accessories integrations can describe."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.start import async_at_started

from .bridge import HapBridge
from .const import DOMAIN, REGISTRY_RELOAD_COOLDOWN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON]

# Registry changes that alter what gets exposed or how it's named/bound.
_ENTITY_CHANGES = {
    "entity_id", "device_id", "disabled_by", "hidden_by", "entity_category",
    "name", "device_class", "original_device_class", "original_name",
}
_DEVICE_CHANGES = {"name", "name_by_user", "disabled_by"}

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
            _async_reload_on_registry_changes(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(async_at_started(hass, _start))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
    return unloaded


def _issue_id(entry: ConfigEntry) -> str:
    return f"start_failed_{entry.entry_id}"


async def _async_reload(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_reload_on_registry_changes(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> None:
    """Rebuild the bridge when devices or entities come, go or get renamed.

    Debounced, since adding one device usually means a burst of entity events.
    """

    @callback
    def _reload() -> None:
        hass.config_entries.async_schedule_reload(entry.entry_id)

    # _reload must stay a @callback: Debouncer runs plain functions in a thread.
    debouncer = Debouncer(
        hass, _LOGGER, cooldown=REGISTRY_RELOAD_COOLDOWN, immediate=False, function=_reload
    )

    @callback
    def _entity_filter(data: er.EventEntityRegistryUpdatedData) -> bool:
        if data["action"] != "update":
            return True
        return not _ENTITY_CHANGES.isdisjoint(data["changes"])

    @callback
    def _device_filter(data: dr.EventDeviceRegistryUpdatedData) -> bool:
        if data["action"] == "create":
            return False  # its entities will follow, and those count
        if data["action"] != "update":
            return True
        return not _DEVICE_CHANGES.isdisjoint(data["changes"])

    @callback
    def _changed(event: Event[Any]) -> None:
        if event.event_type == er.EVENT_ENTITY_REGISTRY_UPDATED:
            entity = er.async_get(hass).async_get(event.data["entity_id"])
            if entity is not None and entity.platform == DOMAIN:
                return  # our own button
        debouncer.async_schedule_call()

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _changed, _entity_filter)
    )
    entry.async_on_unload(
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _changed, _device_filter)
    )
    entry.async_on_unload(debouncer.async_shutdown)
