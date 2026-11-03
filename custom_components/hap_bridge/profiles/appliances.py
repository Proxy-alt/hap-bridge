"""Binders for appliances and controls: AC units, humidifiers, alarms, valves,
water heaters, vacuums and mowers, scenes/scripts/buttons, and selects.

Mappings follow core HomeKit where it has one, so a device looks the same
in the Home app either way. Like the binders in fallback.py these take a
ServiceSpec, so profiles can reuse them on richer accessories.
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterable
import inspect
import math
from typing import TYPE_CHECKING, Any

from ..api import HapContext, ServiceSpec

if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import RegistryEntry

# Feature bits from the HA entity models.
CLIMATE_TARGET_TEMPERATURE_RANGE = 2
CLIMATE_TARGET_HUMIDITY = 4
CLIMATE_FAN_MODE = 8
CLIMATE_SWING_MODE = 32
CLIMATE_TURN_OFF = 128
CLIMATE_TURN_ON = 256
ALARM_ARM_HOME = 1
ALARM_ARM_AWAY = 2
ALARM_ARM_NIGHT = 4
ALARM_ARM_VACATION = 32
VALVE_OPEN = 1
VALVE_CLOSE = 2
WATER_HEATER_ON_OFF = 8
VACUUM_RETURN_HOME = 16
VACUUM_START = 8192
MOWER_START = 1
MOWER_DOCK = 4


async def _run(*calls: Awaitable[Any] | None) -> None:
    """Await service calls in order (ctx.call may return None in tests)."""
    for result in calls:
        if inspect.isawaitable(result):
            await result


def _state(ctx: HapContext, eid: str) -> str:
    state = ctx.state(eid)
    return state.state if state else ""


# -- climate: HeaterCooler ----------------------------------------------------------

# Fan modes that say *when* the fan runs rather than how fast.
_TIMING_FAN_MODES = {"auto", "on", "off", "diffuse", "focus", "circulate"}
_SWING_ON_MODES = ("on", "both", "vertical", "horizontal")


def fan_speeds(ent: RegistryEntry) -> list[str]:
    return [
        m for m in (ent.capabilities or {}).get("fan_modes") or []
        if m.lower() not in _TIMING_FAN_MODES
    ]


def swing_modes(ent: RegistryEntry) -> tuple[str, str] | None:
    """(on mode, off mode) if the entity can swing on and off."""
    modes = (ent.capabilities or {}).get("swing_modes") or []
    lower = {m.lower(): m for m in modes}
    on = next((lower[m] for m in _SWING_ON_MODES if m in lower), None)
    return (on, lower["off"]) if on and "off" in lower else None


# HVAC mode -> TargetHeaterCoolerState (0 auto, 1 heat, 2 cool).
_HC_TARGETS = {"heat_cool": 0, "auto": 0, "heat": 1, "cool": 2}
_HC_TARGET_NAMES = {0: "Auto", 1: "Heat", 2: "Cool"}
# hvac_action -> CurrentHeaterCoolerState (0 inactive, 1 idle, 2 heating, 3 cooling).
# HomeKit has no drying/fan value; core HomeKit shows those as cooling too.
_HC_ACTIONS = {
    "off": 0, "idle": 1, "heating": 2, "preheating": 2, "defrosting": 2,
    "cooling": 3, "drying": 3, "fan": 3,
}


def uses_heater_cooler(ent: RegistryEntry) -> bool:
    """Core HomeKit's rule: AC-style units (fan speeds or swing) without a
    humidity target, which only Thermostat can show, become HeaterCooler."""
    features = ent.supported_features
    if features & CLIMATE_TARGET_HUMIDITY:
        return False
    has_fan = bool(features & CLIMATE_FAN_MODE) and len(fan_speeds(ent)) >= 2
    has_swing = bool(features & CLIMATE_SWING_MODE) and swing_modes(ent) is not None
    modes = (ent.capabilities or {}).get("hvac_modes") or []
    return (has_fan or has_swing) and any(m in _HC_TARGETS for m in modes)


def heater_cooler_category(ent: RegistryEntry) -> str:
    modes = (ent.capabilities or {}).get("hvac_modes") or []
    return "AIR_CONDITIONER" if "cool" in modes or "heat_cool" in modes else "HEATER"


def bind_heater_cooler(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Climate entity as a HeaterCooler: power, mode, setpoints, fan speed, swing."""
    eid = ent.entity_id
    caps = ent.capabilities or {}
    features = ent.supported_features
    modes = caps.get("hvac_modes") or []
    by_value: dict[int, str] = {}
    for mode in modes:
        if (value := _HC_TARGETS.get(mode)) is not None and (
            value not in by_value or mode == "heat_cool"
        ):
            by_value[value] = mode
    speeds = fan_speeds(ent) if features & CLIMATE_FAN_MODE else []
    swing = swing_modes(ent) if features & CLIMATE_SWING_MODE else None
    ranged = bool(features & CLIMATE_TARGET_TEMPERATURE_RANGE)

    temp_props: dict[str, Any] = {}
    if (low := ctx.to_celsius(caps.get("min_temp"))) is not None:
        temp_props["minValue"] = low
    if (high := ctx.to_celsius(caps.get("max_temp"))) is not None:
        temp_props["maxValue"] = high

    def setpoint(attr: str) -> float | None:
        value = ctx.attr(eid, attr) if ranged else None
        return ctx.to_celsius(value if value is not None else ctx.attr(eid, "temperature"))

    def write(values: dict[str, Any]):
        calls: list[Awaitable[Any] | None] = []
        if values.get("Active") == 0:
            if features & CLIMATE_TURN_OFF:
                return _run(ctx.call("climate", "turn_off", eid))
            return _run(ctx.call("climate", "set_hvac_mode", eid, hvac_mode="off"))
        target = values.get("TargetHeaterCoolerState")
        if target in by_value:
            calls.append(ctx.call("climate", "set_hvac_mode", eid, hvac_mode=by_value[target]))
        elif values.get("Active") == 1 and _state(ctx, eid) == "off":
            if features & CLIMATE_TURN_ON:
                calls.append(ctx.call("climate", "turn_on", eid))
            else:  # no turn_on: pick the first mode HomeKit knows (auto, heat, cool)
                calls.append(ctx.call("climate", "set_hvac_mode", eid,
                                      hvac_mode=by_value[min(by_value)]))
        heat, cool = values.get("HeatingThresholdTemperature"), values.get("CoolingThresholdTemperature")
        if heat is not None or cool is not None:
            if ranged:
                low = ctx.from_celsius(heat) if heat is not None else ctx.attr(eid, "target_temp_low")
                high = ctx.from_celsius(cool) if cool is not None else ctx.attr(eid, "target_temp_high")
                calls.append(ctx.call("climate", "set_temperature", eid,
                                      target_temp_low=low, target_temp_high=high))
            else:
                # One setpoint: use the threshold for the mode it's in.
                mode = by_value.get(target) if target is not None else _state(ctx, eid)
                value = heat if (mode == "heat" or cool is None) else cool
                calls.append(ctx.call("climate", "set_temperature", eid,
                                      temperature=ctx.from_celsius(value)))
        if (speed := values.get("RotationSpeed")) is not None and speeds and speed > 0:
            index = min(max(math.ceil(speed / 100 * len(speeds)) - 1, 0), len(speeds) - 1)
            calls.append(ctx.call("climate", "set_fan_mode", eid, fan_mode=speeds[index]))
        if (swinging := values.get("SwingMode")) is not None and swing:
            calls.append(ctx.call("climate", "set_swing_mode", eid,
                                  swing_mode=swing[0] if swinging else swing[1]))
        return _run(*calls)

    svc.on_write(write)
    svc.char("Active", watch=[eid], get=lambda: int(_state(ctx, eid) not in ("off", "")))
    svc.char(
        "CurrentHeaterCoolerState",
        watch=[eid],
        get=lambda: _HC_ACTIONS.get(
            ctx.attr(eid, "hvac_action") or "", 0 if _state(ctx, eid) == "off" else 1
        ),
    )
    svc.char(
        "TargetHeaterCoolerState",
        watch=[eid],
        get=lambda: _HC_TARGETS.get(_state(ctx, eid)),
        valid_values={_HC_TARGET_NAMES[v]: v for v in sorted(by_value)},
    )
    svc.char(
        "CurrentTemperature",
        watch=[eid],
        get=lambda: ctx.to_celsius(ctx.attr(eid, "current_temperature")),
    )
    if 1 in by_value or 0 in by_value:
        svc.char("HeatingThresholdTemperature", watch=[eid],
                 get=lambda: setpoint("target_temp_low"), props=temp_props)
    if 2 in by_value or 0 in by_value:
        svc.char("CoolingThresholdTemperature", watch=[eid],
                 get=lambda: setpoint("target_temp_high"), props=temp_props)
    svc.char("TemperatureDisplayUnits", watch=[eid], get=lambda: int(ctx.uses_fahrenheit()))
    if len(speeds) >= 2:

        def speed() -> int | None:
            mode = ctx.attr(eid, "fan_mode")
            if mode not in speeds:
                return None  # e.g. "auto": keep the last speed shown
            return round((speeds.index(mode) + 1) / len(speeds) * 100)

        svc.char("RotationSpeed", watch=[eid], get=speed,
                 props={"minStep": 100 / len(speeds)})
    if swing:
        svc.char("SwingMode", watch=[eid], get=lambda: int(ctx.attr(eid, "swing_mode") == swing[0]))


# -- humidifier ---------------------------------------------------------------------

# HumidifierAction -> CurrentHumidifierDehumidifierState.
_HUMIDIFIER_ACTIONS = {"off": 0, "idle": 1, "humidifying": 2, "drying": 3}


def bind_humidifier(
    ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry, humidity: str | None = None
) -> None:
    """A humidifier or dehumidifier; `humidity` is an optional sensor entity."""
    eid = ent.entity_id
    caps = ent.capabilities or {}
    dehumidifier = (ent.device_class or ent.original_device_class) == "dehumidifier"
    target_state = 2 if dehumidifier else 1

    svc.char(
        "Active",
        watch=[eid],
        get=lambda: int(ctx.is_on(eid)),
        set=lambda v: ctx.call("humidifier", "turn_on" if v else "turn_off", eid),
    )

    def current() -> int:
        if (action := ctx.attr(eid, "action")) is not None:
            return _HUMIDIFIER_ACTIONS.get(action, 1)
        return 1 if ctx.is_on(eid) else 0

    svc.char("CurrentHumidifierDehumidifierState", watch=[eid], get=current)
    # The device is one or the other; that's the only target offered.
    svc.char(
        "TargetHumidifierDehumidifierState",
        watch=[eid],
        get=lambda: target_state,
        valid_values={"Dehumidifier" if dehumidifier else "Humidifier": target_state},
    )
    if humidity is not None:
        svc.char("CurrentRelativeHumidity", watch=[humidity], get=lambda: ctx.number(humidity))
    else:
        svc.char("CurrentRelativeHumidity", watch=[eid],
                 get=lambda: ctx.attr(eid, "current_humidity"))
    props = {
        k: v for k, v in (("minValue", caps.get("min_humidity")),
                          ("maxValue", caps.get("max_humidity"))) if v is not None
    }
    svc.char(
        "RelativeHumidityDehumidifierThreshold" if dehumidifier
        else "RelativeHumidityHumidifierThreshold",
        watch=[eid],
        get=lambda: ctx.attr(eid, "humidity"),
        set=lambda v: ctx.call("humidifier", "set_humidity", eid, humidity=v),
        props=props,
    )


# -- alarm panel -------------------------------------------------------------------

# alarm_control_panel state -> SecuritySystemCurrentState
# (0 stay/home, 1 away, 2 night, 3 disarmed, 4 triggered). Same as core HomeKit.
_ALARM_CURRENT = {
    "armed_home": 0, "armed_away": 1, "armed_vacation": 1, "armed_night": 2,
    "arming": 3, "disarmed": 3, "triggered": 4,
}
_ALARM_TARGET = {
    "armed_home": 0, "armed_away": 1, "armed_vacation": 1, "armed_night": 2,
    "arming": 1, "disarmed": 3,
}
_ALARM_SERVICES = {0: "alarm_arm_home", 1: "alarm_arm_away", 2: "alarm_arm_night",
                   3: "alarm_disarm"}


def bind_security_system(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Alarm panel as a SecuritySystem, offering only the arm modes it supports.

    Panels that need a code to arm or disarm will refuse: the Home app has
    nowhere to enter one.
    """
    eid = ent.entity_id
    features = ent.supported_features
    targets = {"Disarm": 3}
    if features & ALARM_ARM_HOME:
        targets["StayArm"] = 0
    if features & (ALARM_ARM_AWAY | ALARM_ARM_VACATION):
        targets["AwayArm"] = 1
    if features & ALARM_ARM_NIGHT:
        targets["NightArm"] = 2

    svc.char("SecuritySystemCurrentState", watch=[eid],
             get=lambda: _ALARM_CURRENT.get(_state(ctx, eid)))
    svc.char(
        "SecuritySystemTargetState",
        watch=[eid],
        get=lambda: _ALARM_TARGET.get(_state(ctx, eid)),
        set=lambda v: ctx.call("alarm_control_panel", _ALARM_SERVICES[v], eid),
        valid_values=dict(sorted(targets.items(), key=lambda kv: kv[1])),
    )


# -- valve, water heater -------------------------------------------------------------


def bind_valve(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id
    svc.char(
        "Active",
        watch=[eid],
        get=lambda: int(_state(ctx, eid) in ("open", "opening")),
        set=lambda v: ctx.call("valve", "open_valve" if v else "close_valve", eid),
    )
    svc.char("InUse", watch=[eid], get=lambda: int(_state(ctx, eid) == "open"))
    svc.char("ValveType", watch=[eid], get=lambda: 0)  # generic valve


def bind_water_heater(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Water heater as a heat-only Thermostat (as core HomeKit does)."""
    eid = ent.entity_id
    caps = ent.capabilities or {}
    on_off = bool(ent.supported_features & WATER_HEATER_ON_OFF)
    temp_props: dict[str, Any] = {}
    if (low := ctx.to_celsius(caps.get("min_temp"))) is not None:
        temp_props["minValue"] = low
    if (high := ctx.to_celsius(caps.get("max_temp"))) is not None:
        temp_props["maxValue"] = high

    def heating() -> int:
        return 0 if _state(ctx, eid) in ("off", "") else 1

    svc.char("CurrentHeatingCoolingState", watch=[eid], get=heating)
    svc.char(
        "TargetHeatingCoolingState",
        watch=[eid],
        get=heating,
        set=(lambda v: ctx.call("water_heater", "turn_on" if v else "turn_off", eid))
        if on_off else None,
        valid_values={"Off": 0, "Heat": 1} if on_off else {"Heat": 1},
    )
    svc.char("CurrentTemperature", watch=[eid],
             get=lambda: ctx.to_celsius(ctx.attr(eid, "current_temperature")))
    svc.char(
        "TargetTemperature",
        watch=[eid],
        get=lambda: ctx.to_celsius(ctx.attr(eid, "temperature")),
        set=lambda v: ctx.call("water_heater", "set_temperature", eid,
                               temperature=ctx.from_celsius(v)),
        props=temp_props,
    )
    svc.char("TemperatureDisplayUnits", watch=[eid], get=lambda: int(ctx.uses_fahrenheit()))


# -- things that become switches ----------------------------------------------------------


def bind_vacuum(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """On = cleaning; off = back to the dock (as core HomeKit does)."""
    eid = ent.entity_id
    features = ent.supported_features
    start = "start" if features & VACUUM_START else "turn_on"
    stop = "return_to_base" if features & VACUUM_RETURN_HOME else "turn_off"
    svc.char(
        "On",
        watch=[eid],
        get=lambda: _state(ctx, eid) in ("cleaning", "on"),
        set=lambda v: ctx.call("vacuum", start if v else stop, eid),
    )


def bind_lawn_mower(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id
    stop = "dock" if ent.supported_features & MOWER_DOCK else "pause"
    svc.char(
        "On",
        watch=[eid],
        get=lambda: _state(ctx, eid) == "mowing",
        set=lambda v: ctx.call("lawn_mower", "start_mowing" if v else stop, eid),
    )


# domain -> service that "presses" it
MOMENTARY = {"scene": "turn_on", "button": "press", "input_button": "press"}


def bind_momentary(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """A switch that runs a scene or presses a button, then turns itself off.

    Activating one updates its state (a timestamp), which re-reads `On` as
    False, so the tile flips back on its own.
    """
    eid = ent.entity_id
    service = MOMENTARY[ent.domain]
    svc.char(
        "On",
        watch=[eid],
        get=lambda: False,
        set=lambda v: ctx.call(ent.domain, service, eid) if v else None,
    )


def select_options(ctx: HapContext, ent: RegistryEntry) -> list[str]:
    caps = ent.capabilities or {}
    return list(caps.get("options") or ctx.attr(ent.entity_id, "options") or [])


def bind_select(
    ctx: HapContext, acc: Any, ent: RegistryEntry, options: Iterable[str]
) -> None:
    """One switch per option; turning one on selects it.

    Each switch is keyed on its option's text, so reordering options keeps
    automations pointing at the right one.
    """
    eid = ent.entity_id
    for i, option in enumerate(options):
        svc = acc.service("Switch", unique_id=f"option:{option}", primary=i == 0)
        svc.char("Name", watch=[eid], get=lambda option=option: option)
        svc.char(
            "On",
            watch=[eid],
            get=lambda option=option: _state(ctx, eid) == option,
            set=lambda v, option=option: (
                ctx.call(ent.domain, "select_option", eid, option=option) if v else None
            ),
        )
