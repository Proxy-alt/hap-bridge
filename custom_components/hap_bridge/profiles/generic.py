"""Bundled profiles, used until integrations ship their own `hap_bridge.py`.

`PROFILES` match on device shape (what entities a device has), so they work
across integrations. `BY_INTEGRATION` holds ones that only make sense for a
specific integration's entity layout, keyed by integration domain.
`FINISHERS` run after the fallback, to add to whatever accessories exist.
"""

from __future__ import annotations

from collections.abc import Callable

from ..api import HapContext
from . import appliances
from .fallback import (
    AIR_POLLUTANTS,
    bind_air_quality,
    bind_button,
    bind_doorbell,
    bind_fan,
    bind_garage_door,
    bind_light,
    bind_thermostat,
    button_presses,
)


def fan_with_light(ctx: HapContext) -> None:
    """Ceiling fan with a light: one tile with the light as a linked service."""
    fans, lights = ctx.entities("fan"), ctx.entities("light")
    if ctx.device is None or len(fans) != 1 or len(lights) != 1:
        return
    fan, light = ctx.entry(fans[0]), ctx.entry(lights[0])
    acc = ctx.accessory(category="FAN")
    fan_svc = acc.service("Fanv2", primary=True)
    bind_fan(ctx, fan_svc, fan)
    bind_light(ctx, acc.service("Lightbulb", linked_to=fan_svc), light)


def _mentions_filter(ent) -> bool:
    return "filter" in (ent.translation_key or ent.entity_id)


def _is_filter_life(ent) -> bool:
    return _mentions_filter(ent) and ent.unit_of_measurement in (None, "%")


def _pollutants(ctx: HapContext) -> dict[str, str]:
    """The device's unclaimed pollutant sensors, by device class."""
    found = {}
    for device_class in AIR_POLLUTANTS:
        if (eid := ctx.entity("sensor", device_class=device_class)) is not None:
            found[device_class] = eid
    return found


def air_purifier(ctx: HapContext) -> None:
    """A fan with filter life and/or PM2.5 sensors is treated as a purifier."""
    fans = ctx.entities("fan")
    if ctx.device is None or len(fans) != 1:
        return
    filter_life = ctx.entity("sensor", predicate=_is_filter_life)
    # A "replace filter" binary sensor and a "reset filter" button, if it has them.
    filter_change = ctx.entity("binary_sensor", predicate=_mentions_filter)
    filter_reset = ctx.entity("button", predicate=_mentions_filter)
    pollutants = _pollutants(ctx)
    if filter_life is None and filter_change is None and not pollutants:
        return

    fan = fans[0]
    fan_entry = ctx.entry(fan)
    acc = ctx.accessory(category="AIR_PURIFIER")
    purifier = acc.service("AirPurifier", primary=True)
    bind_fan(ctx, purifier, fan_entry)

    def auto_preset() -> str | None:
        modes = ctx.attr(fan, "preset_modes") or []
        return next((m for m in modes if m.lower() == "auto"), None)

    def set_target(value: int):
        # TargetAirPurifierState: 0 = manual, 1 = auto.
        if value == 1 and (preset := auto_preset()):
            return ctx.call("fan", "set_preset_mode", fan, preset_mode=preset)
        if value == 0:
            return ctx.call("fan", "set_percentage", fan, percentage=ctx.attr(fan, "percentage") or 50)
        return None

    # CurrentAirPurifierState: 0 = inactive, 2 = purifying.
    purifier.char("CurrentAirPurifierState", watch=[fan], get=lambda: 2 if ctx.is_on(fan) else 0)
    purifier.char(
        "TargetAirPurifierState",
        watch=[fan],
        get=lambda: int((ctx.attr(fan, "preset_mode") or "").lower() == "auto"),
        set=set_target,
    )

    if filter_life is not None or filter_change is not None:
        filt = acc.service("FilterMaintenance", linked_to=purifier)
        if filter_life is not None:
            filt.char("FilterLifeLevel", watch=[filter_life], get=lambda: ctx.number(filter_life))
        if filter_change is not None:
            filt.char("FilterChangeIndication", watch=[filter_change],
                      get=lambda: int(ctx.is_on(filter_change)))
        else:
            filt.char(
                "FilterChangeIndication",
                watch=[filter_life],
                get=lambda: int((ctx.number(filter_life) or 100) < 10),
            )
        if filter_reset is not None:
            # Write-only in HomeKit: the Home app's "reset filter" presses the button.
            filt.char("ResetFilterIndication", watch=[filter_reset],
                      set=lambda _v: ctx.call("button", "press", filter_reset))

    if pollutants:
        bind_air_quality(ctx, acc.service("AirQualitySensor", linked_to=purifier), pollutants)

def thermostat(ctx: HapContext) -> None:
    """A climate entity: AC-style units as HeaterCooler, others as Thermostat
    with the device's humidity sensor folded in if it has one."""
    climates = ctx.entities("climate")
    if ctx.device is None or len(climates) != 1:
        return
    climate = ctx.entry(climates[0])
    if appliances.uses_heater_cooler(climate):
        acc = ctx.accessory(category=appliances.heater_cooler_category(climate))
        appliances.bind_heater_cooler(ctx, acc.service("HeaterCooler", primary=True), climate)
        return  # HeaterCooler has no humidity; the sensor gets its own tile
    humidity = ctx.entity("sensor", device_class="humidity")
    acc = ctx.accessory(category="THERMOSTAT")
    bind_thermostat(ctx, acc.service("Thermostat", primary=True), climate, humidity)


def humidifier(ctx: HapContext) -> None:
    """A humidifier with the device's humidity sensor folded in."""
    humidifiers = ctx.entities("humidifier")
    if ctx.device is None or len(humidifiers) != 1:
        return
    humidity = ctx.entity("sensor", device_class="humidity")
    acc = ctx.accessory(category="HUMIDIFIER")
    appliances.bind_humidifier(
        ctx, acc.service("HumidifierDehumidifier", primary=True),
        ctx.entry(humidifiers[0]), humidity,
    )


def doorbell(ctx: HapContext) -> None:
    """A doorbell with the device's motion sensor linked to it."""
    if ctx.device is None:
        return
    bell = ctx.entity("event", device_class="doorbell")
    if bell is None:
        return
    motion = ctx.entity("binary_sensor", device_class="motion")
    acc = ctx.accessory(category="VIDEO_DOOR_BELL")
    svc = acc.service("Doorbell", primary=True)
    bind_doorbell(ctx, svc, ctx.entry(bell))
    if motion is not None:
        acc.service("MotionSensor", linked_to=svc).char(
            "MotionDetected", watch=[motion], get=lambda: ctx.is_on(motion)
        )


def garage_door(ctx: HapContext) -> None:
    """A garage door with the device's obstruction sensor wired to ObstructionDetected."""
    if ctx.device is None:
        return
    door = ctx.entity("cover", device_class="garage")
    if door is None:
        return
    obstruction = ctx.entity("binary_sensor", device_class="safety") or ctx.entity(
        "binary_sensor", device_class="problem"
    )
    acc = ctx.accessory(category="GARAGE_DOOR_OPENER")
    bind_garage_door(
        ctx, acc.service("GarageDoorOpener", primary=True), ctx.entry(door), obstruction
    )


def air_quality_sensor(ctx: HapContext) -> None:
    """All of a device's pollutant sensors as one air quality tile."""
    if ctx.device is None or not (pollutants := _pollutants(ctx)):
        return
    acc = ctx.accessory(category="SENSOR")
    bind_air_quality(ctx, acc.service("AirQualitySensor", primary=True), pollutants)


def buttons(ctx: HapContext) -> None:
    """A multi-button remote as one accessory with a numbered switch per button."""
    if ctx.device is None:
        return
    eids = ctx.entities(
        "event", device_class="button", predicate=lambda e: bool(button_presses(e))
    )
    if len(eids) < 2:
        return  # a single button is left to the fallback
    acc = ctx.accessory(category="PROGRAMMABLE_SWITCH")
    # Several switches on one accessory need a ServiceLabel saying how they're numbered.
    acc.service("ServiceLabel").char(
        "ServiceLabelNamespace", watch=[eids[0]], get=lambda: 1  # 1 = arabic numerals
    )
    for index, eid in enumerate(eids, 1):
        svc = acc.service("StatelessProgrammableSwitch", primary=index == 1)
        bind_button(ctx, svc, ctx.entry(eid))
        svc.char("ServiceLabelIndex", watch=[eid], get=lambda index=index: index)


# StatusLowBattery below this level when there's no low-battery binary sensor.
LOW_BATTERY_PERCENT = 20


# Sensor services that carry StatusLowBattery / StatusTampered / StatusFault.
_STATUS_SERVICES = {
    "AirQualitySensor", "CarbonDioxideSensor", "CarbonMonoxideSensor", "ContactSensor",
    "HumiditySensor", "LeakSensor", "LightSensor", "MotionSensor", "OccupancySensor",
    "SmokeSensor", "TemperatureSensor",
}


def device_status(ctx: HapContext) -> None:
    """Battery and health from the device's diagnostic entities.

    A battery level sensor adds a linked Battery service to the device's
    first tile. Low battery, tamper and problem sensors also set the status
    flags on every sensor tile of the device, which the Home app badges.
    """
    if ctx.device is None or not ctx.accessories:
        return
    level = ctx.entity(
        "sensor",
        device_class="battery",
        predicate=lambda e: e.unit_of_measurement in (None, "%"),
    )
    low = ctx.entity("binary_sensor", device_class="battery")
    charging = ctx.entity("binary_sensor", device_class="battery_charging")
    tamper = ctx.entity("binary_sensor", device_class="tamper")
    problem = ctx.entity("binary_sensor", device_class="problem")

    def is_low() -> int:
        if low is not None:
            return int(ctx.is_on(low))
        return int((ctx.number(level) or 100) < LOW_BATTERY_PERCENT)

    battery_watch = [e for e in (low, level) if e is not None]
    flags = []
    if battery_watch:
        flags.append(("StatusLowBattery", battery_watch, is_low))
    if tamper is not None:
        flags.append(("StatusTampered", [tamper], lambda: int(ctx.is_on(tamper))))
    if problem is not None:
        flags.append(("StatusFault", [problem], lambda: int(ctx.is_on(problem))))
    for acc in ctx.accessories:
        for svc in acc.services:
            if svc.type_name in _STATUS_SERVICES:
                for name, watch, get in flags:
                    svc.char(name, watch=watch, get=get, affects_available=False)

    if level is None:
        return
    acc = ctx.accessories[0]
    primary = next((s for s in acc.services if s.primary), None)
    svc = acc.service("BatteryService", linked_to=primary)
    svc.char("BatteryLevel", watch=[level], get=lambda: ctx.number(level))
    svc.char("StatusLowBattery", watch=battery_watch, get=is_low)
    # ChargingState: 0 = not charging, 1 = charging, 2 = not chargeable.
    if charging is not None:
        svc.char("ChargingState", watch=[charging], get=lambda: int(ctx.is_on(charging)))
    else:
        svc.char("ChargingState", watch=[level], get=lambda: 2)

Profile = Callable[[HapContext], None]

# Purifier first: a purifier is also "a device with one fan".
PROFILES: list[Profile] = [
    air_purifier, fan_with_light, thermostat, humidifier, doorbell, garage_door,
    air_quality_sensor, buttons,
]

FINISHERS: list[Profile] = [device_status]

BY_INTEGRATION: dict[str, list[Profile]] = {}
