"""Turns accessory specs into a running HAP bridge."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable
from functools import partial
import inspect
import logging
from typing import Any

from pyhap import const as hap_const
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic

from homeassistant.components import persistent_notification, zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PORT
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import STORAGE_DIR, Store

from .api import AccessorySpec, CharSpec
from .const import BRIDGE_AID, DOMAIN, MAX_BRIDGED_ACCESSORIES, STORAGE_VERSION
from .ids import IdRegistry, StableIIDManager
from .profiles import async_collect_specs
from .validate import validate_spec

_LOGGER = logging.getLogger(__name__)


class HapBridge:
    """One config entry: one HAP bridge, its accessories and state bindings."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.ids"
        )
        self._registry = IdRegistry()
        self._driver: AccessoryDriver | None = None
        self._bindings: dict[str, list[tuple[Characteristic, CharSpec]]] = defaultdict(list)
        self._unsub_state = None

    async def async_start(self) -> None:
        self._registry = IdRegistry(await self._store.async_load())
        zc = await zeroconf.async_get_async_instance(self.hass)
        self._driver = await self.hass.async_add_executor_job(
            partial(
                AccessoryDriver,
                port=self.entry.data[CONF_PORT],
                persist_file=self.hass.config.path(
                    STORAGE_DIR, f"{DOMAIN}.{self.entry.entry_id}.state"
                ),
                async_zeroconf_instance=zc,
                loop=self.hass.loop,
            )
        )
        bridge = Bridge(
            self._driver,
            self.entry.data[CONF_NAME],
            iid_manager=StableIIDManager(self._registry, BRIDGE_AID),
        )

        specs = await async_collect_specs(self.hass, self.entry)
        added = 0
        for spec in specs:
            errors, warnings = validate_spec(spec, self._driver.loader)
            for warning in warnings:
                _LOGGER.warning("%s (%s): %s", spec.name, spec.key, warning)
            if errors:
                _LOGGER.error("Skipping %s (%s): %s", spec.name, spec.key, "; ".join(errors))
                continue
            if added >= MAX_BRIDGED_ACCESSORIES:
                _LOGGER.warning(
                    "Bridge is full (%s accessories); skipping %s",
                    MAX_BRIDGED_ACCESSORIES,
                    spec.name,
                )
                continue
            bridge.add_accessory(self._materialize(spec))
            added += 1

        self._driver.add_accessory(bridge)
        if self._registry.dirty:
            await self._store.async_save(self._registry.as_dict())
            self._registry.dirty = False

        if self._bindings:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._bindings), self._async_state_changed
            )
        await self._driver.async_start()
        _LOGGER.info("HAP bridge %s started with %s accessories", bridge.display_name, added)

        if not self._driver.state.paired:
            pincode = self._driver.state.pincode.decode()
            persistent_notification.async_create(
                self.hass,
                f"Add **{bridge.display_name}** in the Home app with code "
                f"`{pincode}`.\n\nSetup URI: `{bridge.xhm_uri()}`",
                title="HAP Bridge pairing",
                notification_id=f"{DOMAIN}_{self.entry.entry_id}_pairing",
            )

    async def async_stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._driver:
            await self._driver.async_stop()
            self._driver = None
        self._bindings.clear()

    def _materialize(self, spec: AccessorySpec) -> Accessory:
        assert self._driver is not None
        loader = self._driver.loader
        aid = self._registry.aid(spec.key)
        acc = Accessory(
            self._driver, spec.name, aid=aid, iid_manager=StableIIDManager(self._registry, aid)
        )
        acc.category = _category(spec.category)
        acc.set_info_service(**{k: str(v) for k, v in spec.info.items() if v})

        built: dict[int, Service] = {}
        for svc_spec in spec.services:
            # Build the service fully before add_service: IIDs are assigned there,
            # and the unique_id must already be set for them to be stable.
            service = loader.get_service(svc_spec.type_name)
            service.unique_id = svc_spec.unique_id
            present = {c.display_name for c in service.characteristics}
            for name in svc_spec.chars:
                if name not in present:
                    service.add_characteristic(loader.get_char(name))
            acc.add_service(service)
            built[id(svc_spec)] = service

            for name, char_spec in svc_spec.chars.items():
                char = service.configure_char(
                    name,
                    properties=char_spec.props or None,
                    valid_values=char_spec.valid_values,
                    setter_callback=self._setter(spec, char_spec),
                )
                self._push(char, char_spec)
                for entity_id in char_spec.watch:
                    self._bindings[entity_id].append((char, char_spec))

        for svc_spec in spec.services:
            service = built[id(svc_spec)]
            if svc_spec.primary:
                acc.set_primary_service(service)
            for linked in svc_spec.linked:
                service.add_linked_service(built[id(linked)])
        return acc

    def _setter(self, spec: AccessorySpec, char_spec: CharSpec):
        if char_spec.set is None:
            return None

        async def _await(result: Awaitable[Any], value: Any) -> None:
            try:
                await result
            except Exception:  # noqa: BLE001 - one bad write must not kill the bridge
                _LOGGER.exception("%s: writing %s=%r failed", spec.name, char_spec.name, value)

        def _on_write(value: Any) -> None:
            # HAP-python may call this off the event loop.
            result = char_spec.set(value)
            if inspect.isawaitable(result):
                asyncio.run_coroutine_threadsafe(_await(result, value), self.hass.loop)

        return _on_write

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        for char, char_spec in self._bindings.get(event.data["entity_id"], ()):
            self._push(char, char_spec)

    @staticmethod
    def _push(char: Characteristic, char_spec: CharSpec) -> None:
        if char_spec.get is None:
            return
        try:
            value = char_spec.get()
            if value is not None:
                char.set_value(value)
        except Exception:  # noqa: BLE001 - a broken getter only affects its own value
            _LOGGER.exception("Reading %s failed", char_spec.name)


def _category(category: str | int) -> int:
    if isinstance(category, int):
        return category
    return getattr(hap_const, f"CATEGORY_{category.upper()}", hap_const.CATEGORY_OTHER)
