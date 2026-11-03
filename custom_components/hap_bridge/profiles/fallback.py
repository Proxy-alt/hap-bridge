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
CLIMATE_TARGET_TEMPERATURE_RANGE = 2


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


# HVAC mode -> TargetHeatingCoolingState. heat_cool wins over auto for "Auto".
_HVAC_TARGETS = {"off": 0, "heat": 1, "cool": 2, "auto": 3, "heat_cool": 3}
_HAP_TARGET_NAMES = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto"}
# hvac_action -> CurrentHeatingCoolingState (0 = idle/off).
_HVAC_ACTIONS = {"heating": 1, "preheating": 1, "cooling": 2}


def bind_thermostat(
    ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry, humidity: str | None = None
) -> None:
    """Climate entity as a Thermostat; `humidity` is an optional sensor entity."""
    eid = ent.entity_id
    caps = ent.capabilities or {}
    modes = caps.get("hvac_modes") or ["off", "heat"]
    by_value: dict[int, str] = {}
    for mode in modes:
        if (value := _HVAC_TARGETS.get(mode)) is not None and (
            value not in by_value or mode == "heat_cool"
        ):
            by_value[value] = mode

    def current_state() -> int:
        if (action := ctx.attr(eid, "hvac_action")) is not None:
            return _HVAC_ACTIONS.get(action, 0)
        state = ctx.state(eid)
        return {"heat": 1, "cool": 2}.get(state.state if state else "", 0)

    def target_state() -> int | None:
        state = ctx.state(eid)
        return _HVAC_TARGETS.get(state.state) if state else None

    def set_target_state(value: int):
        if (mode := by_value.get(value)) is not None:
            return ctx.call("climate", "set_hvac_mode", eid, hvac_mode=mode)
        return None

    def target_temperature() -> float | None:
        if (temp := ctx.attr(eid, "temperature")) is not None:
            return ctx.to_celsius(temp)
        low, high = ctx.attr(eid, "target_temp_low"), ctx.attr(eid, "target_temp_high")
        if low is not None and high is not None:
            return ctx.to_celsius((low + high) / 2)
        return None

    temp_props: dict[str, Any] = {}
    if (low := ctx.to_celsius(caps.get("min_temp"))) is not None:
        temp_props["minValue"] = low
    if (high := ctx.to_celsius(caps.get("max_temp"))) is not None:
        temp_props["maxValue"] = high

    svc.char("CurrentHeatingCoolingState", watch=[eid], get=current_state)
    svc.char(
        "TargetHeatingCoolingState",
        watch=[eid],
        get=target_state,
        set=set_target_state,
        valid_values={_HAP_TARGET_NAMES[v]: v for v in sorted(by_value)},
    )
    svc.char(
        "CurrentTemperature",
        watch=[eid],
        get=lambda: ctx.to_celsius(ctx.attr(eid, "current_temperature")),
    )
    svc.char(
        "TargetTemperature",
        watch=[eid],
        get=target_temperature,
        set=lambda v: ctx.call("climate", "set_temperature", eid, temperature=ctx.from_celsius(v)),
        props=temp_props,
    )
    svc.char("TemperatureDisplayUnits", watch=[eid], get=lambda: int(ctx.uses_fahrenheit()))

    if ent.supported_features & CLIMATE_TARGET_TEMPERATURE_RANGE:
        for char, attr, other in (
            ("HeatingThresholdTemperature", "target_temp_low", "target_temp_high"),
            ("CoolingThresholdTemperature", "target_temp_high", "target_temp_low"),
        ):

            def setter(v, attr=attr, other=other):
                # set_temperature needs both ends of the range.
                return ctx.call(
                    "climate", "set_temperature", eid,
                    **{attr: ctx.from_celsius(v), other: ctx.attr(eid, other)},
                )

            svc.char(
                char,
                watch=[eid],
                get=lambda attr=attr: ctx.to_celsius(ctx.attr(eid, attr)),
                set=setter,
                props=temp_props,
            )

    if humidity is not None:
        svc.char("CurrentRelativeHumidity", watch=[humidity], get=lambda: ctx.number(humidity))
    elif ctx.attr(eid, "current_humidity") is not None:
        svc.char(
            "CurrentRelativeHumidity", watch=[eid], get=lambda: ctx.attr(eid, "current_humidity")
        )


# Cover state -> CurrentDoorState (0 open, 1 closed, 2 opening, 3 closing).
_DOOR_STATES = {"open": 0, "closed": 1, "opening": 2, "closing": 3}


def bind_garage_door(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id

    def state() -> str:
        current = ctx.state(eid)
        return current.state if current else ""

    svc.char("CurrentDoorState", watch=[eid], get=lambda: _DOOR_STATES.get(state(), 4))
    svc.char(
        "TargetDoorState",
        watch=[eid],
        get=lambda: int(state() in ("closed", "closing")),
        set=lambda v: ctx.call("cover", "close_cover" if v else "open_cover", eid),
    )
    svc.char("ObstructionDetected", watch=[eid], get=lambda: False)


def bind_doorbell(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Doorbell `event` entity: each new event is one press.

    An event entity's state is the timestamp of its last event, so a press is
    a new timestamp. Comparing against the last *real* timestamp keeps
    startup and unavailable -> available from ringing.
    """
    eid = ent.entity_id

    def stamp() -> str | None:
        current = ctx.state(eid)
        if current is None or current.state in ("unavailable", "unknown"):
            return None
        return current.state

    last = [stamp()]

    def pressed() -> int | None:
        now = stamp()
        if now is None or now == last[0]:
            return None
        last[0] = now
        return 0  # ProgrammableSwitchEvent: single press

    svc.char("ProgrammableSwitchEvent", watch=[eid], get=pressed)


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
    if _device_class(ent) == "garage":
        bind_garage_door(
            ctx,
            _accessory(ctx, ent, "GARAGE_DOOR_OPENER").service("GarageDoorOpener", primary=True),
            ent,
        )
        return
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


def _climate(ctx: HapContext, ent: RegistryEntry) -> None:
    bind_thermostat(ctx, _accessory(ctx, ent, "THERMOSTAT").service("Thermostat", primary=True), ent)


def _event(ctx: HapContext, ent: RegistryEntry) -> None:
    if _device_class(ent) == "doorbell":
        svc = _accessory(ctx, ent, "VIDEO_DOOR_BELL").service("Doorbell", primary=True)
        bind_doorbell(ctx, svc, ent)


_BUILDERS: dict[str, Callable[[HapContext, RegistryEntry], None]] = {
    "light": _light,
    "switch": _switch,
    "input_boolean": _switch,
    "fan": _fan,
    "sensor": _sensor,
    "binary_sensor": _binary_sensor,
    "lock": _lock,
    "cover": _cover,
    "climate": _climate,
    "event": _event,
}
