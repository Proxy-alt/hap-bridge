"""TVs and receivers as HomeKit Televisions, with the Remote widget wired up.

What the Home app gets:
- power, inputs (sources and apps) and the active input;
- the Control Center Remote: arrows, select, back, exit, info and play/pause
  go to the TV's own remote where the integration has one (key names per
  integration below), else to media_player services. Every key press is
  also fired as a `hap_bridge_tv_remote_key` event for automations;
- volume from the iPhone's buttons while the Remote is open, mute, and a
  volume slider where the TV supports setting it;
- play/pause/stop state, and "View TV Settings" (opens the TV's menu);
- names given to the TV and its inputs in the Home app, and inputs hidden
  there, are kept across restarts.

TVs are standalone accessories: HomeKit expects them paired on their own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..api import HapContext, ServiceSpec

if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import RegistryEntry

    from ..api import AccessorySpec

EVENT_REMOTE_KEY = "hap_bridge_tv_remote_key"

# media_player feature bits
PAUSE = 1
VOLUME_SET = 4
VOLUME_MUTE = 8
PREVIOUS_TRACK = 16
NEXT_TRACK = 32
VOLUME_STEP = 1024
SELECT_SOURCE = 2048
STOP = 4096
PLAY = 16384
# remote feature bit
ACTIVITY = 4

# HomeKit allows at most this many inputs per TV (core HomeKit uses the same cap).
MAX_INPUTS = 90

# RemoteKey values the Home app sends -> our key names (as core HomeKit names them).
REMOTE_KEYS = {
    0: "rewind", 1: "fast_forward", 2: "next_track", 3: "previous_track",
    4: "arrow_up", 5: "arrow_down", 6: "arrow_left", 7: "arrow_right",
    8: "select", 9: "back", 10: "exit", 11: "play_pause", 15: "information",
}

# Per integration: the command its remote (or webostv.button) takes for each key.
# Taken from each integration's documented command names. "settings" is what
# "View TV Settings" in the Home app presses.
KEYMAPS: dict[str, dict[str, str]] = {
    "androidtv_remote": {
        "arrow_up": "DPAD_UP", "arrow_down": "DPAD_DOWN", "arrow_left": "DPAD_LEFT",
        "arrow_right": "DPAD_RIGHT", "select": "DPAD_CENTER", "back": "BACK", "exit": "HOME",
        "information": "INFO", "play_pause": "MEDIA_PLAY_PAUSE", "rewind": "MEDIA_REWIND",
        "fast_forward": "MEDIA_FAST_FORWARD", "next_track": "MEDIA_NEXT",
        "previous_track": "MEDIA_PREVIOUS", "settings": "SETTINGS",
    },
    "apple_tv": {
        "arrow_up": "up", "arrow_down": "down", "arrow_left": "left", "arrow_right": "right",
        "select": "select", "back": "menu", "exit": "home", "next_track": "next",
        "previous_track": "previous", "rewind": "skip_backward",
        "fast_forward": "skip_forward", "settings": "control_center",
    },
    "roku": {
        "arrow_up": "up", "arrow_down": "down", "arrow_left": "left", "arrow_right": "right",
        "select": "select", "back": "back", "exit": "home", "information": "info",
        "play_pause": "play", "rewind": "reverse", "fast_forward": "forward",
    },
    "samsungtv": {
        "arrow_up": "KEY_UP", "arrow_down": "KEY_DOWN", "arrow_left": "KEY_LEFT",
        "arrow_right": "KEY_RIGHT", "select": "KEY_ENTER", "back": "KEY_RETURN",
        "exit": "KEY_EXIT", "information": "KEY_INFO", "rewind": "KEY_REWIND",
        "fast_forward": "KEY_FF", "settings": "KEY_MENU",
    },
    "philips_js": {
        "arrow_up": "CursorUp", "arrow_down": "CursorDown", "arrow_left": "CursorLeft",
        "arrow_right": "CursorRight", "select": "Confirm", "back": "Back", "exit": "Exit",
        "information": "Info", "play_pause": "PlayPause", "rewind": "Rewind",
        "fast_forward": "FastForward", "settings": "Options",
    },
    # No remote entity: buttons go through webostv.button on the media player.
    "webostv": {
        "arrow_up": "UP", "arrow_down": "DOWN", "arrow_left": "LEFT", "arrow_right": "RIGHT",
        "select": "ENTER", "back": "BACK", "exit": "EXIT", "information": "INFO",
        "settings": "MENU",
    },
}
BUTTON_SERVICE = {"webostv": ("webostv", "button")}

# InputSourceType by name (0 = other): HDMI 3, tuner 2, AirPlay 8, USB 9, home 1.
_INPUT_TYPES = (
    ("hdmi", 3), ("airplay", 8), ("usb", 9), ("antenna", 2), ("tuner", 2), ("live tv", 2),
    ("cable", 2), ("home", 1), ("component", 6), ("composite", 4), ("dvi", 7),
)
_OFF_STATES = ("off", "standby", "unavailable", "unknown", "")
# media_player state -> CurrentMediaState (0 play, 1 pause, 2 stop, 4 loading).
_MEDIA_STATES = {"playing": 0, "paused": 1, "idle": 2, "on": 2, "buffering": 4}


def is_tv(ent: RegistryEntry) -> bool:
    device_class = ent.device_class or ent.original_device_class
    return device_class in ("tv", "receiver") or (
        device_class is None and ent.platform in KEYMAPS
    )


def is_activity_remote(ent: RegistryEntry) -> bool:
    return bool(ent.supported_features & ACTIVITY) and bool(
        (ent.capabilities or {}).get("activity_list")
    )


def input_type(name: str) -> int:
    lower = name.lower()
    return next((value for word, value in _INPUT_TYPES if word in lower), 0)


def _state(ctx: HapContext, eid: str) -> str:
    state = ctx.state(eid)
    return state.state if state else ""


def describe_tv(ctx: HapContext, ent: RegistryEntry, remote: str | None = None) -> AccessorySpec:
    """A media player (TV or receiver) as a standalone Television accessory.

    `remote` is the device's remote entity, used for the Remote widget's keys.
    """
    eid = ent.entity_id
    features = ent.supported_features
    receiver = (ent.device_class or ent.original_device_class) == "receiver"
    # 34 = audio receiver (pyhap has no constant for it)
    acc = ctx.accessory(
        ctx.name_of(eid) if ctx.device is None else None,
        category=34 if receiver else "TELEVISION",
        key=f"entity:{ent.id}" if ctx.device is None else None,
        standalone=True,
    )
    tv = acc.service("Television", primary=True)
    sources: list[str] = []
    if features & SELECT_SOURCE:
        # The live list first: the registry's copy is a snapshot from when the
        # entity last registered, and goes stale for TVs whose apps load later.
        caps = ent.capabilities or {}
        sources = list(dict.fromkeys(ctx.attr(eid, "source_list") or caps.get("source_list") or []))
    _bind_television(
        ctx, acc, tv, ent,
        power=lambda on: ctx.call("media_player", "turn_on" if on else "turn_off", eid),
        sources=sources[:MAX_INPUTS],
        current_source=lambda: ctx.attr(eid, "source"),
        select=lambda source: ctx.call("media_player", "select_source", eid, source=source),
        remote=remote,
    )
    if features & (PLAY | PAUSE):
        tv.char("CurrentMediaState", watch=[eid], get=lambda: _MEDIA_STATES.get(_state(ctx, eid), 2))

        def set_media(value: int):
            # TargetMediaState: 0 play, 1 pause, 2 stop.
            service = {0: "media_play", 1: "media_pause"}.get(value)
            if value == 2:
                service = "media_stop" if features & STOP else "media_pause"
            return ctx.call("media_player", service, eid) if service else None

        tv.char(
            "TargetMediaState",
            watch=[eid],
            get=lambda: min(_MEDIA_STATES.get(_state(ctx, eid), 2), 2),
            set=set_media,
        )
    if features & (VOLUME_MUTE | VOLUME_STEP | VOLUME_SET):
        _bind_speaker(ctx, acc, tv, ent)
    return acc


def describe_activity_remote(ctx: HapContext, ent: RegistryEntry) -> AccessorySpec:
    """A remote with activities (e.g. Harmony) as a Television whose inputs are activities."""
    eid = ent.entity_id
    acc = ctx.accessory(
        ctx.name_of(eid) if ctx.device is None else None,
        category="TELEVISION",
        key=f"entity:{ent.id}" if ctx.device is None else None,
        standalone=True,
    )
    tv = acc.service("Television", primary=True)
    activities = list(dict.fromkeys((ent.capabilities or {}).get("activity_list") or []))
    _bind_television(
        ctx, acc, tv, ent,
        power=lambda on: ctx.call("remote", "turn_on" if on else "turn_off", eid),
        sources=activities[:MAX_INPUTS],
        current_source=lambda: ctx.attr(eid, "current_activity"),
        select=lambda activity: ctx.call("remote", "turn_on", eid, activity=activity),
        remote=None,
    )
    return acc


def _bind_television(
    ctx: HapContext,
    acc: AccessorySpec,
    tv: ServiceSpec,
    ent: RegistryEntry,
    *,
    power: Any,
    sources: list[str],
    current_source: Any,
    select: Any,
    remote: str | None,
) -> None:
    eid = ent.entity_id
    settings = ctx.settings
    prefix = f"tv:{ent.id}"

    tv.char("Active", watch=[eid], get=lambda: int(_state(ctx, eid) not in _OFF_STATES),
            set=lambda v: power(bool(v)))
    tv.char("SleepDiscoveryMode", watch=[eid], get=lambda: 1)  # always discoverable
    default_name = acc.name
    tv.char(
        "ConfiguredName",
        watch=[eid],
        get=lambda: settings.get(f"{prefix}:name", default_name),
        set=lambda v: settings.set(f"{prefix}:name", v),
    )
    tv.char("RemoteKey", watch=[eid], set=lambda v: _press(ctx, ent, remote, REMOTE_KEYS.get(v)))
    # "View TV Settings" in the Home app writes 0 here.
    tv.char("PowerModeSelection", watch=[eid], set=lambda _v: _press(ctx, ent, remote, "settings"))

    if not sources:
        tv.char("ActiveIdentifier", watch=[eid], get=lambda: 0)  # required; no inputs
        return

    def active() -> int | None:
        source = current_source()
        return sources.index(source) if source in sources else None

    tv.char(
        "ActiveIdentifier",
        watch=[eid],
        get=active,
        set=lambda v: select(sources[v]) if 0 <= v < len(sources) else None,
    )
    for index, source in enumerate(sources):
        key = f"{prefix}:input:{source}"
        # Keyed on the source's name: its HomeKit IDs survive the list reordering.
        svc = acc.service("InputSource", unique_id=f"input:{source}", linked_to=tv)
        svc.char("Name", watch=[eid], get=lambda source=source: source)
        svc.char(
            "ConfiguredName",
            watch=[eid],
            get=lambda key=key, source=source: settings.get(f"{key}:name", source),
            set=lambda v, key=key: settings.set(f"{key}:name", v),
        )
        svc.char("Identifier", watch=[eid], get=lambda index=index: index)
        svc.char("IsConfigured", watch=[eid], get=lambda: 1)
        svc.char("InputSourceType", watch=[eid], get=lambda source=source: input_type(source))
        # 0 shown, 1 hidden. The Home app writes the target; we keep and echo it.
        hidden = lambda key=key: int(settings.get(f"{key}:hidden", False))  # noqa: E731
        svc.char("CurrentVisibilityState", watch=[eid], get=hidden)
        svc.char(
            "TargetVisibilityState",
            watch=[eid],
            get=hidden,
            set=lambda v, key=key: settings.set(f"{key}:hidden", bool(v)),
        )


def _bind_speaker(ctx: HapContext, acc: AccessorySpec, tv: ServiceSpec, ent: RegistryEntry) -> None:
    """The Remote's volume (iPhone volume buttons), mute and, if settable, level."""
    eid = ent.entity_id
    features = ent.supported_features
    speaker = acc.service("TelevisionSpeaker", linked_to=tv)
    speaker.char("Active", watch=[eid], get=lambda: 1)
    # VolumeControlType: 1 relative (up/down only), 3 absolute (has a level).
    absolute = bool(features & VOLUME_SET)
    speaker.char("VolumeControlType", watch=[eid], get=lambda: 3 if absolute else 1)
    speaker.char(
        "VolumeSelector",  # 0 = up, 1 = down
        watch=[eid],
        set=lambda v: ctx.call("media_player", "volume_down" if v else "volume_up", eid),
    )
    if features & VOLUME_MUTE:
        speaker.char(
            "Mute",
            watch=[eid],
            get=lambda: bool(ctx.attr(eid, "is_volume_muted")),
            set=lambda v: ctx.call("media_player", "volume_mute", eid, is_volume_muted=bool(v)),
        )
    else:
        speaker.char("Mute", watch=[eid], get=lambda: False)
    if absolute:

        def volume() -> int | None:
            level = ctx.attr(eid, "volume_level")
            return None if level is None else round(level * 100)

        speaker.char(
            "Volume",
            watch=[eid],
            get=volume,
            set=lambda v: ctx.call("media_player", "volume_set", eid, volume_level=v / 100),
        )


def _press(ctx: HapContext, ent: RegistryEntry, remote: str | None, key: str | None):
    """Send a Remote widget key to the TV, and tell automations about it."""
    if key is None:
        return None
    eid = ent.entity_id
    features = ent.supported_features
    command = KEYMAPS.get(ent.platform, {}).get(key)
    handled = True
    if key == "play_pause" and ent.domain == "media_player" and features & (PLAY | PAUSE):
        # Like core HomeKit: toggle through the media player, which knows the state.
        state = _state(ctx, eid)
        service = {"playing": "media_pause", "paused": "media_play"}.get(state, "media_play_pause")
        call = ctx.call("media_player", service, eid)
    elif command is not None and ent.platform in BUTTON_SERVICE:
        domain, service = BUTTON_SERVICE[ent.platform]
        call = ctx.hass.services.async_call(domain, service, {"entity_id": eid, "button": command})
    elif command is not None and remote is not None:
        call = ctx.call("remote", "send_command", remote, command=[command])
    elif key in ("next_track", "previous_track") and ent.domain == "media_player" and (
        features & (NEXT_TRACK if key == "next_track" else PREVIOUS_TRACK)
    ):
        call = ctx.call("media_player", f"media_{key}", eid)
    else:
        call, handled = None, False
    ctx.hass.bus.async_fire(EVENT_REMOTE_KEY, {"entity_id": eid, "key": key, "handled": handled})
    return call
