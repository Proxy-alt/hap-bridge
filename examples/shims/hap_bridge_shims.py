"""Example profile pack: a custom integration shipping shims for others.

Put this file as `hap_bridge_shims.py` in your own custom integration (e.g.
one installed from HACS). When that integration loads, HAP Bridge picks up
every entry in SHIMS; no import from HAP Bridge is needed.
"""


async def _robot_vacuum(ctx):
    vacuum = ctx.entity("vacuum")
    if vacuum is None:
        return
    acc = ctx.accessory(category="SWITCH")
    ctx.blocks.vacuum(acc.service("Switch", primary=True), vacuum)
    if (battery := ctx.entity("sensor", device_class="battery")) is not None:
        ctx.blocks.battery(acc.service("BatteryService"), battery)


SHIMS = [
    {
        "name": "acme_robot_vacuum",
        "handles": {"integration": "acme_cloud", "model": "Robo*"},
        "describe": _robot_vacuum,
        "api": 1,  # optional: the HAP Bridge API version it needs
    },
]
