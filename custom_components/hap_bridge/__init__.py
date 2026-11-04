"""HAP Bridge: a HomeKit bridge whose accessories integrations can describe."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.integration_platform import async_process_integration_platforms
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from . import backup, issues
from . import settings
from .bridge import HapBridge
from .const import DOMAIN, REGISTRY_RELOAD_COOLDOWN
from .preview import async_preview
from .shims import DATA_PACKS, PACK_PLATFORM, read_pack

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON]

# Registry changes that alter what gets exposed or how it's named/bound.
_ENTITY_CHANGES = {
    "entity_id", "device_id", "disabled_by", "hidden_by", "entity_category",
    "name", "device_class", "original_device_class", "original_name",
}
_DEVICE_CHANGES = {"name", "name_by_user", "disabled_by"}

type HapBridgeConfigEntry = ConfigEntry[HapBridge]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PREVIEW_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("device_id"): cv.string,
            vol.Optional("entity_id"): cv.entity_id,
            vol.Optional("config_entry_id"): cv.string,
        }
    ),
    cv.has_at_least_one_key("device_id", "entity_id"),
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    @callback
    def _register_pack(hass: HomeAssistant, domain: str, platform: Any) -> None:
        pack = read_pack(domain, platform)
        hass.data.setdefault(DATA_PACKS, {})[domain] = pack
        _LOGGER.info("Shim pack %s: %s shims", domain, len(pack.shims))
        # A pack set up after the bridges started: rebuild them to use it.
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            if entry.runtime_data.started:
                hass.config_entries.async_schedule_reload(entry.entry_id)

    await async_process_integration_platforms(hass, PACK_PLATFORM, _register_pack)

    async def _preview(call: ServiceCall) -> ServiceResponse:
        entry = _preview_entry(hass, call.data.get("config_entry_id"))
        return await async_preview(
            hass, entry, device_id=call.data.get("device_id"), entity_id=call.data.get("entity_id")
        )

    hass.services.async_register(
        DOMAIN, "preview", _preview, schema=PREVIEW_SCHEMA, supports_response=SupportsResponse.ONLY
    )

    async def _list_pairings(call: ServiceCall) -> ServiceResponse:
        entry = _preview_entry(hass, call.data.get("config_entry_id"))
        return {"units": entry.runtime_data.pairings()}

    async def _remove_pairing(call: ServiceCall) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if entry_id := call.data.get("config_entry_id"):
            entries = [e for e in entries if e.entry_id == entry_id]
        for entry in entries:
            if entry.runtime_data.async_remove_pairing(call.data["pairing_id"]):
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_pairing",
            translation_placeholders={"pairing_id": call.data["pairing_id"]},
        )

    async def _room_assignments(call: ServiceCall) -> ServiceResponse:
        entry = _preview_entry(hass, call.data.get("config_entry_id"))
        return entry.runtime_data.room_assignments()

    def _by_serial(call: ServiceCall) -> Any:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if entry_id := call.data.get("config_entry_id"):
            entries = [e for e in entries if e.entry_id == entry_id]
        for entry in entries:
            if (spec := entry.runtime_data.accessory_by_serial(call.data["serial"])) is not None:
                return spec
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_accessory",
            translation_placeholders={"serial": call.data["serial"]},
        )

    async def _accessory_settings(call: ServiceCall) -> ServiceResponse:
        return settings.describe(hass, _by_serial(call), call.data["serial"])

    async def _set_accessory_setting(call: ServiceCall) -> None:
        await settings.async_set(
            hass, _by_serial(call), call.data["entity_id"], call.data.get("value"))

    by_serial = {vol.Required("serial"): cv.string, vol.Optional("config_entry_id"): cv.string}
    hass.services.async_register(
        DOMAIN, "accessory_settings", _accessory_settings, schema=vol.Schema(by_serial),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "set_accessory_setting", _set_accessory_setting,
        schema=vol.Schema({**by_serial, vol.Required("entity_id"): cv.entity_id,
                           vol.Optional("value"): vol.Any(str, int, float, bool)}),
    )
    hass.services.async_register(
        DOMAIN, "room_assignments", _room_assignments,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "list_pairings", _list_pairings,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    async def _backup(call: ServiceCall) -> ServiceResponse:
        entry = _preview_entry(hass, call.data.get("config_entry_id"))
        return await backup.async_export(hass, entry)

    async def _restore(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data["config_entry_id"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_bridge")
        try:
            path = await hass.async_add_executor_job(backup.resolve, hass, call.data["file"])
            units = await backup.async_restore(hass, entry, path)
        except (FileNotFoundError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="bad_backup",
                translation_placeholders={"file": call.data["file"], "error": str(err)},
            ) from err
        return {"units": units}

    hass.services.async_register(
        DOMAIN, "backup", _backup,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "restore", _restore,
        schema=vol.Schema({vol.Required("file"): cv.string,
                           vol.Required("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "remove_pairing", _remove_pairing,
        schema=vol.Schema({
            vol.Required("pairing_id"): cv.string,
            vol.Optional("config_entry_id"): cv.string,
        }),
    )
    return True


def _preview_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    """The bridge whose options (included/excluded entities) the preview uses."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id is not None:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_bridge"
        )
    return entries[0]


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


async def async_remove_entry(hass: HomeAssistant, entry: HapBridgeConfigEntry) -> None:
    issues.async_clear_profile_issues(hass, entry)


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
