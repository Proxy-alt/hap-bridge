"""The public API integrations use to describe their devices as HAP accessories.

An integration ships `hap_bridge.py` next to its other platforms. It doesn't
import anything from HAP Bridge: everything comes in through `ctx`, so core
integrations can ship one too and nothing breaks when HAP Bridge isn't
installed.

    HAP_BRIDGE_API = 1  # the API version this profile needs (optional)

    async def async_describe_device(ctx) -> None:
        fan = ctx.entity("fan")
        filter_life = ctx.entity("sensor", translation_key="filter_life")
        if fan is None:
            return
        acc = ctx.accessory(category="AIR_PURIFIER")
        purifier = ctx.blocks.fan(acc.service("AirPurifier", primary=True), fan)
        if filter_life:
            ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=purifier),
                              life=filter_life)

`ctx.blocks` are the bridge's own tested mappings (lights, climate, TVs,
cameras...). For anything else, bind characteristics directly with
`svc.char(...)`; `ctx.hap` has the service, characteristic and value names
(`ctx.hap.Service.AIR_PURIFIER`, `ctx.hap.Active.ACTIVE`).

The bridge owns IDs, pairing and transport. Specs here are plain data:
nothing touches HAP-python until the bridge validates and materializes them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers.device_registry import DeviceEntry
    from homeassistant.helpers.entity_registry import RegistryEntry

# Bumped when the API changes incompatibly. A profile can declare the version
# it needs as `HAP_BRIDGE_API = n`; older bridges then skip it with a repair
# issue instead of failing halfway.
API_VERSION = 1

Getter = Callable[[], Any]
Setter = Callable[[Any], Awaitable[Any] | None]


@dataclass(frozen=True)
class Recording:
    """What starts a HomeKit Secure Video recording on a camera.

    A recording starts when any `motion` entity turns on, or when the
    `doorbell` event fires, and ends once they've all been quiet for a while
    (the hub decides when it has enough). The hub works out by itself whether
    a clip shows people, animals, vehicles or packages; HomeKit has no way
    for a camera to say what it saw. So with only person and vehicle sensors
    as `motion`, a Home app setting to record animals finds nothing.
    """

    motion: tuple[str, ...] = ()
    doorbell: str | None = None


@dataclass
class CharSpec:
    name: str
    get: Getter | None = None
    set: Setter | None = None
    watch: tuple[str, ...] = ()
    props: dict[str, Any] = field(default_factory=dict)
    valid_values: dict[str, Any] | None = None
    affects_available: bool = True


class ServiceSpec:
    def __init__(
        self, owner: AccessorySpec, type_name: str, unique_id: str | None, primary: bool
    ) -> None:
        self._owner = owner
        self.type_name = type_name
        self.explicit_unique_id = unique_id
        self.primary = primary
        self.chars: dict[str, CharSpec] = {}
        self.linked: list[ServiceSpec] = []
        self.write: Callable[[dict[str, Any]], Awaitable[Any] | None] | None = None
        self.adaptive_light: str | None = None
        # None: decided by the bridge (on for a color-temperature light);
        # True/False: the profile asked for it, or opted out.
        self.adaptive_wanted: bool | None = None

    @property
    def unique_id(self) -> str | None:
        """What tells this service apart from others of its type; part of its IIDs.

        Resolved lazily because characteristics (and what they watch) are added
        after the service is created.
        """
        return self._owner.unique_ids()[id(self)]

    def watched(self, *, for_availability: bool = False) -> list[str]:
        return [
            eid
            for char in self.chars.values()
            if char.affects_available or not for_availability
            for eid in char.watch
        ]

    def char(
        self,
        name: str,
        *,
        get: Getter | None = None,
        set: Setter | None = None,  # noqa: A002 - mirrors HAP read/write naming
        watch: Iterable[str] = (),
        props: dict[str, Any] | None = None,
        valid_values: dict[str, Any] | None = None,
        affects_available: bool = True,
    ) -> ServiceSpec:
        """Bind a characteristic. Returns the service so calls can chain.

        `get` is re-evaluated whenever an entity in `watch` changes state and the
        result is pushed to HomeKit; returning None skips the update. `set` is
        called with the raw HAP value when a controller writes; it may return a
        coroutine (e.g. from `ctx.call`). With `affects_available=False` the
        watched entities don't count towards the accessory's "No Response"
        (for side details such as battery or tamper flags).
        """
        watch = tuple(watch)
        self._owner.claim(*watch)
        self.chars[name] = CharSpec(
            name, get, set, watch, props or {}, valid_values, affects_available
        )
        return self

    def link(self, other: ServiceSpec) -> ServiceSpec:
        self.linked.append(other)
        return self

    def on_write(self, write: Callable[[dict[str, Any]], Awaitable[Any] | None]) -> ServiceSpec:
        """Handle all of one controller write to this service in a single call.

        `write` gets `{characteristic name: HAP value}` for everything the Home
        app changed at once (e.g. On + Brightness + Hue), so it can make one
        service call. Characteristics bound without their own `set` become
        writable through it. It may return a coroutine.
        """
        self.write = write
        return self

    def adaptive_lighting(
        self, light_entity_id: str | None = None, *, enabled: bool = True
    ) -> ServiceSpec:
        """Offer Adaptive Lighting on this Lightbulb, run by the bridge.

        The bridge already does this for any Lightbulb with Brightness,
        ColorTemperature and an `on_write` whose ColorTemperature watches a
        light that supports color temperature, so most profiles never call
        it. Call it to name the light when ColorTemperature doesn't watch it,
        or with `enabled=False` to opt out.

        The bridge follows the Home app's schedule by calling light.turn_on
        on the light, and stops when a color is chosen by hand.
        """
        self.adaptive_wanted = enabled
        self.adaptive_light = light_entity_id if enabled else None
        return self


class AccessorySpec:
    def __init__(
        self,
        ctx: HapContext,
        key: str,
        name: str,
        category: str | int,
        standalone: bool = False,
        camera: str | None = None,
    ) -> None:
        self._ctx = ctx
        self.key = key
        self.name = name
        self.category = category
        # Published as its own HomeKit accessory (own port and pairing code)
        # instead of behind the bridge. HomeKit expects this for TVs and cameras.
        self.standalone = standalone
        # A camera entity to stream and snapshot; the bridge adds the camera
        # services and runs ffmpeg. Implies standalone.
        self.camera = camera
        # Also send the camera stream's audio. Off unless the profile or the
        # options flow says the camera has it: ffmpeg can't stream a source
        # without audio when asked for some.
        self.camera_audio = False
        # HomeKit Secure Video triggers; None means the camera can't record.
        self.recording: Recording | None = None
        # Keep ffmpeg running so recordings start before their trigger.
        self.camera_prebuffer = True
        # Called when the Home app's "Identify Accessory" is tapped.
        self.identify: Callable[[], Any] | None = None
        # The name the profile gave, when the options flow renamed it.
        self.default_name = name
        # Which step built it, e.g. "integration:acme", "profile:air_purifier",
        # "fallback". Shown in diagnostics and previews.
        self.source = ctx.source
        self.services: list[ServiceSpec] = []
        self.info: dict[str, str | None] = {}
        # Overrides the default "No Response" rule; see is_available().
        self.available: Callable[[], bool] | None = None

    def is_available(self) -> bool:
        """Whether HomeKit should see the accessory as reachable.

        By default: every entity the primary service watches (or, without a
        primary, any watched entity) has a real state. Linked extras such as a
        filter sensor going unavailable don't take the whole tile down.
        """
        if self.available is not None:
            return self.available()
        primary = next((s for s in self.services if s.primary), None)
        if primary is not None and (watched := primary.watched(for_availability=True)):
            return all(self._ctx.available(eid) for eid in watched)
        watched = [eid for s in self.services for eid in s.watched(for_availability=True)]
        return not watched or any(self._ctx.available(eid) for eid in watched)

    def service(
        self,
        type_name: str,
        *,
        unique_id: str | None = None,
        primary: bool = False,
        linked_to: ServiceSpec | None = None,
    ) -> ServiceSpec:
        """Add a HAP service. Repeat services of one type get distinct IDs.

        Without a `unique_id`, a service is keyed on the registry ID of the
        first entity it watches, so services coming and going never shift the
        IDs of the others.
        """
        svc = ServiceSpec(self, type_name, unique_id, primary)
        self.services.append(svc)
        if linked_to is not None:
            linked_to.link(svc)
        return svc

    def claim(self, *entity_ids: str) -> None:
        self._ctx.claim(*entity_ids)

    def unique_ids(self) -> dict[int, str | None]:
        """Unique ID per service (keyed by `id(service)`), distinct within a type."""
        out: dict[int, str | None] = {}
        taken: set[tuple[str, str | None]] = set()
        for svc in self.services:
            base = svc.explicit_unique_id
            if base is None:
                entries = (self._ctx.entry(eid) for eid in svc.watched())
                base = next((e.id for e in entries if e is not None), None)
            uid, n = base, 0
            while (svc.type_name, uid) in taken:
                n += 1
                uid = str(n) if base is None else f"{base}:{n}"
            taken.add((svc.type_name, uid))
            out[id(svc)] = uid
        return out


class Settings:
    """Small values the Home app sets and the bridge keeps across restarts,
    e.g. names given to TV inputs or which inputs are hidden."""

    def __init__(
        self, data: dict[str, Any] | None = None, save: Callable[[], None] | None = None
    ) -> None:
        self.data: dict[str, Any] = data if data is not None else {}
        self._save = save

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self.data.get(key) != value:
            self.data[key] = value
            if self._save is not None:
                self._save()


class HapContext:
    """What a profile gets: one device, its entities, and helpers to bind them."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: DeviceEntry | None,
        entries: list[RegistryEntry],
        settings: Settings | None = None,
    ) -> None:
        self.hass = hass
        self.device = device
        self.entries = entries
        self.settings = settings if settings is not None else Settings()
        self.accessories: list[AccessorySpec] = []
        self._claimed: set[str] = set()
        # Set by the bridge while each step runs; stamped onto new accessories.
        self.source = "unknown"
        self._blocks: Any = None

    api_version = API_VERSION

    @property
    def blocks(self) -> Any:
        """The bridge's tested building blocks (see blocks.py)."""
        if self._blocks is None:
            from .blocks import Blocks

            self._blocks = Blocks(self)
        return self._blocks

    @property
    def hap(self) -> ModuleType:
        """HomeKit service, characteristic and value names (see hap.py)."""
        from . import hap

        return hap

    def config_entry(self, domain: str) -> ConfigEntry | None:
        """The config entry of `domain` this device (or entity) belongs to."""
        entry_ids: set[str] = set()
        if self.device is not None:
            entry_ids.update(getattr(self.device, "config_entries", ()))
        entry_ids.update(
            entry_id for e in self.entries if (entry_id := getattr(e, "config_entry_id", None))
        )
        for entry_id in sorted(entry_ids):
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == domain:
                return entry
        return None

    def runtime_data(self, domain: str) -> Any:
        """That config entry's runtime_data (a coordinator, client...), or None.

        For profiles that talk to the device directly, beyond its entities.
        """
        entry = self.config_entry(domain)
        return getattr(entry, "runtime_data", None) if entry is not None else None

    # -- building -----------------------------------------------------------

    def accessory(
        self,
        name: str | None = None,
        *,
        category: str | int = "OTHER",
        key: str | None = None,
        standalone: bool = False,
        camera: str | None = None,
    ) -> AccessorySpec:
        """Start an accessory. `key` must be stable; it decides the HomeKit AID.

        Defaults to the device ID, suffixed for a second accessory on one device.
        `standalone` publishes it outside the bridge with its own pairing code
        (TVs, cameras); `camera` makes it stream that camera entity.
        """
        if key is None:
            if self.device is not None:
                base = f"device:{self.device.id}"
            else:
                # Device-less contexts each hold their own entities; key on them so
                # two such contexts never share an AID.
                base = "orphan:" + ",".join(sorted(e.id for e in self.entries))
            key = base if not self.accessories else f"{base}:{len(self.accessories)}"
        if name is None:
            name = self._device_name()
        acc = AccessorySpec(self, key, name, category, standalone or camera is not None, camera)
        if self.device is not None:
            acc.info = {
                "manufacturer": self.device.manufacturer,
                "model": self.device.model,
                "firmware_revision": self.device.sw_version,
                "serial_number": self.device.serial_number,
            }
        self.accessories.append(acc)
        return acc

    def claim(self, *entity_ids: str) -> None:
        """Mark entities as handled so the fallback mapper skips them."""
        self._claimed.update(entity_ids)

    def unclaimed(self) -> list[RegistryEntry]:
        return [e for e in self.entries if e.entity_id not in self._claimed]

    # -- finding entities -----------------------------------------------------

    def entities(
        self,
        domain: str,
        *,
        device_class: str | None = None,
        translation_key: str | None = None,
        predicate: Callable[[RegistryEntry], bool] | None = None,
    ) -> list[str]:
        """Unclaimed entity IDs on this device matching the filters."""
        found = []
        for e in self.unclaimed():
            if e.domain != domain:
                continue
            if device_class and (e.device_class or e.original_device_class) != device_class:
                continue
            if translation_key and e.translation_key != translation_key:
                continue
            if predicate and not predicate(e):
                continue
            found.append(e.entity_id)
        return found

    def entity(self, domain: str, **filters: Any) -> str | None:
        found = self.entities(domain, **filters)
        return found[0] if found else None

    def entry(self, entity_id: str) -> RegistryEntry | None:
        return next((e for e in self.entries if e.entity_id == entity_id), None)

    # -- reading and writing state -------------------------------------------

    def state(self, entity_id: str) -> State | None:
        return self.hass.states.get(entity_id)

    def available(self, entity_id: str) -> bool:
        state = self.state(entity_id)
        return state is not None and state.state not in ("unavailable", "unknown")

    def is_on(self, entity_id: str) -> bool:
        state = self.state(entity_id)
        return state is not None and state.state == "on"

    def attr(self, entity_id: str, name: str, default: Any = None) -> Any:
        state = self.state(entity_id)
        return default if state is None else state.attributes.get(name, default)

    def number(self, entity_id: str) -> float | None:
        state = self.state(entity_id)
        try:
            return float(state.state) if state else None
        except ValueError:
            return None

    def to_celsius(self, value: float | None) -> float | None:
        """HA temperature (in the configured unit system) to HAP's Celsius."""
        if value is None or not self.uses_fahrenheit():
            return value
        return round((value - 32) * 5 / 9, 1)

    def from_celsius(self, value: float) -> float:
        """HAP Celsius to an HA temperature in the configured unit system."""
        return round(value * 9 / 5 + 32, 1) if self.uses_fahrenheit() else value

    def uses_fahrenheit(self) -> bool:
        config = getattr(self.hass, "config", None)
        units = getattr(config, "units", None)
        return getattr(units, "temperature_unit", "°C") == "°F"

    def call(self, domain: str, service: str, entity_id: str, **data: Any) -> Awaitable[Any]:
        return self.hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=False
        )

    def name_of(self, entity_id: str) -> str:
        if (name := self.attr(entity_id, "friendly_name")) is not None:
            return name
        entry = self.entry(entity_id)
        return (entry and (entry.name or entry.original_name)) or entity_id

    def _device_name(self) -> str:
        if self.device is not None:
            return self.device.name_by_user or self.device.name or self.device.id
        return "Accessory"
