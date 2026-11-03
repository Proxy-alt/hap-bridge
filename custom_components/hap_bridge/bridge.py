"""Turns accessory specs into a running HAP bridge."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from functools import partial
import inspect
import logging
from typing import Any

from pyhap import const as hap_const
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.service import Service

from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PORT
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import STORAGE_DIR, Store

from . import pairing
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
        self._driver_running = False
        self._bridge: Bridge | None = None
        self._accessories: list[tuple[AccessorySpec, Accessory]] = []
        self._bindings: dict[str, list[tuple[Characteristic, CharSpec]]] = defaultdict(list)
        self._unsub_state = None

    @property
    def started(self) -> bool:
        return self._bridge is not None

    async def async_start(self) -> None:
        self._registry = IdRegistry(await self._store.async_load())
        zc = await zeroconf.async_get_async_instance(self.hass)
        self._driver = await self.hass.async_add_executor_job(
            partial(
                _Driver,
                on_paired=self._on_paired,
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
        seen_keys: set[str] = set()
        for spec in specs:
            # The key decides the AID; a second accessory with the same key would
            # make Bridge.add_accessory raise and take the whole bridge down.
            if spec.key in seen_keys:
                _LOGGER.error(
                    "Skipping %s: accessory key %r is already used", spec.name, spec.key
                )
                continue
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
            acc = self._materialize(spec)
            bridge.add_accessory(acc)
            self._accessories.append((spec, acc))
            seen_keys.add(spec.key)
            added += 1

        self._driver.add_accessory(bridge)
        self._bridge = bridge
        if self._registry.dirty:
            await self._store.async_save(self._registry.as_dict())
            self._registry.dirty = False

        if self._bindings:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._bindings), self._async_state_changed
            )
        await self._driver.async_start()
        self._driver_running = True
        _LOGGER.info("HAP bridge %s started with %s accessories", bridge.display_name, added)

        if self._driver.state.paired:
            pairing.async_dismiss(self.hass, self.entry)
        else:
            self._async_show_pairing()

    @callback
    def _async_show_pairing(self) -> None:
        assert self._driver is not None and self._bridge is not None
        pairing.async_show(
            self.hass,
            self.entry,
            self._bridge.display_name,
            self._driver.state.pincode.decode(),
            self._bridge.xhm_uri(),
        )

    def _on_paired(self) -> None:
        # Called from HAP-python's pairing handler, possibly off the event loop.
        self.hass.loop.call_soon_threadsafe(pairing.async_dismiss, self.hass, self.entry)

    @callback
    def async_reset_pairing(self) -> None:
        """Forget every paired controller so the bridge can be added again."""
        if self._driver is None:
            return
        for client in list(self._driver.state.paired_clients):
            self._driver.state.remove_paired_client(client)
        self._driver.async_persist()
        # The advertisement carries the "paired" flag the Home app looks for.
        self._driver.async_update_advertisement()
        self._async_show_pairing()
        _LOGGER.info("Removed all pairings from %s", self.entry.title)

    async def async_stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._driver and self._driver_running:
            # Stopping a driver that never started fails inside HAP-python,
            # which matters when async_start gave up halfway.
            await self._driver.async_stop()
        self._driver = None
        self._driver_running = False
        self._bridge = None
        self._accessories.clear()
        self._bindings.clear()

    def diagnostics(self) -> dict[str, Any]:
        """The exposed accessory tree as HomeKit sees it, for debugging profiles."""
        if self._driver is None or self._bridge is None:
            return {"started": False}
        return {
            "started": True,
            "paired_clients": len(self._driver.state.paired_clients),
            "config_version": self._driver.state.config_version,
            "accessories": [
                _diagnose_accessory(spec, acc) for spec, acc in self._accessories
            ],
        }

    def _materialize(self, spec: AccessorySpec) -> Accessory:
        assert self._driver is not None
        loader = self._driver.loader
        aid = self._registry.aid(spec.key)
        acc = _Accessory(
            spec, self._driver, spec.name, aid=aid,
            iid_manager=StableIIDManager(self._registry, aid),
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

            if svc_spec.write is not None:
                service.setter_callback = self._setter(
                    f"{spec.name}: writing {svc_spec.type_name}", svc_spec.write
                )
            for name, char_spec in svc_spec.chars.items():
                char = service.configure_char(
                    name,
                    properties=char_spec.props or None,
                    valid_values=char_spec.valid_values,
                    setter_callback=char_spec.set and self._setter(
                        f"{spec.name}: writing {name}", char_spec.set
                    ),
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

    def _setter(self, label: str, setter: Callable[[Any], Any]) -> Callable[[Any], None]:
        async def _write(value: Any) -> None:
            try:
                result = setter(value)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - one bad write must not kill the bridge
                _LOGGER.exception("%s=%r failed", label, value)

        def _on_write(value: Any) -> None:
            # HAP-python may call this off the event loop, and setters are free to
            # touch hass, so always run them on it (create_task is thread-safe).
            self.hass.create_task(_write(value), f"{DOMAIN} {label}")

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


class _Accessory(Accessory):
    """Accessory whose reachability follows its spec."""

    def __init__(self, spec: AccessorySpec, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._spec = spec

    @property
    def available(self) -> bool:
        # False makes reads fail, which the Home app shows as "No Response".
        try:
            return self._spec.is_available()
        except Exception:  # noqa: BLE001 - a broken check must not break reads
            _LOGGER.exception("%s: availability check failed", self._spec.name)
            return True


def _diagnose_accessory(spec: AccessorySpec, acc: Accessory) -> dict[str, Any]:
    iid = acc.iid_manager.get_iid
    services = []
    for svc in acc.services:
        services.append(
            {
                "type": svc.display_name,
                "iid": iid(svc),
                "unique_id": svc.unique_id,
                "primary": bool(svc.is_primary_service),
                "linked": [iid(s) for s in svc.linked_services],
                "characteristics": {
                    c.display_name: {"iid": iid(c), "value": c.value}
                    for c in svc.characteristics
                },
            }
        )
    watched = sorted({eid for s in spec.services for eid in s.watched()})
    return {
        "aid": acc.aid,
        "key": spec.key,
        "name": spec.name,
        "category": acc.category,
        "available": acc.available,
        "watches": watched,
        "services": services,
    }


class _Driver(AccessoryDriver):
    """AccessoryDriver that reports successful pairings."""

    def __init__(self, *args: Any, on_paired: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_paired = on_paired

    def pair(
        self, client_username_bytes: bytes, client_public: bytes, client_permissions: bytes
    ) -> bool:
        success = super().pair(client_username_bytes, client_public, client_permissions)
        if success:
            self._on_paired()
        return success


def _category(category: str | int) -> int:
    if isinstance(category, int):
        return category
    return getattr(hap_const, f"CATEGORY_{category.upper()}", hap_const.CATEGORY_OTHER)
