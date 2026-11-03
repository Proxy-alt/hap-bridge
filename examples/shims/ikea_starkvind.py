"""Example shim: the IKEA STARKVIND air purifier on ZHA as one purifier tile.

Copy to <config>/hap_bridge/profiles/ikea_starkvind.py, check it with the
"HAP Bridge: Preview" action on the purifier, then reload the bridge.

Entity lookups below match ZHA's entities for this model; adjust them for
your setup (the preview lists every entity the shim left out, and why).
"""

HAP_BRIDGE_API = 1

# Which devices this shim is for: case-insensitive patterns, all must match.
# A model match is the most specific, so it wins over broader shims.
HANDLES = {"integration": "zha", "manufacturer": "IKEA*", "model": "STARKVIND*"}


async def async_describe_device(ctx):
    fan = ctx.entity("fan")
    if fan is None:
        return
    filter_life = ctx.entity("sensor", translation_key="filter_life_time") or ctx.entity(
        "sensor", predicate=lambda e: "filter" in e.entity_id)
    replace_filter = ctx.entity("binary_sensor", predicate=lambda e: "filter" in e.entity_id)
    pm25 = ctx.entity("sensor", device_class="pm25")
    hap = ctx.hap

    acc = ctx.accessory(category="AIR_PURIFIER")
    purifier = ctx.blocks.fan(acc.service(hap.Service.AIR_PURIFIER, primary=True), fan)
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
        if (ctx.attr(fan, "preset_mode") or "").lower() == "auto"
        else hap.TargetAirPurifierState.MANUAL,
        set=lambda v: ctx.call("fan", "set_preset_mode", fan, preset_mode="auto")
        if v else ctx.call("fan", "set_percentage", fan, percentage=ctx.attr(fan, "percentage") or 50),
    )
    if filter_life or replace_filter:
        ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=purifier),
                          life=filter_life, change=replace_filter)
    if pm25:
        ctx.blocks.air_quality(acc.service("AirQualitySensor", linked_to=purifier), pm25=pm25)
