"""Shim: SamsungTV Smart (custom integration `samsungtv_smart`) as a HomeKit TV.

Copy to <config>/hap_bridge/profiles/samsungtv_smart.py, check it with the
"HAP Bridge: Preview" action on the TV, then reload the bridge.

The bundled TV profile already builds the Television (power, inputs from the
source list and apps, volume, play/pause) for this integration's media player,
but only knows key names for the core `samsungtv`. This shim builds the same
Television and routes the Control Center Remote to this integration's remote
entity, which takes Samsung `KEY_*` names (a command is sent to the TV as a
`send_key` through the media player).

Inputs come from the media player's `source_list` attribute when the bridge
starts, so start (or reload) the bridge with the TV on if it's empty.
"""

HAP_BRIDGE_API = 1

HANDLES = {"integration": "samsungtv_smart"}

# Remote widget key -> Samsung key code. Play/pause and next/previous go through
# the media player instead, which knows the state.
KEYS = {
    "arrow_up": "KEY_UP", "arrow_down": "KEY_DOWN", "arrow_left": "KEY_LEFT",
    "arrow_right": "KEY_RIGHT", "select": "KEY_ENTER", "back": "KEY_RETURN",
    "exit": "KEY_EXIT", "information": "KEY_INFO", "rewind": "KEY_REWIND",
    "fast_forward": "KEY_FF", "settings": "KEY_MENU",
}
# RemoteKey values the Home app sends.
REMOTE_KEYS = {
    0: "rewind", 1: "fast_forward", 2: "next_track", 3: "previous_track",
    4: "arrow_up", 5: "arrow_down", 6: "arrow_left", 7: "arrow_right",
    8: "select", 9: "back", 10: "exit", 11: "play_pause", 15: "information",
}
EVENT = "hap_bridge_tv_remote_key"


async def async_describe_device(ctx):
    player = ctx.entity("media_player")
    if player is None:
        return
    remote = ctx.entity("remote")
    acc = ctx.blocks.tv(player, remote=remote)
    television = next(s for s in acc.services if s.type_name == "Television")

    def press(key):
        if key is None:
            return None
        handled = True
        if key == "play_pause":
            state = ctx.state(player)
            service = {"playing": "media_pause", "paused": "media_play"}.get(
                state.state if state else "", "media_play_pause")
            call = ctx.call("media_player", service, player)
        elif key in ("next_track", "previous_track"):
            call = ctx.call("media_player", f"media_{key}", player)
        elif key in KEYS and remote is not None:
            call = ctx.call("remote", "send_command", remote, command=[KEYS[key]])
        else:
            call, handled = None, False
        ctx.hass.bus.async_fire(EVENT, {"entity_id": player, "key": key, "handled": handled})
        return call

    television.char("RemoteKey", watch=[player], set=lambda v: press(REMOTE_KEYS.get(v)))
    # "View TV Settings" in the Home app.
    television.char("PowerModeSelection", watch=[player], set=lambda _v: press("settings"))
