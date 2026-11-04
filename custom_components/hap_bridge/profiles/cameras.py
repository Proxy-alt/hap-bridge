"""Cameras as standalone accessories, optionally video doorbells."""

from __future__ import annotations

from ..api import AccessorySpec, HapContext
from .fallback import bind_doorbell


def describe_camera(
    ctx: HapContext,
    camera: str,
    *,
    doorbell: str | None = None,
    motion: str | None = None,
    name: str | None = None,
    key: str | None = None,
    audio: bool = False,
) -> AccessorySpec:
    """A camera entity streamed to the Home app, as a video doorbell if `doorbell`
    (an event entity) is given, with an optional motion binary sensor. `audio`
    sends the stream's sound too; only set it if the stream has an audio track."""
    acc = ctx.accessory(
        name, category="VIDEO_DOOR_BELL" if doorbell else "CAMERA", key=key, camera=camera
    )
    acc.camera_audio = audio
    acc.claim(camera)
    acc.available = lambda: ctx.available(camera)
    if doorbell is not None:
        # What HomeKit expects of a video doorbell: Doorbell, a programmable
        # switch for automations, and a speaker.
        ring = acc.service("Doorbell", primary=True)
        bind_doorbell(ctx, ring, ctx.entry(doorbell))
        switch = acc.service("StatelessProgrammableSwitch")
        bind_doorbell(ctx, switch, ctx.entry(doorbell))
        switch.chars["ProgrammableSwitchEvent"].valid_values = {"SinglePress": 0}
        acc.service("Speaker").char("Mute", watch=[doorbell], get=lambda: False)
    if motion is not None:
        acc.service("MotionSensor").char(
            "MotionDetected", watch=[motion], get=lambda: ctx.is_on(motion)
        )
    return acc
