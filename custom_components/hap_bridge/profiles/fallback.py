"""Per-entity mapping for anything no profile claimed.

Roughly what the core HomeKit Bridge does today. The `bind_*` helpers are
also reused by richer profiles so one entity type is always mapped one way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..api import HapContext, ServiceSpec
from . import appliances, tv

if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import RegistryEntry

# Feature bits from the HA entity models.
FAN_SET_SPEED = 1
FAN_OSCILLATE = 2
FAN_DIRECTION = 4
FAN_PRESET_MODE = 8
COVER_SET_POSITION = 4
COVER_SET_TILT_POSITION = 128
CLIMATE_TARGET_TEMPERATURE_RANGE = 2


def describe_entity(ctx: HapContext, ent: RegistryEntry) -> None:
    # Config/diagnostic entities and hidden ones are for profiles to use, not tiles.
    if ent.entity_category is not None or ent.hidden_by is not None:
        return
    builder = _BUILDERS.get(ent.domain)
    if builder is not None:
        builder(ctx, ent)


def why_not(ent: RegistryEntry) -> str:
    """Why the fallback leaves an entity out (when nothing else used it)."""
    if ent.entity_category is not None:
        return f"{ent.entity_category} entity"
    if ent.hidden_by is not None:
        return "hidden"
    if ent.domain not in _BUILDERS:
        return f"HomeKit has no mapping for {ent.domain} entities"
    if (device_class := _device_class(ent)) is not None:
        return f"HomeKit has no mapping for this {ent.domain} (device class {device_class})"
    return f"HomeKit has no mapping for this {ent.domain}"


def _accessory(ctx: HapContext, ent: RegistryEntry, category: str):
    return ctx.accessory(ctx.name_of(ent.entity_id), category=category, key=f"entity:{ent.id}")


def _device_class(ent: RegistryEntry) -> str | None:
    return ent.device_class or ent.original_device_class


# -- binders ------------------------------------------------------------------


# Color modes HA can drive from an hs_color (it converts for xy/rgb* lights).
_HS_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


def bind_light(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """On/off, brightness, hue/saturation and color temperature, as one write.

    The Home app sends e.g. On + Brightness + Hue together; they become a
    single light.turn_on instead of one call per characteristic.
    """
    eid = ent.entity_id
    caps = ent.capabilities or {}
    modes = set(caps.get("supported_color_modes") or [])

    def write(values: dict[str, Any]):
        if values.get("On") is False or values.get("Brightness") == 0:
            return ctx.call("light", "turn_off", eid)
        data: dict[str, Any] = {}
        if (brightness := values.get("Brightness")) is not None:
            data["brightness_pct"] = brightness
        # When the Home app sends both, the temperature is the newer intent.
        if (mireds := values.get("ColorTemperature")) is not None:
            data["color_temp_kelvin"] = round(1_000_000 / mireds)
        elif "Hue" in values or "Saturation" in values:
            hue, sat = ctx.attr(eid, "hs_color") or (0, 0)
            data["hs_color"] = (values.get("Hue", hue), values.get("Saturation", sat))
        return ctx.call("light", "turn_on", eid, **data)

    svc.on_write(write)
    svc.char("On", watch=[eid], get=lambda: ctx.is_on(eid))
    if modes - {"onoff"}:
        # Off lights have no brightness/color; keep showing the last one.
        svc.char(
            "Brightness",
            watch=[eid],
            get=lambda: _scale(ctx.attr(eid, "brightness"), 255, 100),
        )
    if modes & _HS_MODES:
        svc.char("Hue", watch=[eid], get=lambda: _index(ctx.attr(eid, "hs_color"), 0))
        svc.char("Saturation", watch=[eid], get=lambda: _index(ctx.attr(eid, "hs_color"), 1))
    if "color_temp" in modes:
        props: dict[str, Any] = {}
        # Warmest kelvin = most mireds.
        if max_k := caps.get("max_color_temp_kelvin"):
            props["minValue"] = 1_000_000 // max_k
        if min_k := caps.get("min_color_temp_kelvin"):
            props["maxValue"] = -(-1_000_000 // min_k)

        def mireds() -> int | None:
            kelvin = ctx.attr(eid, "color_temp_kelvin")
            return round(1_000_000 / kelvin) if kelvin else None

        svc.char("ColorTemperature", watch=[eid], get=mireds, props=props)
        svc.adaptive_lighting(eid)


def _scale(value: float | None, source_max: float, target_max: float) -> int | None:
    return None if value is None else round(value / source_max * target_max)


def _index(values: Any, i: int) -> Any:
    return values[i] if values else None


def _auto_preset(ctx: HapContext, eid: str) -> str | None:
    modes = ctx.attr(eid, "preset_modes") or []
    return next((m for m in modes if m.lower() == "auto"), None)


def bind_fan(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Fan controls; on Fanv2 also direction and an auto preset."""
    eid = ent.entity_id
    features = ent.supported_features
    svc.char(
        "Active",
        watch=[eid],
        get=lambda: int(ctx.is_on(eid)),
        set=lambda v: ctx.call("fan", "turn_on" if v else "turn_off", eid),
    )
    if features & FAN_SET_SPEED:
        svc.char(
            "RotationSpeed",
            watch=[eid],
            get=lambda: ctx.attr(eid, "percentage") or 0,
            set=lambda v: ctx.call("fan", "set_percentage", eid, percentage=v),
        )
    if features & FAN_OSCILLATE:
        svc.char(
            "SwingMode",
            watch=[eid],
            get=lambda: int(bool(ctx.attr(eid, "oscillating"))),
            set=lambda v: ctx.call("fan", "oscillate", eid, oscillating=bool(v)),
        )
    if svc.type_name != "Fanv2":
        return  # e.g. AirPurifier: no direction, and its own auto/manual state
    if features & FAN_DIRECTION:
        # RotationDirection: 0 = clockwise (forward), 1 = counter-clockwise.
        svc.char(
            "RotationDirection",
            watch=[eid],
            get=lambda: int(ctx.attr(eid, "direction") == "reverse"),
            set=lambda v: ctx.call(
                "fan", "set_direction", eid, direction="reverse" if v else "forward"
            ),
        )
    if features & FAN_PRESET_MODE and _auto_preset(ctx, eid):

        def set_target(value: int):
            # TargetFanState: 0 = manual, 1 = auto.
            if value and (preset := _auto_preset(ctx, eid)):
                return ctx.call("fan", "set_preset_mode", eid, preset_mode=preset)
            return ctx.call(
                "fan", "set_percentage", eid, percentage=ctx.attr(eid, "percentage") or 50
            )

        svc.char(
            "TargetFanState",
            watch=[eid],
            get=lambda: int((ctx.attr(eid, "preset_mode") or "").lower() == "auto"),
            set=set_target,
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


def bind_garage_door(
    ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry, obstruction: str | None = None
) -> None:
    """Garage door cover; `obstruction` is an optional binary sensor entity."""
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
    if obstruction is not None:
        svc.char("ObstructionDetected", watch=[obstruction], get=lambda: ctx.is_on(obstruction))
    else:
        svc.char("ObstructionDetected", watch=[eid], get=lambda: False)


def _new_events(ctx: HapContext, eid: str) -> Callable[[], bool]:
    """True once per new event of an `event` entity.

    An event entity's state is the timestamp of its last event, so a new
    event is a new timestamp. Comparing against the last *real* timestamp
    keeps startup and unavailable -> available from firing.
    """

    def stamp() -> str | None:
        current = ctx.state(eid)
        if current is None or current.state in ("unavailable", "unknown"):
            return None
        return current.state

    last = [stamp()]

    def fired() -> bool:
        now = stamp()
        if now is None or now == last[0]:
            return False
        last[0] = now
        return True

    return fired


def bind_doorbell(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Doorbell `event` entity: each new event is one ring."""
    fired = _new_events(ctx, ent.entity_id)
    # ProgrammableSwitchEvent 0 = single press.
    svc.char("ProgrammableSwitchEvent", watch=[ent.entity_id], get=lambda: 0 if fired() else None)


# Button event_type -> ProgrammableSwitchEvent. Integrations name these
# freely; these are the common spellings.
_PRESS_EVENTS = {
    "press": 0, "single_press": 0, "short_press": 0, "short_release": 0, "initial_press": 0,
    "double_press": 1,
    "long_press": 2, "long_release": 2, "hold": 2,
}
_PRESS_NAMES = {0: "SinglePress", 1: "DoublePress", 2: "LongPress"}


def button_presses(ent: RegistryEntry) -> dict[str, int]:
    """The event types of a button `event` entity that HomeKit can represent."""
    types = (ent.capabilities or {}).get("event_types") or []
    return {t: _PRESS_EVENTS[t] for t in types if t in _PRESS_EVENTS}


def bind_button(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """Button `event` entity as a stateless programmable switch.

    Only the press kinds it can send are offered, so the Home app doesn't
    show a "double press" trigger for a button that never double-presses.
    """
    eid = ent.entity_id
    presses = button_presses(ent)
    fired = _new_events(ctx, eid)

    def press() -> int | None:
        if not fired():
            return None
        return presses.get(ctx.attr(eid, "event_type"))

    svc.char(
        "ProgrammableSwitchEvent",
        watch=[eid],
        get=press,
        valid_values={_PRESS_NAMES[v]: v for v in sorted(set(presses.values()))},
    )


# Pollutant sensor device class -> (density characteristic, AirQuality bands).
# Bands are upper limits for 1 (excellent) .. 4 (inferior); above is 5 (poor).
# Same thresholds as core HomeKit (US AQI for PM, WHO IAQ for VOC).
AIR_POLLUTANTS: dict[str, tuple[str, tuple[float, ...] | None]] = {
    "pm25": ("PM2.5Density", (9, 35.4, 55.4, 125.4)),
    "pm10": ("PM10Density", (54, 154, 254, 354)),
    "nitrogen_dioxide": ("NitrogenDioxideDensity", (30, 60, 80, 90)),
    "volatile_organic_compounds": ("VOCDensity", (250, 500, 1000, 3000)),
    "ozone": ("OzoneDensity", None),
    "sulphur_dioxide": ("SulphurDioxideDensity", None),
}


def bind_air_quality(ctx: HapContext, svc: ServiceSpec, sensors: dict[str, str]) -> None:
    """AirQuality from the worst pollutant, plus each one's density.

    `sensors` maps pollutant device class (keys of AIR_POLLUTANTS) to entity.
    """

    def air_quality() -> int:
        levels = []
        for device_class, eid in sensors.items():
            bands = AIR_POLLUTANTS[device_class][1]
            if bands is not None and (value := ctx.number(eid)) is not None:
                levels.append(next((q for q, limit in enumerate(bands, 1) if value <= limit), 5))
        return max(levels, default=0)  # 0 = unknown

    svc.char("AirQuality", watch=list(sensors.values()), get=air_quality)
    for device_class, eid in sensors.items():
        svc.char(AIR_POLLUTANTS[device_class][0], watch=[eid], get=lambda eid=eid: ctx.number(eid))



def bind_filter(
    ctx: HapContext,
    svc: ServiceSpec,
    *,
    life: str | None = None,
    change: str | None = None,
    reset: str | None = None,
) -> None:
    """FilterMaintenance from a filter-life sensor (%), a "replace filter" binary
    sensor and/or a "reset filter" button. Needs `life` or `change`."""
    if life is not None:
        svc.char("FilterLifeLevel", watch=[life], get=lambda: ctx.number(life))
    if change is not None:
        svc.char("FilterChangeIndication", watch=[change], get=lambda: int(ctx.is_on(change)))
    elif life is not None:
        svc.char("FilterChangeIndication", watch=[life],
                 get=lambda: int((ctx.number(life) or 100) < 10))
    if reset is not None:
        # Write-only in HomeKit: the Home app's "reset filter" presses the button.
        svc.char("ResetFilterIndication", watch=[reset],
                 set=lambda _v: ctx.call("button", "press", reset))


# StatusLowBattery below this level when there's no low-battery binary sensor.
LOW_BATTERY_PERCENT = 20


def low_battery(ctx: HapContext, level: str | None, low: str | None) -> Callable[[], int]:
    def is_low() -> int:
        if low is not None:
            return int(ctx.is_on(low))
        return int((ctx.number(level) or 100) < LOW_BATTERY_PERCENT)

    return is_low


def bind_battery(
    ctx: HapContext,
    svc: ServiceSpec,
    level: str,
    *,
    low: str | None = None,
    charging: str | None = None,
) -> None:
    """BatteryService from a level sensor (%), optional low-battery and charging sensors."""
    svc.char("BatteryLevel", watch=[level], get=lambda: ctx.number(level))
    svc.char("StatusLowBattery", watch=[e for e in (low, level) if e],
             get=low_battery(ctx, level, low))
    # ChargingState: 0 = not charging, 1 = charging, 2 = not chargeable.
    if charging is not None:
        svc.char("ChargingState", watch=[charging], get=lambda: int(ctx.is_on(charging)))
    else:
        svc.char("ChargingState", watch=[level], get=lambda: 2)

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


# Level sensors that also get a "detected" flag above core HomeKit's thresholds.
_GAS_SENSORS = {
    "carbon_monoxide": ("CarbonMonoxideSensor", "CarbonMonoxide", 25),
    "carbon_dioxide": ("CarbonDioxideSensor", "CarbonDioxide", 1000),
}


_SENSOR_SERVICES = {
    "temperature": "TemperatureSensor",
    "humidity": "HumiditySensor",
    "illuminance": "LightSensor",
    **{dc: service for dc, (service, _p, _t) in _GAS_SENSORS.items()},
    **{dc: "AirQualitySensor" for dc in AIR_POLLUTANTS},
}

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
    "carbon_monoxide": ("CarbonMonoxideSensor", "CarbonMonoxideDetected", int),
    # Core HomeKit does the same: HAP has no generic gas sensor.
    "gas": ("CarbonMonoxideSensor", "CarbonMonoxideDetected", int),
}


def sensor_service(ent: RegistryEntry) -> str | None:
    """The HomeKit service for a sensor or binary sensor, or None if there's none."""
    device_class = _device_class(ent) or ""
    if ent.domain == "binary_sensor":
        mapping = _BINARY_SENSORS.get(device_class)
        return mapping[0] if mapping else None
    if ent.domain == "sensor":
        return _SENSOR_SERVICES.get(device_class)
    return None


def bind_sensor(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """A sensor or binary sensor on the service `sensor_service(ent)` names."""
    eid = ent.entity_id
    device_class = _device_class(ent) or ""
    if ent.domain == "binary_sensor":
        _service, char, convert = _BINARY_SENSORS[device_class]
        svc.char(char, watch=[eid], get=lambda: convert(ctx.is_on(eid)))
    elif device_class == "temperature":

        def celsius() -> float | None:
            value = ctx.number(eid)
            if value is not None and ctx.attr(eid, "unit_of_measurement") == "°F":
                value = (value - 32) * 5 / 9
            return value

        svc.char("CurrentTemperature", watch=[eid], get=celsius)
    elif device_class == "humidity":
        svc.char("CurrentRelativeHumidity", watch=[eid], get=lambda: ctx.number(eid))
    elif device_class == "illuminance":
        # HAP's minimum is 0.0001 lx; 0 would be rejected.
        svc.char(
            "CurrentAmbientLightLevel",
            watch=[eid],
            get=lambda: None if (lux := ctx.number(eid)) is None else max(lux, 0.0001),
        )
    elif device_class in _GAS_SENSORS:
        _service, prefix, threshold = _GAS_SENSORS[device_class]
        svc.char(f"{prefix}Level", watch=[eid], get=lambda: ctx.number(eid))
        svc.char(
            f"{prefix}Detected",
            watch=[eid],
            get=lambda: int((ctx.number(eid) or 0) > threshold),
        )
    elif device_class in AIR_POLLUTANTS:
        bind_air_quality(ctx, svc, {device_class: eid})


_LOCK_STATES = {"unlocked": 0, "locked": 1, "jammed": 2}


def bind_lock(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    eid = ent.entity_id

    def current() -> int:
        state = ctx.state(eid)
        return _LOCK_STATES.get(state.state if state else "", 3)

    def target() -> int | None:
        state = ctx.state(eid)
        if state is None:
            return None
        return 1 if state.state in ("locked", "locking") else 0

    svc.char("LockCurrentState", watch=[eid], get=current)
    svc.char(
        "LockTargetState",
        watch=[eid],
        get=target,
        set=lambda v: ctx.call("lock", "lock" if v else "unlock", eid),
    )


def bind_window_covering(ctx: HapContext, svc: ServiceSpec, ent: RegistryEntry) -> None:
    """A cover with a settable position (and tilt, if it has one)."""
    eid = ent.entity_id
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
    if ent.supported_features & COVER_SET_TILT_POSITION:
        # HA tilt is 0..100; HAP's is an angle, -90..90.
        def angle() -> int | None:
            tilt = ctx.attr(eid, "current_tilt_position")
            return None if tilt is None else round(tilt / 100 * 180 - 90)

        svc.char("CurrentHorizontalTiltAngle", watch=[eid], get=angle)
        svc.char(
            "TargetHorizontalTiltAngle",
            watch=[eid],
            get=angle,
            set=lambda v: ctx.call(
                "cover", "set_cover_tilt_position", eid, tilt_position=round((v + 90) / 180 * 100)
            ),
        )


def _sensor(ctx: HapContext, ent: RegistryEntry) -> None:
    if (service := sensor_service(ent)) is not None:
        bind_sensor(ctx, _accessory(ctx, ent, "SENSOR").service(service, primary=True), ent)


def _lock(ctx: HapContext, ent: RegistryEntry) -> None:
    bind_lock(ctx, _accessory(ctx, ent, "DOOR_LOCK").service("LockMechanism", primary=True), ent)


def _cover(ctx: HapContext, ent: RegistryEntry) -> None:
    if _device_class(ent) == "garage":
        bind_garage_door(
            ctx,
            _accessory(ctx, ent, "GARAGE_DOOR_OPENER").service("GarageDoorOpener", primary=True),
            ent,
        )
    elif ent.supported_features & COVER_SET_POSITION:
        acc = _accessory(ctx, ent, "WINDOW_COVERING")
        bind_window_covering(ctx, acc.service("WindowCovering", primary=True), ent)


def _climate(ctx: HapContext, ent: RegistryEntry) -> None:
    if appliances.uses_heater_cooler(ent):
        acc = _accessory(ctx, ent, appliances.heater_cooler_category(ent))
        appliances.bind_heater_cooler(ctx, acc.service("HeaterCooler", primary=True), ent)
    else:
        acc = _accessory(ctx, ent, "THERMOSTAT")
        bind_thermostat(ctx, acc.service("Thermostat", primary=True), ent)


def _simple(category: str, service: str, binder: Callable[..., None]):
    """A builder for one entity -> one accessory with one service."""

    def build(ctx: HapContext, ent: RegistryEntry) -> None:
        binder(ctx, _accessory(ctx, ent, category).service(service, primary=True), ent)

    return build


def _valve(ctx: HapContext, ent: RegistryEntry) -> None:
    if ent.supported_features & (appliances.VALVE_OPEN | appliances.VALVE_CLOSE):
        appliances.bind_valve(ctx, _accessory(ctx, ent, "FAUCET").service("Valve", primary=True), ent)


def _media_player(ctx: HapContext, ent: RegistryEntry) -> None:
    if tv.is_tv(ent):
        tv.describe_tv(ctx, ent)


def _remote(ctx: HapContext, ent: RegistryEntry) -> None:
    if tv.is_activity_remote(ent):
        tv.describe_activity_remote(ctx, ent)


def _camera(ctx: HapContext, ent: RegistryEntry) -> None:
    from .cameras import describe_camera  # cameras imports this module

    describe_camera(ctx, ent.entity_id, name=ctx.name_of(ent.entity_id), key=f"entity:{ent.id}")


def _button(ctx: HapContext, ent: RegistryEntry) -> None:
    # Restart/update/identify buttons are maintenance, not something to tap in Home.
    if _device_class(ent) not in ("restart", "update", "identify"):
        _simple("SWITCH", "Switch", appliances.bind_momentary)(ctx, ent)


def _select(ctx: HapContext, ent: RegistryEntry) -> None:
    if options := appliances.select_options(ctx, ent):
        appliances.bind_select(ctx, _accessory(ctx, ent, "SWITCH"), ent, options)


def _event(ctx: HapContext, ent: RegistryEntry) -> None:
    device_class = _device_class(ent)
    if device_class == "doorbell":
        svc = _accessory(ctx, ent, "VIDEO_DOOR_BELL").service("Doorbell", primary=True)
        bind_doorbell(ctx, svc, ent)
    elif device_class == "button" and button_presses(ent):
        svc = _accessory(ctx, ent, "PROGRAMMABLE_SWITCH").service(
            "StatelessProgrammableSwitch", primary=True
        )
        bind_button(ctx, svc, ent)


_BUILDERS: dict[str, Callable[[HapContext, RegistryEntry], None]] = {
    "light": _light,
    "switch": _switch,
    "input_boolean": _switch,
    "fan": _fan,
    "sensor": _sensor,
    "binary_sensor": _sensor,
    "lock": _lock,
    "cover": _cover,
    "climate": _climate,
    "event": _event,
    "humidifier": _simple("HUMIDIFIER", "HumidifierDehumidifier", appliances.bind_humidifier),
    "alarm_control_panel": _simple(
        "ALARM_SYSTEM", "SecuritySystem", appliances.bind_security_system
    ),
    "valve": _valve,
    "water_heater": _simple("THERMOSTAT", "Thermostat", appliances.bind_water_heater),
    "vacuum": _simple("SWITCH", "Switch", appliances.bind_vacuum),
    "lawn_mower": _simple("SWITCH", "Switch", appliances.bind_lawn_mower),
    "scene": _simple("SWITCH", "Switch", appliances.bind_momentary),
    "button": _button,
    "input_button": _simple("SWITCH", "Switch", appliances.bind_momentary),
    "script": _switch,
    "select": _select,
    "input_select": _select,
    "media_player": _media_player,
    "remote": _remote,
    "camera": _camera,
}
