""""Reset pairing" buttons: one for the bridge, one per overflow
bridge and standalone accessory."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HapBridgeConfigEntry
from .bridge import SIGNAL_UNITS
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HapBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ResetPairingButton(entry, entry.entry_id, None)])
    added: set[str] = set()

    @callback
    def _add_units() -> None:
        # The bridge only knows its standalone accessories (TVs, cameras) and
        # overflow bridges once started.
        new = [
            ResetPairingButton(entry, unit.id, unit.name)
            for unit in entry.runtime_data.units.values()
            if unit.id != entry.entry_id and unit.id not in added
        ]
        added.update(button.unit_id for button in new)
        async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_UNITS.format(entry.entry_id), _add_units)
    )
    _add_units()


class ResetPairingButton(ButtonEntity):
    """Unpairs every Home app controller and shows the pairing code again."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, entry: HapBridgeConfigEntry, unit_id: str, name: str | None) -> None:
        self._entry = entry
        self.unit_id = unit_id
        if name is None:
            self._attr_translation_key = "reset_pairing"
        else:
            self._attr_translation_key = "reset_accessory_pairing"
            self._attr_translation_placeholders = {"name": name}
        self._attr_unique_id = f"{unit_id}_reset_pairing"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HAP Bridge",
            model="Bridge",
        )

    @property
    def available(self) -> bool:
        return self.unit_id in self._entry.runtime_data.units

    async def async_press(self) -> None:
        self._entry.runtime_data.async_reset_pairing(self.unit_id)
