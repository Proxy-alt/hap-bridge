"""Bundled profiles, used until integrations ship their own `hap_bridge.py`.

`PROFILES` match on device shape (what entities a device has), so they work
across integrations. `BY_INTEGRATION` holds ones that only make sense for a
specific integration's entity layout, keyed by integration domain.
`FINISHERS` run after the fallback, to add to whatever accessories exist.
"""

from __future__ import annotations

from collections.abc import Callable

from ..api import HapContext
from .fallback import bind_fan, bind_light, bind_thermostat


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


def _is_filter_life(ent) -> bool:
    return "filter" in (ent.translation_key or ent.entity_id) and (
        ent.unit_of_measurement in (None, "%")
    )


# AirQuality: 1 excellent .. 5 poor, from PM2.5 in µg/m³ (US EPA breakpoints).
_PM25_BANDS = ((12, 1), (35, 2), (55, 3), (150, 4))


def air_purifier(ctx: HapContext) -> None:
    """A fan with filter life and/or PM2.5 sensors is treated as a purifier."""
    fans = ctx.entities("fan")
    if ctx.device is None or len(fans) != 1:
        return
    filter_life = ctx.entity("sensor", predicate=_is_filter_life)
    pm25 = ctx.entity("sensor", device_class="pm25")
    if filter_life is None and pm25 is None:
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

    if filter_life is not None:
        filt = acc.service("FilterMaintenance", linked_to=purifier)
        filt.char("FilterLifeLevel", watch=[filter_life], get=lambda: ctx.number(filter_life))
        filt.char(
            "FilterChangeIndication",
            watch=[filter_life],
            get=lambda: int((ctx.number(filter_life) or 100) < 10),
        )

    if pm25 is not None:

        def air_quality() -> int:
            value = ctx.number(pm25)
            if value is None:
                return 0  # unknown
            return next((q for limit, q in _PM25_BANDS if value <= limit), 5)

        aq = acc.service("AirQualitySensor", linked_to=purifier)
        aq.char("AirQuality", watch=[pm25], get=air_quality)
        aq.char("PM2.5Density", watch=[pm25], get=lambda: ctx.number(pm25))


def thermostat(ctx: HapContext) -> None:
    """A climate entity, with the device's humidity sensor folded in if it has one."""
    climates = ctx.entities("climate")
    if ctx.device is None or len(climates) != 1:
        return
    humidity = ctx.entity("sensor", device_class="humidity")
    acc = ctx.accessory(category="THERMOSTAT")
    bind_thermostat(
        ctx, acc.service("Thermostat", primary=True), ctx.entry(climates[0]), humidity
    )


# StatusLowBattery below this level when there's no low-battery binary sensor.
LOW_BATTERY_PERCENT = 20


def battery(ctx: HapContext) -> None:
    """Link a Battery service to the device's tile if it has a battery level sensor."""
    if ctx.device is None or not ctx.accessories:
        return
    level = ctx.entity(
        "sensor",
        device_class="battery",
        predicate=lambda e: e.unit_of_measurement in (None, "%"),
    )
    if level is None:
        return
    low = ctx.entity("binary_sensor", device_class="battery")
    charging = ctx.entity("binary_sensor", device_class="battery_charging")

    acc = ctx.accessories[0]
    primary = next((s for s in acc.services if s.primary), None)
    svc = acc.service("BatteryService", linked_to=primary)
    svc.char("BatteryLevel", watch=[level], get=lambda: ctx.number(level))
    if low is not None:
        svc.char("StatusLowBattery", watch=[low], get=lambda: int(ctx.is_on(low)))
    else:
        svc.char(
            "StatusLowBattery",
            watch=[level],
            get=lambda: int((ctx.number(level) or 100) < LOW_BATTERY_PERCENT),
        )
    # ChargingState: 0 = not charging, 1 = charging, 2 = not chargeable.
    if charging is not None:
        svc.char("ChargingState", watch=[charging], get=lambda: int(ctx.is_on(charging)))
    else:
        svc.char("ChargingState", watch=[level], get=lambda: 2)


Profile = Callable[[HapContext], None]

# Purifier first: a purifier is also "a device with one fan".
PROFILES: list[Profile] = [air_purifier, fan_with_light, thermostat]

FINISHERS: list[Profile] = [battery]

BY_INTEGRATION: dict[str, list[Profile]] = {}
