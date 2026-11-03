"""A "Reset pairing" button on the bridge's own device."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HapBridgeConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HapBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ResetPairingButton(entry)])


class ResetPairingButton(ButtonEntity):
    """Unpairs every Home app controller and shows the pairing code again."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = "reset_pairing"

    def __init__(self, entry: HapBridgeConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reset_pairing"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HAP Bridge",
            model="Bridge",
        )

    @property
    def available(self) -> bool:
        return self._entry.runtime_data.started

    async def async_press(self) -> None:
        self._entry.runtime_data.async_reset_pairing()
