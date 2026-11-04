"""Bundled profiles, used until integrations ship their own `hap_bridge.py`.

`PROFILES` match on device shape (what entities a device has), so they work
across integrations. `BY_INTEGRATION` holds ones that only make sense for a
specific integration's entity layout, keyed by integration domain.
`FINISHERS` run after the fallback, to add to whatever accessories exist.
"""

from __future__ import annotations

from collections.abc import Callable

from ..api import HapContext
from . import appliances, cameras, tv
from .fallback import (
    AIR_POLLUTANTS,
    bind_air_quality,
    bind_battery,
    bind_button,
    bind_doorbell,
    bind_fan,
    bind_filter,
    bind_garage_door,
    bind_light,
    bind_thermostat,
    button_presses,
    low_battery,
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
        bind_filter(ctx, acc.service("FilterMaintenance", linked_to=purifier),
                    life=filter_life, change=filter_change, reset=filter_reset)

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


def television(ctx: HapContext) -> None:
    """A TV or receiver, with the device's remote entity driving the Remote widget."""
    if ctx.device is None:
        return
    players = ctx.entities("media_player", predicate=tv.is_tv)
    if not players:
        return
    remote = ctx.entity("remote")
    tv.describe_tv(ctx, ctx.entry(players[0]), remote)
    if remote is not None:
        ctx.claim(remote)


def activity_remote(ctx: HapContext) -> None:
    """A remote with activities (e.g. Harmony), when there's no TV on the device."""
    if ctx.device is None:
        return
    if (remote := ctx.entity("remote", predicate=tv.is_activity_remote)) is not None:
        tv.describe_activity_remote(ctx, ctx.entry(remote))
        ctx.claim(remote)


def camera(ctx: HapContext) -> None:
    """A camera, as a video doorbell if the device has a doorbell, with its motion sensor."""
    if ctx.device is None:
        return
    cameras_ = ctx.entities("camera")
    if not cameras_:
        return
    bell = ctx.entity("event", device_class="doorbell")
    motion = ctx.entity("binary_sensor", device_class="motion")
    cameras.describe_camera(ctx, cameras_[0], doorbell=bell, motion=motion)
    for eid in cameras_[1:]:  # e.g. a second lens: its own accessory
        cameras.describe_camera(ctx, eid, name=ctx.name_of(eid))


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

    is_low = low_battery(ctx, level, low)

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
    bind_battery(ctx, acc.service("BatteryService", linked_to=primary), level,
                 low=low, charging=charging)

Profile = Callable[[HapContext], None]

# Purifier first: a purifier is also "a device with one fan".
# Camera before doorbell: a doorbell with a camera is a video doorbell.
PROFILES: list[Profile] = [
    camera, television, activity_remote,
    air_purifier, fan_with_light, thermostat, humidifier, doorbell, garage_door,
    air_quality_sensor, buttons,
]

def adaptive_lighting(ctx: HapContext) -> None:
    """Offer Adaptive Lighting on every Lightbulb that can run it.

    That's one with Brightness, ColorTemperature and an on_write, whose
    ColorTemperature watches a light that supports color temperature, and
    whose profile didn't opt out. Bundled lights already ask for it; this
    catches the ones integrations and shims build by hand.
    """
    for acc in ctx.accessories:
        for svc in acc.services:
            if (
                svc.type_name != "Lightbulb"
                or svc.adaptive_light is not None
                or svc.adaptive_wanted is False
                or "ColorTemperature" not in svc.chars
                # Asked for explicitly: resolve the light and let validation
                # explain anything else that's missing.
                or (not svc.adaptive_wanted and ("Brightness" not in svc.chars or svc.write is None))
            ):
                continue
            light = next(
                (eid for eid in svc.chars["ColorTemperature"].watch if _has_color_temp(ctx, eid)),
                None,
            )
            if light is not None:
                svc.adaptive_light = light


def _has_color_temp(ctx: HapContext, entity_id: str) -> bool:
    ent = ctx.entry(entity_id)
    if ent is None or ent.domain != "light":
        return False
    return "color_temp" in ((ent.capabilities or {}).get("supported_color_modes") or [])


FINISHERS: list[Profile] = [device_status, adaptive_lighting]

BY_INTEGRATION: dict[str, list[Profile]] = {}
