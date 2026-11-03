"""Per-entity mapping for anything no profile claimed.

Roughly what the core HomeKit Bridge does today. The `bind_*` helpers are
also reused by richer profiles so one entity type is always mapped one way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..api import HapContext, ServiceSpec

if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import RegistryEntry

# Feature bits from the HA entity models.
FAN_SET_SPEED = 1
COVER_SET_POSITION = 4


def describe_entity(ctx: HapContext, ent: RegistryEntry) -> None:
    # Config/diagnostic entities and hidden ones are for profiles to use, not tiles.
    if ent.entity_category is not None or ent.hidden_by is not None:
        return
    builder = _BUILDERS.get(ent.domain)
    if builder is not None:
        builder(ctx, ent)


def _accessory(ctx: HapContext, ent: RegistryEntry, category: str):
    return ctx.accessory(ctx.name_of(ent.entity_id), category=category, key=f"entity:{ent.id}")


def _device_class(ent: RegistryEntry) -> str | None:
    return ent.device_class or ent.original_device_class


# -- binders ------------------------------------------------------------------


def bind_light(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id
    svc.char(
        "On",
        watch=[eid],
        get=lambda: ctx.is_on(eid),
        set=lambda v: ctx.call("light", "turn_on" if v else "turn_off", eid),
    )
    modes = (ent.capabilities or {}).get("supported_color_modes") or []
    if any(mode != "onoff" for mode in modes):
        svc.char(
            "Brightness",
            watch=[eid],
            get=lambda: round((ctx.attr(eid, "brightness") or 0) / 255 * 100),
            set=lambda v: ctx.call("light", "turn_on", eid, brightness_pct=v),
        )


def bind_fan(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id
    svc.char(
        "Active",
        watch=[eid],
        get=lambda: int(ctx.is_on(eid)),
        set=lambda v: ctx.call("fan", "turn_on" if v else "turn_off", eid),
    )
    if ent.supported_features & FAN_SET_SPEED:
        svc.char(
            "RotationSpeed",
            watch=[eid],
            get=lambda: ctx.attr(eid, "percentage") or 0,
            set=lambda v: ctx.call("fan", "set_percentage", eid, percentage=v),
        )


def bind_on_off(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id
    svc.char(
        "On",
        watch=[eid],
        get=lambda: ctx.is_on(eid),
        set=lambda v: ctx.call(ent.domain, "turn_on" if v else "turn_off", eid),
    )


# -- per-domain accessories -----------------------------------------------------


def _light(ctx: HapContext, ent: RegistryEntry) -> None:
    bind_light(ctx, _accessory(ctx, ent, "LIGHTBULB").service("Lightbulb", primary=True), ent)


def _switch(ctx: HapContext, ent: RegistryEntry) -> None:
    if _device_class(ent) == "outlet":
        svc = _accessory(ctx, ent, "OUTLET").service("Outlet", primary=True)
        svc.char("OutletInUse", watch=[ent.entity_id], get=lambda: ctx.is_on(ent.entity_id))
    else:
        svc = _accessory(ctx, ent, "SWITCH").service("Switch", primary=True)
    bind_on_off(ctx, svc, ent)


def _fan(ctx: HapContext, ent: RegistryEntry) -> None:
    bind_fan(ctx, _accessory(ctx, ent, "FAN").service("Fanv2", primary=True), ent)


def _sensor(ctx: HapContext, ent: RegistryEntry) -> None:
    eid = ent.entity_id
    device_class = _device_class(ent)
    if device_class == "temperature":

        def celsius() -> float | None:
            value = ctx.number(eid)
            if value is not None and ctx.attr(eid, "unit_of_measurement") == "°F":
                value = (value - 32) * 5 / 9
            return value

        svc = _accessory(ctx, ent, "SENSOR").service("TemperatureSensor", primary=True)
        svc.char("CurrentTemperature", watch=[eid], get=celsius)
    elif device_class == "humidity":
        svc = _accessory(ctx, ent, "SENSOR").service("HumiditySensor", primary=True)
        svc.char("CurrentRelativeHumidity", watch=[eid], get=lambda: ctx.number(eid))


_BINARY_SENSORS: dict[str, tuple[str, str, Callable[[bool], Any]]] = {
    "motion": ("MotionSensor", "MotionDetected", bool),
    "occupancy": ("OccupancySensor", "OccupancyDetected", int),
    "presence": ("OccupancySensor", "OccupancyDetected", int),
    # ContactSensorState: 0 = contact (closed), 1 = no contact (open).
    "door": ("ContactSensor", "ContactSensorState", int),
    "window": ("ContactSensor", "ContactSensorState", int),
    "opening": ("ContactSensor", "ContactSensorState", int),
    "garage_door": ("ContactSensor", "ContactSensorState", int),
    "moisture": ("LeakSensor", "LeakDetected", int),
    "smoke": ("SmokeSensor", "SmokeDetected", int),
}


def _binary_sensor(ctx: HapContext, ent: RegistryEntry) -> None:
    mapping = _BINARY_SENSORS.get(_device_class(ent) or "")
    if mapping is None:
        return
    service, char, convert = mapping
    eid = ent.entity_id
    svc = _accessory(ctx, ent, "SENSOR").service(service, primary=True)
    svc.char(char, watch=[eid], get=lambda: convert(ctx.is_on(eid)))


_LOCK_STATES = {"unlocked": 0, "locked": 1, "jammed": 2}


def _lock(ctx: HapContext, ent: RegistryEntry) -> None:
    eid = ent.entity_id

    def current() -> int:
        state = ctx.state(eid)
        return _LOCK_STATES.get(state.state if state else "", 3)

    def target() -> int | None:
        state = ctx.state(eid)
        if state is None:
            return None
        return 1 if state.state in ("locked", "locking") else 0

    svc = _accessory(ctx, ent, "DOOR_LOCK").service("LockMechanism", primary=True)
    svc.char("LockCurrentState", watch=[eid], get=current)
    svc.char(
        "LockTargetState",
        watch=[eid],
        get=target,
        set=lambda v: ctx.call("lock", "lock" if v else "unlock", eid),
    )


def _cover(ctx: HapContext, ent: RegistryEntry) -> None:
    if not ent.supported_features & COVER_SET_POSITION:
        return
    eid = ent.entity_id
    svc = _accessory(ctx, ent, "WINDOW_COVERING").service("WindowCovering", primary=True)
    svc.char(
        "CurrentPosition", watch=[eid], get=lambda: ctx.attr(eid, "current_position")
    )
    svc.char(
        "TargetPosition",
        watch=[eid],
        get=lambda: ctx.attr(eid, "current_position"),
        set=lambda v: ctx.call("cover", "set_cover_position", eid, position=v),
    )
    # PositionState: 0 = closing, 1 = opening, 2 = stopped.
    svc.char(
        "PositionState",
        watch=[eid],
        get=lambda: {"closing": 0, "opening": 1}.get(getattr(ctx.state(eid), "state", ""), 2),
    )


_BUILDERS: dict[str, Callable[[HapContext, RegistryEntry], None]] = {
    "light": _light,
    "switch": _switch,
    "input_boolean": _switch,
    "fan": _fan,
    "sensor": _sensor,
    "binary_sensor": _binary_sensor,
    "lock": _lock,
    "cover": _cover,
}
