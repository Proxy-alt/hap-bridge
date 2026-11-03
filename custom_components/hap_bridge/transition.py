"""Adaptive Lighting's wire format and curve math, without Home Assistant.

Apple hasn't published this part of HAP. The type numbers and semantics
follow HAP-NodeJS's AdaptiveLightingController, which reverse-engineered
them; names marked "unknown" there are unknown here too.

The Home app writes a transition curve to the light's TransitionControl
characteristic: color temperatures (in mireds) at time offsets over about
24 hours, each with a brightness adjustment factor. The accessory then
works out the color temperature itself every `update_interval`:

    mireds = curve(t) + adjustment_factor(t) * clamp(brightness, range)

`runtime` (adaptive.py) drives a light with it; this module only parses,
answers and evaluates.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
import struct
from typing import Any

# SupportedTransitionConfiguration
SUPPORTED_TRANSITION_CONFIGURATION = 0x01
SUPPORTED_IID = 0x01
SUPPORTED_TYPE = 0x02
TRANSITION_TYPE_BRIGHTNESS = 0x01
TRANSITION_TYPE_COLOR_TEMPERATURE = 0x02

# TransitionControl writes
CONTROL_READ = 0x01
CONTROL_UPDATE = 0x02
READ_IID = 0x01
UPDATE_CONFIGURATION = 0x01

# A value transition configuration
CONFIG_IID = 0x01
CONFIG_PARAMETERS = 0x02
CONFIG_UNKNOWN_3 = 0x03  # always 1 when enabling; absent/0 means "disable"
CONFIG_CURVE = 0x05
CONFIG_UPDATE_INTERVAL = 0x06  # uint16 ms
CONFIG_NOTIFY_INTERVAL = 0x08  # uint32 ms

PARAM_TRANSITION_ID = 0x01  # 16 bytes
PARAM_START_TIME = 0x02  # 8 bytes, ms since 2001-01-01
PARAM_UNKNOWN_3 = 0x03  # 8 bytes, optional

CURVE_ENTRY = 0x01
CURVE_ADJUSTMENT_IID = 0x02
CURVE_ADJUSTMENT_RANGE = 0x03
RANGE_MIN = 0x01
RANGE_MAX = 0x02

ENTRY_ADJUSTMENT_FACTOR = 0x01  # float32
ENTRY_VALUE = 0x02  # float32, mireds
ENTRY_OFFSET = 0x03  # ms after the previous entry (after its hold time)
ENTRY_DURATION = 0x04  # ms the *previous* entry's value holds

# Write response
RESPONSE_STATUS = 0x01
STATUS_IID = 0x01
STATUS_PARAMETERS = 0x02
STATUS_TIME_SINCE_START = 0x03

DEFAULT_UPDATE_INTERVAL_MS = 60_000
MIN_UPDATE_INTERVAL_MS = 1_000


class TransitionError(ValueError):
    """A TransitionControl write this light can't accept."""


# -- TLV8 -----------------------------------------------------------------------


def tlv(*pairs: Any) -> bytes:
    """Encode type/value pairs. A list value becomes a 0x00-delimited TLV list."""
    out = bytearray()
    for tag, value in zip(pairs[::2], pairs[1::2], strict=True):
        if isinstance(value, list):
            for i, item in enumerate(value):
                if i:
                    out += b"\x00\x00"
                out += tlv(tag, item)
            continue
        if isinstance(value, int):
            value = bytes([value])
        # Values over 255 bytes are split into consecutive same-type items.
        chunks = [value[i : i + 255] for i in range(0, len(value), 255)] or [b""]
        for chunk in chunks:
            out += bytes([tag, len(chunk)]) + chunk
    return bytes(out)


def untlv(data: bytes) -> dict[int, bytes | list[bytes]]:
    """Decode TLV8, joining split values and collecting delimited lists."""
    out: dict[int, bytes | list[bytes]] = {}
    i, last_tag, last_len, after_delimiter = 0, -1, -1, False
    while i < len(data):
        if i + 2 > len(data):
            raise TransitionError("truncated TLV header")
        tag, length = data[i], data[i + 1]
        value = data[i + 2 : i + 2 + length]
        if len(value) != length:
            raise TransitionError("truncated TLV value")
        i += 2 + length
        if tag == 0 and length == 0:
            after_delimiter = True
            continue
        if tag not in out:
            out[tag] = value
        elif after_delimiter and tag == last_tag:
            existing = out[tag]
            out[tag] = [*existing, value] if isinstance(existing, list) else [existing, value]
        elif tag == last_tag and last_len == 255:
            existing = out[tag]
            if isinstance(existing, list):
                existing[-1] += value
            else:
                out[tag] = existing + value
        else:
            raise TransitionError(f"duplicate TLV type {tag}")
        last_tag, last_len, after_delimiter = tag, length, False
    return out


def _one(items: dict[int, Any], tag: int) -> bytes:
    value = items.get(tag)
    if value is None:
        raise TransitionError(f"missing TLV type {tag}")
    return value[0] if isinstance(value, list) else value


def _many(items: dict[int, Any], tag: int) -> list[bytes]:
    value = items.get(tag)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def read_uint(data: bytes) -> int:
    if len(data) not in (1, 2, 4, 8):
        raise TransitionError(f"bad unsigned integer length {len(data)}")
    return int.from_bytes(data, "little")


def write_uint(value: int) -> bytes:
    """Smallest of uint8/16/32/64, little endian (what HAP expects for IIDs etc.)."""
    for size in (1, 2, 4, 8):
        if value < 1 << (8 * size):
            return value.to_bytes(size, "little")
    raise ValueError(value)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# -- the transition --------------------------------------------------------------


@dataclass
class CurveEntry:
    mireds: float
    adjustment_factor: float
    # ms from the end of the previous entry's hold to this entry
    transition_ms: int
    # ms this entry's value holds before moving on to the next
    hold_ms: int = 0


@dataclass
class Transition:
    """One active Adaptive Lighting schedule, as the Home app sent it."""

    iid: int  # the ColorTemperature characteristic
    brightness_iid: int
    transition_id: bytes
    start_time: bytes  # kept verbatim to echo back
    unknown_3: bytes | None
    curve: list[CurveEntry]
    min_brightness: int
    max_brightness: int
    update_interval_ms: int
    notify_interval_ms: int
    # Wall-clock ms when the curve was received. The curve's own start time is
    # the controller's clock; like HAP-NodeJS, time is measured from receipt.
    received_ms: int = 0
    _cursor: tuple[int, int] = field(default=(0, 0), repr=False, compare=False)

    def mireds_at(self, now_ms: int, brightness: float, lo: int, hi: int) -> int | None:
        """The color temperature for `now_ms` and a brightness in percent.

        Clamped to the light's range [lo, hi]. None once the curve has ended,
        which means Adaptive Lighting should switch itself off.
        """
        point = self._point(now_ms - self.received_ms)
        if point is None:
            return None
        lower, upper, into = point
        if lower.hold_ms and into <= lower.hold_ms:
            mireds, factor = lower.mireds, lower.adjustment_factor
        else:
            share = (into - lower.hold_ms) / upper.transition_ms if upper.transition_ms else 1
            mireds = lower.mireds + (upper.mireds - lower.mireds) * share
            factor = lower.adjustment_factor + (
                upper.adjustment_factor - lower.adjustment_factor
            ) * share
        multiplier = min(max(brightness, self.min_brightness), self.max_brightness)
        return min(max(round(mireds + factor * multiplier), lo), hi)

    def _point(self, offset: int) -> tuple[CurveEntry, CurveEntry, int] | None:
        """The curve segment containing `offset`, and how far into it we are."""
        if offset < 0:
            offset = 0
        # Resume from the last segment found; time only moves forward.
        for index, start in (self._cursor, (0, 0)):
            for i in range(index, len(self.curve) - 1):
                lower, upper = self.curve[i], self.curve[i + 1]
                lower_at = start + lower.transition_ms
                if offset < lower_at:
                    break  # before this segment: the cursor was ahead
                if offset <= lower_at + lower.hold_ms + upper.transition_ms:
                    self._cursor = (i, start)
                    return lower, upper, offset - lower_at
                start = lower_at + lower.hold_ms
            else:
                return None  # past the last entry
        return None

    def ended(self, now_ms: int) -> bool:
        return self._point(now_ms - self.received_ms) is None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_cursor")
        for key in ("transition_id", "start_time", "unknown_3"):
            data[key] = data[key].hex() if data[key] is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transition:
        data = dict(data)
        for key in ("transition_id", "start_time", "unknown_3"):
            data[key] = bytes.fromhex(data[key]) if data[key] is not None else None
        data["curve"] = [CurveEntry(**entry) for entry in data["curve"]]
        return cls(**data)


# -- characteristic values -------------------------------------------------------


def supported_configuration(brightness_iid: int, temperature_iid: int) -> str:
    """SupportedTransitionConfiguration: brightness and color temperature can transition."""
    return b64(
        tlv(
            SUPPORTED_TRANSITION_CONFIGURATION,
            [
                tlv(SUPPORTED_IID, write_uint(brightness_iid), SUPPORTED_TYPE,
                    TRANSITION_TYPE_BRIGHTNESS),
                tlv(SUPPORTED_IID, write_uint(temperature_iid), SUPPORTED_TYPE,
                    TRANSITION_TYPE_COLOR_TEMPERATURE),
            ],
        )
    )


def _parameters(t: Transition) -> bytes:
    params = tlv(PARAM_TRANSITION_ID, t.transition_id, PARAM_START_TIME, t.start_time)
    if t.unknown_3 is not None:
        params += tlv(PARAM_UNKNOWN_3, t.unknown_3)
    return params


def status(t: Transition | None, now_ms: int | None = None) -> bytes:
    """The transition's status, as read from TransitionControl (empty if none)."""
    if t is None:
        return b""
    since = 0 if now_ms is None else max(now_ms - t.received_ms, 0)
    return tlv(
        RESPONSE_STATUS,
        tlv(
            STATUS_IID, write_uint(t.iid),
            STATUS_PARAMETERS, _parameters(t),
            STATUS_TIME_SINCE_START, write_uint(since),
        ),
    )


@dataclass
class ControlResult:
    """What a TransitionControl write asked for, and the write response."""

    response: str
    # The new transition (enable/renew), or None.
    transition: Transition | None = None
    disable: bool = False


def handle_control_write(
    value: str,
    *,
    active: Transition | None,
    temperature_iid: int,
    brightness_iid: int,
    now_ms: int,
) -> ControlResult:
    """Interpret a TransitionControl write (read the curve, set it, or clear it)."""
    try:
        request = untlv(base64.b64decode(value))
    except (ValueError, TypeError) as err:
        raise TransitionError(f"not base64 TLV: {err}") from err

    response = b""
    result = ControlResult(response="")
    if CONTROL_READ in request:
        read = untlv(_one(request, CONTROL_READ))
        iid = read_uint(_one(read, READ_IID))
        if active is not None:
            if iid != active.iid:
                raise TransitionError(f"read of IID {iid}, active transition is on {active.iid}")
            response += tlv(CONTROL_READ, _describe(active))

    if CONTROL_UPDATE in request:
        update = untlv(_one(request, CONTROL_UPDATE))
        config = untlv(_one(update, UPDATE_CONFIGURATION))
        iid = read_uint(_one(config, CONFIG_IID))
        if iid != temperature_iid:
            raise TransitionError(f"transition for IID {iid}, expected {temperature_iid}")
        enabled = config.get(CONFIG_UNKNOWN_3)
        if not enabled or not _one(config, CONFIG_UNKNOWN_3)[0]:
            # Only the IID: the Home app is switching Adaptive Lighting off.
            result.disable = True
            response += tlv(CONTROL_UPDATE, b"")
        else:
            transition = _parse_transition(config, iid, brightness_iid, now_ms)
            result.transition = transition
            response += tlv(CONTROL_UPDATE, status(transition))

    result.response = b64(response)
    return result


def _parse_transition(
    config: dict[int, Any], iid: int, brightness_iid: int, now_ms: int
) -> Transition:
    params = untlv(_one(config, CONFIG_PARAMETERS))
    curve_tlv = untlv(_one(config, CONFIG_CURVE))

    curve: list[CurveEntry] = []
    for raw in _many(curve_tlv, CURVE_ENTRY):
        entry = untlv(raw)
        if curve and ENTRY_DURATION in entry:
            # On the wire, an entry's duration is how long the previous one holds.
            curve[-1].hold_ms = read_uint(_one(entry, ENTRY_DURATION))
        curve.append(
            CurveEntry(
                mireds=struct.unpack("<f", _one(entry, ENTRY_VALUE))[0],
                adjustment_factor=struct.unpack("<f", _one(entry, ENTRY_ADJUSTMENT_FACTOR))[0],
                transition_ms=read_uint(_one(entry, ENTRY_OFFSET)),
            )
        )
    if len(curve) < 2:
        raise TransitionError("transition curve needs at least two entries")

    adjustment_iid = read_uint(_one(curve_tlv, CURVE_ADJUSTMENT_IID))
    if adjustment_iid != brightness_iid:
        raise TransitionError(f"adjusts IID {adjustment_iid}, brightness is {brightness_iid}")
    adj_range = untlv(_one(curve_tlv, CURVE_ADJUSTMENT_RANGE))
    update = config.get(CONFIG_UPDATE_INTERVAL)
    notify = config.get(CONFIG_NOTIFY_INTERVAL)
    return Transition(
        iid=iid,
        brightness_iid=adjustment_iid,
        transition_id=_one(params, PARAM_TRANSITION_ID),
        start_time=_one(params, PARAM_START_TIME),
        unknown_3=params.get(PARAM_UNKNOWN_3),
        curve=curve,
        min_brightness=read_uint(_one(adj_range, RANGE_MIN)),
        max_brightness=read_uint(_one(adj_range, RANGE_MAX)),
        update_interval_ms=max(
            read_uint(update) if update else DEFAULT_UPDATE_INTERVAL_MS, MIN_UPDATE_INTERVAL_MS
        ),
        notify_interval_ms=read_uint(notify) if notify else 0,
        received_ms=now_ms,
    )


def _describe(t: Transition) -> bytes:
    """The whole active configuration, for a CONTROL_READ."""
    entries = []
    previous_hold = 0
    for entry in t.curve:
        entries.append(
            tlv(
                ENTRY_ADJUSTMENT_FACTOR, struct.pack("<f", entry.adjustment_factor),
                ENTRY_VALUE, struct.pack("<f", entry.mireds),
                ENTRY_OFFSET, write_uint(entry.transition_ms),
                ENTRY_DURATION, write_uint(previous_hold),
            )
        )
        previous_hold = entry.hold_ms
    return tlv(
        CONFIG_IID, write_uint(t.iid),
        CONFIG_PARAMETERS, _parameters(t),
        CONFIG_UNKNOWN_3, 1,
        CONFIG_CURVE, tlv(
            CURVE_ENTRY, entries,
            CURVE_ADJUSTMENT_IID, write_uint(t.brightness_iid),
            CURVE_ADJUSTMENT_RANGE, tlv(
                RANGE_MIN, t.min_brightness.to_bytes(4, "little"),
                RANGE_MAX, t.max_brightness.to_bytes(4, "little"),
            ),
        ),
        CONFIG_UPDATE_INTERVAL, write_uint(t.update_interval_ms),
        CONFIG_NOTIFY_INTERVAL, write_uint(t.notify_interval_ms),
    )
