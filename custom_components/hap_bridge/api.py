"""The public API integrations use to describe their devices as HAP accessories.

An integration ships `hap_bridge.py` next to its other platforms:

    async def async_describe_device(ctx: HapContext) -> None:
        fan = ctx.entity("fan")
        if fan is None:
            return
        acc = ctx.accessory(category="FAN")
        svc = acc.service("Fanv2", primary=True)
        svc.char(
            "Active",
            watch=[fan],
            get=lambda: int(ctx.is_on(fan)),
            set=lambda v: ctx.call("fan", "turn_on" if v else "turn_off", fan),
        )

Service and characteristic names are the raw HAP names from HAP-python's
bundled definitions ("AirPurifier", "FilterLifeLevel", ...); properties and
valid values are raw HAP props. The bridge owns IDs, pairing and transport.

Specs here are plain data: nothing touches HAP-python until the bridge
validates and materializes them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers.device_registry import DeviceEntry
    from homeassistant.helpers.entity_registry import RegistryEntry

Getter = Callable[[], Any]
Setter = Callable[[Any], Awaitable[Any] | None]


@dataclass
class CharSpec:
    name: str
    get: Getter | None = None
    set: Setter | None = None
    watch: tuple[str, ...] = ()
    props: dict[str, Any] = field(default_factory=dict)
    valid_values: dict[str, Any] | None = None


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

    @property
    def unique_id(self) -> str | None:
        """What tells this service apart from others of its type; part of its IIDs.

        Resolved lazily because characteristics (and what they watch) are added
        after the service is created.
        """
        return self._owner.unique_ids()[id(self)]

    def watched(self) -> list[str]:
        return [eid for char in self.chars.values() for eid in char.watch]

    def char(
        self,
        name: str,
        *,
        get: Getter | None = None,
        set: Setter | None = None,  # noqa: A002 - mirrors HAP read/write naming
        watch: Iterable[str] = (),
        props: dict[str, Any] | None = None,
        valid_values: dict[str, Any] | None = None,
    ) -> ServiceSpec:
        """Bind a characteristic. Returns the service so calls can chain.

        `get` is re-evaluated whenever an entity in `watch` changes state and the
        result is pushed to HomeKit; returning None skips the update. `set` is
        called with the raw HAP value when a controller writes; it may return a
        coroutine (e.g. from `ctx.call`).
        """
        watch = tuple(watch)
        self._owner.claim(*watch)
        self.chars[name] = CharSpec(name, get, set, watch, props or {}, valid_values)
        return self

    def link(self, other: ServiceSpec) -> ServiceSpec:
        self.linked.append(other)
        return self


class AccessorySpec:
    def __init__(
        self, ctx: HapContext, key: str, name: str, category: str | int
    ) -> None:
        self._ctx = ctx
        self.key = key
        self.name = name
        self.category = category
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
        if primary is not None and (watched := primary.watched()):
            return all(self._ctx.available(eid) for eid in watched)
        watched = [eid for s in self.services for eid in s.watched()]
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


class HapContext:
    """What a profile gets: one device, its entities, and helpers to bind them."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: DeviceEntry | None,
        entries: list[RegistryEntry],
    ) -> None:
        self.hass = hass
        self.device = device
        self.entries = entries
        self.accessories: list[AccessorySpec] = []
        self._claimed: set[str] = set()

    # -- building -----------------------------------------------------------

    def accessory(
        self,
        name: str | None = None,
        *,
        category: str | int = "OTHER",
        key: str | None = None,
    ) -> AccessorySpec:
        """Start an accessory. `key` must be stable; it decides the HomeKit AID.

        Defaults to the device ID, suffixed for a second accessory on one device.
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
        acc = AccessorySpec(self, key, name, category)
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
