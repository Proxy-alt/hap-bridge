"""Cameras as standalone accessories, optionally video doorbells."""

from __future__ import annotations

from typing import Any

from ..api import AccessorySpec, HapContext, Recording
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
    recording: dict[str, Any] | bool | None = None,
) -> AccessorySpec:
    """A camera entity streamed to the Home app, as a video doorbell if `doorbell`
    (an event entity) is given, with an optional motion binary sensor. `audio`
    sends the stream's sound too; only set it if the stream has an audio track.

    `recording` says what starts a HomeKit Secure Video recording:
    `{"motion": [entity ids], "doorbell": event entity id}`. By default it's
    `motion` and `doorbell`; `False` turns recording off. The Motion Sensor
    shown with the camera is on while any of the motion entities is.
    """
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
    triggers = recording_triggers(recording, motion=motion, doorbell=doorbell)
    motions = list(triggers.motion) if triggers else [motion] if motion else []
    if motions:
        acc.service("MotionSensor").char(
            "MotionDetected", watch=motions, get=lambda: any(ctx.is_on(m) for m in motions)
        )
    acc.recording = triggers
    return acc


def recording_triggers(
    recording: dict[str, Any] | bool | None, *, motion: str | None, doorbell: str | None
) -> Recording | None:
    if recording is False:
        return None
    if recording is None or recording is True:
        recording = {"motion": [motion] if motion else [], "doorbell": doorbell}
    if not isinstance(recording, dict) or set(recording) - {"motion", "doorbell"}:
        raise ValueError('recording must be a dict with "motion" and/or "doorbell"')
    moving = recording.get("motion") or ()
    if isinstance(moving, str):
        moving = (moving,)
    bell = recording.get("doorbell")
    if bell is not None and bell != doorbell:
        raise ValueError(
            f"recording doorbell {bell} must be the camera's doorbell (doorbell=...)"
        )
    if not moving and bell is None:
        return None  # nothing could start a recording
    return Recording(motion=tuple(moving), doorbell=bell)
