"""Adaptive Lighting protocol and curve math.

Payloads are built the way HAP-NodeJS's AdaptiveLightingController tests
build them, and the expected values match the ones those tests assert.
"""

import base64
import struct

import pytest

from hb.transition import (
    Transition,
    TransitionError,
    handle_control_write,
    supported_configuration,
    tlv,
    untlv,
    write_uint,
)

HOUR = 3_600_000
TEMP_IID, BRIGHTNESS_IID = 12, 11


def curve(length, temperature=200):
    return [
        (temperature + i * 10, -0.5, 0 if i == 0 else HOUR, 0) for i in range(length)
    ]


def update(entries, *, iid=TEMP_IID, update_interval=60_000, unknown_3=True):
    params = tlv(0x01, b"\xab" * 16, 0x02, (123_456).to_bytes(8, "little"))
    raw_entries = []
    for temperature, factor, offset, duration in entries:
        entry = tlv(0x01, struct.pack("<f", factor), 0x02, struct.pack("<f", temperature),
                    0x03, write_uint(offset))
        if duration is not None:
            entry += tlv(0x04, write_uint(duration))
        raw_entries.append(entry)
    curve_tlv = tlv(
        0x01, raw_entries,
        0x02, write_uint(BRIGHTNESS_IID),
        0x03, tlv(0x01, (10).to_bytes(4, "little"), 0x02, (100).to_bytes(4, "little")),
    )
    config = tlv(
        0x01, write_uint(iid),
        0x02, params,
        *((0x03, 1) if unknown_3 else ()),
        0x05, curve_tlv,
        0x06, update_interval.to_bytes(2, "little"),
        0x08, (600_000).to_bytes(4, "little"),
    )
    return base64.b64encode(tlv(0x02, tlv(0x01, config))).decode()


def write(value, active=None, now=0):
    return handle_control_write(
        value, active=active, temperature_iid=TEMP_IID, brightness_iid=BRIGHTNESS_IID, now_ms=now
    )


def test_tlv_round_trips_lists_and_long_values():
    long = bytes(range(256)) * 2
    data = tlv(0x01, [b"a", b"bc"], 0x02, long, 0x03, 7)
    decoded = untlv(data)
    assert decoded[0x01] == [b"a", b"bc"]
    assert decoded[0x02] == long
    assert decoded[0x03] == b"\x07"


def test_supported_configuration_lists_both_characteristics():
    decoded = untlv(base64.b64decode(supported_configuration(BRIGHTNESS_IID, TEMP_IID)))
    first, second = decoded[0x01]
    assert untlv(first) == {0x01: bytes([BRIGHTNESS_IID]), 0x02: b"\x01"}
    assert untlv(second) == {0x01: bytes([TEMP_IID]), 0x02: b"\x02"}


def test_enable_parses_curve_and_answers_with_status():
    result = write(update(curve(5)), now=1000)
    t = result.transition
    assert t is not None and not result.disable
    assert [e.mireds for e in t.curve] == [200, 210, 220, 230, 240]
    assert (t.min_brightness, t.max_brightness, t.update_interval_ms) == (10, 100, 60_000)
    assert t.received_ms == 1000

    response = untlv(base64.b64decode(result.response))
    status = untlv(untlv(response[0x02])[0x01])
    assert status[0x01] == bytes([TEMP_IID])
    assert untlv(status[0x02])[0x01] == b"\xab" * 16
    assert status[0x03] == b"\x00"


def test_zero_length_segment_jumps_to_upper_bound():
    # HAP-NodeJS: "300 mired at the upper bound, adjusted by -0.5 * 50% brightness" = 275
    entries = [(200, -0.5, 0, 0), (300, -0.5, 0, 0), (400, -0.5, HOUR, None)]
    t = write(update(entries)).transition
    assert t.mireds_at(0, 50, 140, 500) == 275


def test_brightness_adjustment_is_clamped_to_range():
    t = write(update(curve(5))).transition
    # HAP-NodeJS: one minute in at 100% brightness = 150
    assert t.mireds_at(60_000, 100, 140, 500) == 150
    # Below the 10% floor counts as 10%.
    assert t.mireds_at(0, 1, 140, 500) == t.mireds_at(0, 10, 140, 500) == 195


def test_interpolates_and_clamps_to_the_light():
    t = write(update(curve(5))).transition
    assert t.mireds_at(HOUR // 2, 10, 140, 500) == round(205 - 5)
    assert t.mireds_at(HOUR // 2, 10, 140, 180) == 180


def test_hold_time_keeps_value_before_moving_on():
    # Entry 2's DURATION is how long entry 1 holds.
    entries = [(200, 0, 0, None), (300, 0, HOUR, 0), (400, 0, HOUR, HOUR)]
    t = write(update(entries)).transition
    assert t.curve[1].hold_ms == HOUR
    assert t.mireds_at(HOUR + HOUR // 2, 50, 140, 500) == 300   # holding
    assert t.mireds_at(2 * HOUR + HOUR // 2, 50, 140, 500) == 350  # moving on


def test_curve_end_and_backwards_clock():
    t = write(update(curve(3))).transition
    assert t.mireds_at(2 * HOUR, 50, 140, 500) is not None
    assert t.mireds_at(2 * HOUR + 1, 50, 140, 500) is None
    assert t.ended(3 * HOUR)
    assert t.mireds_at(0, 50, 140, 500) == 175  # cursor reset after going back


def test_disable_and_read():
    active = write(update(curve(5))).transition

    read = base64.b64encode(tlv(0x01, tlv(0x01, write_uint(TEMP_IID)))).decode()
    described = untlv(untlv(base64.b64decode(write(read, active).response))[0x01])
    assert described[0x01] == bytes([TEMP_IID])
    entries = untlv(described[0x05])[0x01]
    assert len(entries) == 5

    bad_read = base64.b64encode(tlv(0x01, tlv(0x01, write_uint(BRIGHTNESS_IID)))).decode()
    with pytest.raises(TransitionError):
        write(bad_read, active)

    off = write(update(curve(5), unknown_3=False))
    assert off.disable and off.transition is None
    assert untlv(base64.b64decode(off.response)) == {0x02: b""}


def test_rejects_malformed_updates():
    only_iid = base64.b64encode(
        tlv(0x02, tlv(0x01, tlv(0x01, write_uint(TEMP_IID), 0x03, 1)))
    ).decode()
    with pytest.raises(TransitionError):
        write(only_iid)
    with pytest.raises(TransitionError):
        write(update(curve(5), iid=99))
    with pytest.raises(TransitionError):
        write("not base64 at all!")


def test_minimum_update_interval_and_persistence():
    t = write(update(curve(5), update_interval=0), now=5).transition
    assert t.update_interval_ms == 1000
    restored = Transition.from_dict(t.as_dict())
    assert restored == t
    assert restored.mireds_at(60_005, 100, 140, 500) == 150
