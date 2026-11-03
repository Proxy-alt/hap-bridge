"""Starter HomeKit profile for an integration. Copy to <your_integration>/hap_bridge.py.

HAP Bridge calls `async_describe_device` once per device of your integration
when the bridge starts. Nothing here imports HAP Bridge, so the file is
harmless when HAP Bridge isn't installed. Whatever you don't claim falls
through to HAP Bridge's own mapping.

Check the result without pairing: Developer tools → Actions →
"HAP Bridge: Preview" with your device. Test it in your test suite with
`custom_components.hap_bridge.testing` (see the README).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # types only; never imported at runtime
    from custom_components.hap_bridge.api import HapContext

# The HAP Bridge API version this file needs. Older bridges skip it (with a
# repair issue saying so) instead of failing halfway.
HAP_BRIDGE_API = 1


async def async_describe_device(ctx: HapContext) -> None:
    # 1. Find this device's entities. Only unclaimed ones are returned.
    fan = ctx.entity("fan")
    filter_life = ctx.entity("sensor", translation_key="filter_life")
    reset_filter = ctx.entity("button", translation_key="reset_filter")
    pm25 = ctx.entity("sensor", device_class="pm25")
    if fan is None:
        return  # not a device this profile knows; leave it to HAP Bridge

    # 2. One accessory: one tile in the Home app.
    acc = ctx.accessory(category="AIR_PURIFIER")

    # 3. Services, using the bridge's tested blocks where they fit...
    purifier = ctx.blocks.fan(acc.service(ctx.hap.Service.AIR_PURIFIER, primary=True), fan)

    # ...and plain characteristics where they don't. `get` reruns whenever a
    # watched entity changes; `set` gets what the Home app wrote.
    hap = ctx.hap
    purifier.char(
        hap.Char.CURRENT_AIR_PURIFIER_STATE,
        watch=[fan],
        get=lambda: hap.CurrentAirPurifierState.PURIFYING_AIR
        if ctx.is_on(fan) else hap.CurrentAirPurifierState.INACTIVE,
    )
    purifier.char(
        hap.Char.TARGET_AIR_PURIFIER_STATE,
        watch=[fan],
        get=lambda: hap.TargetAirPurifierState.AUTO
        if ctx.attr(fan, "preset_mode") == "auto" else hap.TargetAirPurifierState.MANUAL,
        set=lambda v: ctx.call("fan", "set_preset_mode", fan,
                               preset_mode="auto" if v else "manual"),
    )

    # 4. Linked services show up inside the purifier's tile.
    if filter_life:
        ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=purifier),
                          life=filter_life, reset=reset_filter)
    if pm25:
        ctx.blocks.air_quality(acc.service("AirQualitySensor", linked_to=purifier), pm25=pm25)

    # Need the device itself (not an entity)? Your config entry's runtime_data:
    #   client = ctx.runtime_data("your_domain")
