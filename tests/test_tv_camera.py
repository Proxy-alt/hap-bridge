from hb.api import HapContext, Settings
from hb.profiles import tv

from .conftest import make_device, make_entry
from .test_profiles import _assert_valid, _run, _values

TV_FEATURES = (tv.PLAY | tv.PAUSE | tv.VOLUME_SET | tv.VOLUME_MUTE | tv.VOLUME_STEP
               | tv.SELECT_SOURCE | tv.NEXT_TRACK)


def _tv(hass, platform="androidtv_remote", with_remote=True, settings=None, **state):
    entries = [make_entry(
        "media_player.living_room", platform=platform, device_class="tv",
        supported_features=TV_FEATURES,
        capabilities={"source_list": ["HDMI 1", "Netflix", "Live TV", "HDMI 1"]},
    )]
    if with_remote:
        entries.append(make_entry("remote.living_room", platform=platform))
    hass.states.set("media_player.living_room", state.pop("state", "playing"),
                    source="Netflix", volume_level=0.3, is_volume_muted=False, **state)
    ctx = HapContext(hass, make_device(), entries, settings)
    _run(ctx)
    return ctx


def _services(acc, type_name):
    return [s for s in acc.services if s.type_name == type_name]


def test_tv_is_a_standalone_television_with_inputs(hass, loader):
    ctx = _tv(hass)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    assert acc.standalone and acc.category == "TELEVISION"
    (television,) = _services(acc, "Television")
    inputs = _services(acc, "InputSource")
    # Duplicate source names are dropped; each input is keyed on its name.
    assert [s.chars["Name"].get() for s in inputs] == ["HDMI 1", "Netflix", "Live TV"]
    assert [s.unique_id for s in inputs] == ["input:HDMI 1", "input:Netflix", "input:Live TV"]
    assert [s.chars["InputSourceType"].get() for s in inputs] == [3, 0, 2]
    assert television.linked[:3] == inputs
    values = _values(acc)
    assert values["Television.Active"] == 1
    assert values["Television.ActiveIdentifier"] == 1
    assert values["Television.CurrentMediaState"] == 0
    assert values["TelevisionSpeaker.Volume"] == 30
    assert values["TelevisionSpeaker.VolumeControlType"] == 3

    television.chars["ActiveIdentifier"].set(2)
    assert hass.services.calls[-1] == (
        "media_player", "select_source", {"entity_id": "media_player.living_room", "source": "Live TV"})


def test_remote_keys_go_to_the_tvs_remote(hass):
    ctx = _tv(hass)
    television = _services(ctx.accessories[0], "Television")[0]
    press = television.chars["RemoteKey"].set
    press(4)   # arrow up
    press(8)   # select
    press(11)  # play/pause: through the media player, which knows it's playing
    press(12)  # not a key HomeKit defines: ignored
    television.chars["PowerModeSelection"].set(0)  # View TV Settings
    calls = hass.services.calls
    assert calls[0] == ("remote", "send_command",
                        {"entity_id": "remote.living_room", "command": ["DPAD_UP"]})
    assert calls[1][2]["command"] == ["DPAD_CENTER"]
    assert calls[2][:2] == ("media_player", "media_pause")
    assert calls[3][2]["command"] == ["SETTINGS"]
    assert [e[1]["key"] for e in hass.bus.events] == ["arrow_up", "select", "play_pause", "settings"]
    assert all(e[0] == tv.EVENT_REMOTE_KEY and e[1]["handled"] for e in hass.bus.events)


def test_webos_buttons_and_unknown_tvs(hass):
    ctx = _tv(hass, platform="webostv", with_remote=False)
    _services(ctx.accessories[0], "Television")[0].chars["RemoteKey"].set(9)  # back
    assert hass.services.calls[-1] == (
        "webostv", "button", {"entity_id": "media_player.living_room", "button": "BACK"})

    ctx = _tv(hass, platform="some_tv", with_remote=False)
    _services(ctx.accessories[0], "Television")[0].chars["RemoteKey"].set(4)
    # No way to send arrows: automations get the event and can handle it.
    assert hass.bus.events[-1][1] == {
        "entity_id": "media_player.living_room", "key": "arrow_up", "handled": False}


def test_names_and_hidden_inputs_are_kept(hass):
    settings = Settings()
    ctx = _tv(hass, settings=settings)
    acc = ctx.accessories[0]
    television = _services(acc, "Television")[0]
    netflix = _services(acc, "InputSource")[1]
    television.chars["ConfiguredName"].set("Den TV")
    netflix.chars["ConfiguredName"].set("Films")
    netflix.chars["TargetVisibilityState"].set(1)

    ctx = _tv(hass, settings=settings)  # e.g. after a restart
    acc = ctx.accessories[0]
    netflix = _services(acc, "InputSource")[1]
    assert _services(acc, "Television")[0].chars["ConfiguredName"].get() == "Den TV"
    assert netflix.chars["ConfiguredName"].get() == "Films"
    assert netflix.chars["CurrentVisibilityState"].get() == 1


def test_receivers_and_activity_remotes(hass, loader):
    hass.states.set("media_player.avr", "on", source="Blu-ray")
    ctx = HapContext(hass, None, [make_entry(
        "media_player.avr", device_class="receiver", supported_features=tv.SELECT_SOURCE,
        capabilities={"source_list": ["Blu-ray", "TV"]})])
    _run(ctx)
    _assert_valid(ctx, loader)
    assert ctx.accessories[0].category == 34

    hass.states.set("remote.harmony", "on", current_activity="Watch TV")
    ctx = HapContext(hass, make_device(), [make_entry(
        "remote.harmony", supported_features=tv.ACTIVITY,
        capabilities={"activity_list": ["Watch TV", "Play Xbox"]})])
    _run(ctx)
    _assert_valid(ctx, loader)
    television = _services(ctx.accessories[0], "Television")[0]
    assert television.chars["ActiveIdentifier"].get() == 0
    television.chars["ActiveIdentifier"].set(1)
    assert hass.services.calls[-1] == (
        "remote", "turn_on", {"entity_id": "remote.harmony", "activity": "Play Xbox"})


def test_plain_speakers_are_not_tvs(hass):
    hass.states.set("media_player.kitchen", "playing")
    ctx = HapContext(hass, None, [make_entry("media_player.kitchen", device_class="speaker")])
    _run(ctx)
    assert ctx.accessories == []


def test_video_doorbell(hass, loader):
    hass.states.set("camera.front", "idle")
    hass.states.set("event.front_ring", "2026-11-01T10:00:00+00:00")
    hass.states.set("binary_sensor.front_motion", "off")
    ctx = HapContext(hass, make_device(), [
        make_entry("camera.front"),
        make_entry("event.front_ring", device_class="doorbell"),
        make_entry("binary_sensor.front_motion", device_class="motion"),
    ])
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    assert acc.camera == "camera.front" and acc.standalone
    assert acc.category == "VIDEO_DOOR_BELL"
    assert [s.type_name for s in acc.services] == [
        "Doorbell", "StatelessProgrammableSwitch", "Speaker", "MotionSensor"]
    hass.states.set("camera.front", "unavailable")
    assert not acc.is_available()


def test_plain_camera(hass, loader):
    hass.states.set("camera.yard", "idle")
    ctx = HapContext(hass, None, [make_entry("camera.yard")])
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    assert (acc.camera, acc.category, acc.services) == ("camera.yard", "CAMERA", [])


def _event_camera(hass, *, motion_class="motion"):
    hass.states.set("camera.porch", "idle")
    hass.states.set("event.porch", "2026-11-01T10:00:00+00:00", event_type="motion")
    ctx = HapContext(hass, make_device(), [
        make_entry("camera.porch"),
        make_entry("event.porch", device_class=motion_class),
    ])
    _run(ctx)
    return ctx


def test_camera_records_on_a_motion_event_entity(hass, loader):
    from datetime import datetime, timezone

    ctx = _event_camera(hass)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    assert acc.recording.motion == ("event.porch",)
    motion = _services(acc, "MotionSensor")[0].chars["MotionDetected"]
    # An old event isn't motion now, and nothing will change by itself.
    assert motion.get() is False and motion.recheck() is None
    assert acc.motion_active() is False
    hass.states.set("event.porch", datetime.now(timezone.utc).isoformat(), event_type="motion")
    assert motion.get() is True and acc.motion_active() is True
    assert 15 < motion.recheck() <= 20


def test_event_entity_without_motion_class_isnt_a_trigger(hass):
    ctx = _event_camera(hass, motion_class=None)
    (acc,) = ctx.accessories
    assert acc.recording is None


def test_motion_events_hold_and_filter(hass):
    from datetime import datetime, timedelta, timezone

    from hb.profiles.cameras import MotionTriggers

    ctx = HapContext(hass, None, [])
    clock = [datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)]
    trig = MotionTriggers(ctx, ["event.cam", "binary_sensor.pir"], ["motion", "person"],
                          now=lambda: clock[0])
    hass.states.set("binary_sensor.pir", "off")

    def fire(kind, seconds_ago=0):
        when = clock[0] - timedelta(seconds=seconds_ago)
        hass.states.set("event.cam", when.isoformat(), event_type=kind)

    fire("sound")  # not a kind that counts
    assert not trig.is_on() and trig.seconds_left() is None
    fire("person", seconds_ago=5)
    assert trig.is_on() and trig.seconds_left() == 15
    # A sound right after doesn't cut the person's motion short.
    fire("sound")
    assert trig.is_on() and trig.seconds_left() == 15
    clock[0] += timedelta(seconds=16)
    assert not trig.is_on() and trig.seconds_left() is None
    # Binary sensors still count by their state.
    hass.states.set("binary_sensor.pir", "on")
    assert trig.is_on() and trig.seconds_left() is None
    hass.states.set("binary_sensor.pir", "off")
    hass.states.set("event.cam", "unavailable")
    assert not trig.is_on()
