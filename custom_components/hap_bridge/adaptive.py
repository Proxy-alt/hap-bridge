"""Runs Adaptive Lighting for one HA light (protocol and math in transition.py).

HomeKit bulbs run the curve themselves; here the bridge does it for them:
every update interval it works out the color temperature for now and the
light's brightness, and sends it with light.turn_on. It switches itself off
(as a real bulb would) when someone picks a color by hand, in the Home app
or anywhere in Home Assistant.

HA can't change an off light's color temperature without turning it on, so
off lights are skipped and get the right temperature as they turn on: in the
same call when the Home app turns them on, right after otherwise.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import timedelta
import logging
import time
from typing import Any

from pyhap.characteristic import Characteristic
from pyhap.service import Service

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .transition import (
    Transition,
    TransitionError,
    b64,
    handle_control_write,
    status,
    supported_configuration,
)

_LOGGER = logging.getLogger(__name__)

# The characteristics this adds to a Lightbulb.
CHARACTERISTICS = ("SupportedTransitionConfiguration", "TransitionControl", "ActiveTransitionCount")
# Home app writes to these mean "I'm choosing the color myself".
MANUAL_COLOR = ("ColorTemperature", "Hue", "Saturation")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mireds(kelvin: float | None) -> int | None:
    return round(1_000_000 / kelvin) if kelvin else None


def _close(a: int, b: int) -> bool:
    # Lights round color temperature (often to 50-100 K), so allow some slack.
    return abs(a - b) <= max(5, b * 0.05)


class AdaptiveLighting:
    """The Adaptive Lighting controller for one Lightbulb service."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        entity_id: str,
        service: Service,
        iid_of: Callable[[Characteristic], int],
        save: Callable[[dict[str, Any] | None], None],
    ) -> None:
        self.hass = hass
        self.name = name
        self.entity_id = entity_id
        self._save = save
        self._temperature = service.get_characteristic("ColorTemperature")
        self._control = service.get_characteristic("TransitionControl")
        self._count = service.get_characteristic("ActiveTransitionCount")
        self._temperature_iid = iid_of(self._temperature)
        self._brightness_iid = iid_of(service.get_characteristic("Brightness"))
        service.get_characteristic("SupportedTransitionConfiguration").set_value(
            supported_configuration(self._brightness_iid, self._temperature_iid)
        )
        self._control.set_value("")
        self._count.set_value(0)
        self._control.setter_callback = self._on_control_write

        self.transition: Transition | None = None
        # Temperatures we sent recently; a state change to anything else is manual.
        self._sent: deque[int] = deque(maxlen=3)
        self._last_brightness = 100.0
        self._unsub: list[CALLBACK_TYPE] = []
        self._unsub_timer: CALLBACK_TYPE | None = None

    # -- lifecycle ----------------------------------------------------------------

    @callback
    def async_start(self, saved: dict[str, Any] | None) -> None:
        self._unsub.append(
            async_track_state_change_event(self.hass, [self.entity_id], self._async_light_changed)
        )
        if saved is None:
            return
        try:
            transition = Transition.from_dict(saved)
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("%s: discarding unreadable saved Adaptive Lighting state", self.name)
            self._save(None)
            return
        if (transition.iid, transition.brightness_iid) != (
            self._temperature_iid, self._brightness_iid
        ) or transition.ended(_now_ms()):
            self._save(None)
            return
        _LOGGER.debug("%s: resuming Adaptive Lighting", self.name)
        self._async_enable(transition, persist=False)

    @callback
    def async_stop(self) -> None:
        self._cancel_timer()
        while self._unsub:
            self._unsub.pop()()

    @property
    def active(self) -> bool:
        return self.transition is not None

    # -- the Home app --------------------------------------------------------------

    def _on_control_write(self, value: str) -> str:
        """TransitionControl write; returns the write response."""
        try:
            result = handle_control_write(
                value,
                active=self.transition,
                temperature_iid=self._temperature_iid,
                brightness_iid=self._brightness_iid,
                now_ms=_now_ms(),
            )
        except TransitionError as err:
            _LOGGER.warning("%s: rejected Adaptive Lighting request: %s", self.name, err)
            raise
        if result.disable:
            self.hass.loop.call_soon_threadsafe(self._async_disable, "turned off in the Home app")
        elif result.transition is not None:
            # Set now so a read in the same request sees it.
            self.transition = result.transition
            self.hass.loop.call_soon_threadsafe(self._async_enable, result.transition)
        return result.response

    def wrap_write(self, write: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        """Wrap the light's batched write to take Adaptive Lighting into account."""

        def _write(values: dict[str, Any]) -> Any:
            values = {k: v for k, v in values.items() if k not in CHARACTERISTICS}
            if not values:
                return None  # only TransitionControl, handled above
            return write(self._adjust(values))

        return _write

    def _adjust(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.transition is None:
            return values
        if any(name in values for name in MANUAL_COLOR):
            self._async_disable("color chosen in the Home app")
            return values
        if values.get("On") is False or values.get("Brightness") == 0:
            return values
        if "On" not in values and "Brightness" not in values:
            return values
        # Turning on or dimming: send the matching temperature in the same call.
        brightness = values.get("Brightness", self._last_brightness)
        mireds = self._target(brightness)
        if mireds is None:
            return values
        self._sent.append(mireds)
        return {**values, "ColorTemperature": mireds}

    # -- running the curve ----------------------------------------------------------

    @callback
    def _async_enable(self, transition: Transition, persist: bool = True) -> None:
        renewed = self.transition is not None and self._unsub_timer is not None
        self.transition = transition
        self._cancel_timer()
        self._count.set_value(1)
        self._control.set_value(b64(status(transition, _now_ms())), should_notify=False)
        if persist:
            self._save(transition.as_dict())
        self._unsub_timer = async_track_time_interval(
            self.hass,
            self._async_tick,
            timedelta(milliseconds=transition.update_interval_ms),
            name=f"hap_bridge adaptive lighting {self.entity_id}",
        )
        _LOGGER.debug("%s: Adaptive Lighting %s", self.name, "renewed" if renewed else "on")
        self._async_update()

    @callback
    def _async_disable(self, reason: str) -> None:
        if self.transition is None:
            return
        _LOGGER.debug("%s: Adaptive Lighting off (%s)", self.name, reason)
        self.transition = None
        self._cancel_timer()
        self._sent.clear()
        self._count.set_value(0)
        self._control.set_value("", should_notify=False)
        self._save(None)

    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _async_tick(self, _now: Any = None) -> None:
        self._async_update()

    def _target(self, brightness: float) -> int | None:
        """The curve's temperature now, or None (and switch off) once it has ended."""
        assert self.transition is not None
        props = self._temperature.properties
        mireds = self.transition.mireds_at(
            _now_ms(), brightness, int(props["minValue"]), int(props["maxValue"])
        )
        if mireds is None:
            self._async_disable("end of the schedule")
        return mireds

    @callback
    def _async_update(self) -> None:
        """Bring the light to the curve's current temperature, if it's on."""
        if self.transition is None:
            return
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state != "on":
            return
        if (brightness := state.attributes.get("brightness")) is not None:
            self._last_brightness = brightness / 255 * 100
        mireds = self._target(self._last_brightness)
        if mireds is None:
            return
        current = _mireds(state.attributes.get("color_temp_kelvin"))
        if current is not None and current == mireds and state.attributes.get("color_mode") == "color_temp":
            return
        self._sent.append(mireds)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "light", "turn_on",
                {"entity_id": self.entity_id, "color_temp_kelvin": round(1_000_000 / mireds)},
            ),
            f"hap_bridge adaptive lighting {self.entity_id}",
        )

    @callback
    def _async_light_changed(self, event: Event[EventStateChangedData]) -> None:
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or new.state != "on":
            return
        if (brightness := new.attributes.get("brightness")) is not None:
            self._last_brightness = brightness / 255 * 100
        if self.transition is None:
            return
        if old is None or old.state != "on":
            self._async_update()  # turned on elsewhere: catch up with the curve
            return
        if self._manual_change(old.attributes, new.attributes):
            self._async_disable("color changed outside the curve")
            return
        if old.attributes.get("brightness") != new.attributes.get("brightness"):
            self._async_update()

    def _manual_change(self, old: Any, new: Any) -> bool:
        mode = new.get("color_mode")
        if mode is not None and mode != "color_temp" and old.get("color_mode") == "color_temp":
            return True  # someone picked a color
        before, after = old.get("color_temp_kelvin"), new.get("color_temp_kelvin")
        if after is None or after == before:
            return False
        mireds = _mireds(after)
        assert mireds is not None
        return not any(_close(mireds, sent) for sent in self._sent)
