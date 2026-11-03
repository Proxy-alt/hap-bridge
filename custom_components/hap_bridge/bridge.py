"""Turns accessory specs into running HomeKit accessories.

One config entry runs one HAP bridge for most accessories, plus one HAP
server per standalone accessory (TVs, cameras): HomeKit expects those to be
paired on their own. Each of these "units" has its own port, pairing code
and pairing state; they share IDs, bindings and Home Assistant listeners.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
import hashlib
import inspect
import logging
import socket
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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import STORAGE_DIR, Store

from . import pairing
from .accessories import SpecAccessory, diagnose
from .adaptive import CHARACTERISTICS as ADAPTIVE_CHARACTERISTICS, AdaptiveLighting
from .api import AccessorySpec, CharSpec, Settings
from .const import BRIDGE_AID, DOMAIN, MAX_BRIDGED_ACCESSORIES, STORAGE_VERSION
from .ids import IdRegistry, StableIIDManager
from .profiles import async_collect_specs
from .validate import validate_spec

_LOGGER = logging.getLogger(__name__)

# The button platform listens for this to add reset buttons for standalone units.
SIGNAL_UNITS = f"{DOMAIN}_units_{{}}"


@dataclass
class Unit:
    """One HAP server: the bridge, or one standalone accessory."""

    id: str
    name: str
    driver: _Driver
    root: Accessory
    running: bool = False
    accessories: list[tuple[AccessorySpec, Accessory]] = field(default_factory=list)

    @property
    def standalone(self) -> bool:
        return not isinstance(self.root, Bridge)


class HapBridge:
    """One config entry: its HAP servers, accessories and state bindings."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.ids"
        )
        self._registry = IdRegistry()
        # Active Adaptive Lighting schedules, by accessory key + service, so a
        # restart or reload picks them up again.
        self._adaptive_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.adaptive"
        )
        self._adaptive_saved: dict[str, Any] = {}
        self._adaptive: list[tuple[str, AdaptiveLighting]] = []
        # Values the Home app sets that profiles keep (TV input names, ...).
        self._settings_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.settings"
        )
        self.units: dict[str, Unit] = {}
        self._bindings: dict[str, list[tuple[Characteristic, CharSpec]]] = defaultdict(list)
        self._unsub_state: Callable[[], None] | None = None

    @property
    def started(self) -> bool:
        return self.entry.entry_id in self.units

    @property
    def _bridge_unit(self) -> Unit | None:
        return self.units.get(self.entry.entry_id)

    # Kept for diagnostics and tests: the bridge's own driver and accessories.
    @property
    def _driver(self) -> _Driver | None:
        return unit.driver if (unit := self._bridge_unit) else None

    @property
    def _accessories(self) -> list[tuple[AccessorySpec, Accessory]]:
        return [pair for unit in self.units.values() for pair in unit.accessories]

    # -- starting and stopping ---------------------------------------------------------

    async def async_start(self) -> None:
        self._registry = IdRegistry(await self._store.async_load())
        self._adaptive_saved = await self._adaptive_store.async_load() or {}
        settings_data = await self._settings_store.async_load() or {}
        settings = Settings(
            settings_data,
            lambda: self._settings_store.async_delay_save(lambda: settings_data, 1),
        )
        zc = await zeroconf.async_get_async_instance(self.hass)

        bridge_unit = await self._async_new_unit(
            self.entry.entry_id, self.entry.data[CONF_NAME], self.entry.data[CONF_PORT], zc
        )
        bridge = Bridge(
            bridge_unit.driver,
            bridge_unit.name,
            iid_manager=StableIIDManager(self._registry, BRIDGE_AID),
        )
        bridge_unit.root = bridge

        specs = await async_collect_specs(self.hass, self.entry, settings)
        standalone: list[AccessorySpec] = []
        seen_keys: set[str] = set()
        for spec in specs:
            # The key decides the AID; a second accessory with the same key would
            # make Bridge.add_accessory raise and take the whole bridge down.
            if spec.key in seen_keys:
                _LOGGER.error("Skipping %s: accessory key %r is already used", spec.name, spec.key)
                continue
            errors, warnings = validate_spec(spec, bridge_unit.driver.loader)
            for warning in warnings:
                _LOGGER.warning("%s (%s): %s", spec.name, spec.key, warning)
            if errors:
                _LOGGER.error("Skipping %s (%s): %s", spec.name, spec.key, "; ".join(errors))
                continue
            seen_keys.add(spec.key)
            if spec.standalone:
                standalone.append(spec)
                continue
            if len(bridge_unit.accessories) >= MAX_BRIDGED_ACCESSORIES:
                _LOGGER.warning(
                    "Bridge is full (%s accessories); skipping %s",
                    MAX_BRIDGED_ACCESSORIES, spec.name,
                )
                continue
            acc = self._materialize(spec, bridge_unit.driver, self._registry.aid(spec.key))
            bridge.add_accessory(acc)
            bridge_unit.accessories.append((spec, acc))
        bridge_unit.driver.add_accessory(bridge)
        self.units[bridge_unit.id] = bridge_unit

        for spec in standalone:
            unit_id = f"{self.entry.entry_id}_{hashlib.sha1(spec.key.encode()).hexdigest()[:10]}"
            port = self._registry.port(spec.key, _port_free, self.entry.data[CONF_PORT] + 1)
            unit = await self._async_new_unit(unit_id, spec.name, port, zc)
            # A standalone accessory is always AID 1 on its own server; its IIDs
            # are still kept per key in the shared registry.
            acc = self._materialize(spec, unit.driver, hap_const.STANDALONE_AID,
                                    iid_aid=self._registry.aid(spec.key))
            unit.root = acc
            unit.accessories.append((spec, acc))
            unit.driver.add_accessory(acc)
            self.units[unit.id] = unit

        if self._registry.dirty:
            await self._store.async_save(self._registry.as_dict())
            self._registry.dirty = False

        if self._bindings:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._bindings), self._async_state_changed
            )
        for unit in self.units.values():
            await unit.driver.async_start()
            unit.running = True
            if unit.driver.state.paired:
                pairing.async_dismiss(self.hass, unit.id)
            else:
                self._async_show_pairing(unit)
        for key, adaptive in self._adaptive:
            adaptive.async_start(self._adaptive_saved.get(key))
        _LOGGER.info(
            "HAP bridge %s started with %s accessories and %s standalone",
            bridge.display_name, len(bridge_unit.accessories), len(standalone),
        )
        async_dispatcher_send(self.hass, SIGNAL_UNITS.format(self.entry.entry_id))

    async def _async_new_unit(self, unit_id: str, name: str, port: int, zc: Any) -> Unit:
        driver = await self.hass.async_add_executor_job(
            partial(
                _Driver,
                on_paired=partial(self._on_paired, unit_id),
                port=port,
                persist_file=self.hass.config.path(STORAGE_DIR, f"{DOMAIN}.{unit_id}.state"),
                async_zeroconf_instance=zc,
                loop=self.hass.loop,
            )
        )
        # root is set by the caller once the accessories are built
        return Unit(unit_id, name, driver, root=None)  # type: ignore[arg-type]

    async def async_stop(self) -> None:
        for _key, adaptive in self._adaptive:
            adaptive.async_stop()
        self._adaptive.clear()
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        for unit in self.units.values():
            for _spec, acc in unit.accessories:
                if (stop_streams := getattr(acc, "async_stop_streams", None)) is not None:
                    await stop_streams()
            if unit.running:
                # Stopping a driver that never started fails inside HAP-python,
                # which matters when async_start gave up halfway.
                await unit.driver.async_stop()
                unit.running = False
        self.units.clear()
        self._bindings.clear()

    # -- pairing --------------------------------------------------------------------------

    @callback
    def _async_show_pairing(self, unit: Unit) -> None:
        pairing.async_show(
            self.hass,
            unit.id,
            unit.name,
            unit.driver.state.pincode.decode(),
            unit.root.xhm_uri(),
        )

    def _on_paired(self, unit_id: str | None = None) -> None:
        # Called from HAP-python's pairing handler, possibly off the event loop.
        self.hass.loop.call_soon_threadsafe(
            pairing.async_dismiss, self.hass, unit_id or self.entry.entry_id
        )

    @callback
    def async_reset_pairing(self, unit_id: str | None = None) -> None:
        """Forget every paired controller of a unit (default: the bridge)."""
        if (unit := self.units.get(unit_id or self.entry.entry_id)) is None:
            return
        driver = unit.driver
        for client in list(driver.state.paired_clients):
            driver.state.remove_paired_client(client)
        driver.async_persist()
        # The advertisement carries the "paired" flag the Home app looks for.
        driver.async_update_advertisement()
        self._async_show_pairing(unit)
        _LOGGER.info("Removed all pairings from %s", unit.name)

    # -- diagnostics -----------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """The exposed accessory tree as HomeKit sees it, for debugging profiles."""
        if not self.started:
            return {"started": False}
        return {
            "started": True,
            "adaptive_lighting": {a.entity_id: a.active for _key, a in self._adaptive},
            "units": [
                {
                    "name": unit.name,
                    "standalone": unit.standalone,
                    "port": unit.driver.state.port,
                    "paired_clients": len(unit.driver.state.paired_clients),
                    "config_version": unit.driver.state.config_version,
                }
                for unit in self.units.values()
            ],
            "accessories": [diagnose(spec, acc) for spec, acc in self._accessories],
        }

    # -- building accessories -------------------------------------------------------------

    def _materialize(
        self, spec: AccessorySpec, driver: AccessoryDriver, aid: int, iid_aid: int | None = None
    ) -> Accessory:
        loader = driver.loader
        iid_manager = StableIIDManager(self._registry, iid_aid if iid_aid is not None else aid)
        if spec.camera is not None:
            from .camera import CameraAccessory  # needs the ffmpeg/camera integrations

            acc: Accessory = CameraAccessory(
                self.hass, spec, driver, spec.name, aid=aid, iid_manager=iid_manager
            )
        else:
            acc = SpecAccessory(spec, driver, spec.name, aid=aid, iid_manager=iid_manager)
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
            if svc_spec.adaptive_light is not None:
                for name in ADAPTIVE_CHARACTERISTICS:
                    if name not in present:
                        service.add_characteristic(loader.get_char(name))
            acc.add_service(service)
            built[id(svc_spec)] = service

            bound: list[tuple[Characteristic, CharSpec]] = []
            refresh = partial(self._push_all, bound)
            write = svc_spec.write
            if svc_spec.adaptive_light is not None:
                key = f"{spec.key}|{svc_spec.unique_id or ''}"
                adaptive = AdaptiveLighting(
                    self.hass,
                    spec.name,
                    svc_spec.adaptive_light,
                    service,
                    acc.iid_manager.get_iid,
                    partial(self._save_adaptive, key),
                )
                self._adaptive.append((key, adaptive))
                assert write is not None  # checked by validate_spec
                write = adaptive.wrap_write(write)
            if write is not None:
                service.setter_callback = self._setter(
                    f"{spec.name}: writing {svc_spec.type_name}", write, refresh
                )
            for name, char_spec in svc_spec.chars.items():
                char = service.configure_char(
                    name,
                    properties=char_spec.props or None,
                    valid_values=char_spec.valid_values,
                    setter_callback=char_spec.set and self._setter(
                        f"{spec.name}: writing {name}", char_spec.set, refresh
                    ),
                )
                bound.append((char, char_spec))
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

    def _save_adaptive(self, key: str, transition: dict[str, Any] | None) -> None:
        if transition is None:
            self._adaptive_saved.pop(key, None)
        else:
            self._adaptive_saved[key] = transition
        self.hass.async_create_task(
            self._adaptive_store.async_save(dict(self._adaptive_saved)),
            f"{DOMAIN} save adaptive lighting",
        )

    def _setter(
        self, label: str, setter: Callable[[Any], Any], refresh: Callable[[], None]
    ) -> Callable[[Any], None]:
        async def _write(value: Any) -> None:
            try:
                result = setter(value)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - one bad write must not kill the bridge
                _LOGGER.exception("%s=%r failed", label, value)
            # Re-read the service: a write can change values that no entity
            # state reflects (e.g. a TV input the Home app just renamed).
            refresh()

        def _on_write(value: Any) -> None:
            # HAP-python may call this off the event loop, and setters are free to
            # touch hass, so always run them on it (create_task is thread-safe).
            self.hass.create_task(_write(value), f"{DOMAIN} {label}")

        return _on_write

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        for char, char_spec in self._bindings.get(event.data["entity_id"], ()):
            self._push(char, char_spec)

    @classmethod
    def _push_all(cls, bound: list[tuple[Characteristic, CharSpec]]) -> None:
        for char, char_spec in bound:
            cls._push(char, char_spec)

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


def _port_free(port: int) -> bool:
    """Whether nothing listens on `port` (checked once, when a port is first assigned)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("", port))
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - sockets may be unavailable (e.g. tests)
        return True
    return True


def _category(category: str | int) -> int:
    if isinstance(category, int):
        return category
    return getattr(hap_const, f"CATEGORY_{category.upper()}", hap_const.CATEGORY_OTHER)
