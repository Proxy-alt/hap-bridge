"""Rewriting WebRTC offers and answers between go2rtc and a camera.

go2rtc's WHEP client offers video then audio and no data channel. Cameras
that only stream over WebRTC were written for Home Assistant's frontend,
which offers audio then video, plus a data channel when the camera's WebRTC
settings ask for one (Nest's do, and refuse offers without it). So the
offer is made to look like the frontend's on the way to the camera, and the
answer is put back the way go2rtc offered it, without any data channel, on
the way back. Everything is bundled on one transport, so dropping the data
channel's section changes nothing else.

Sections are matched to the offer by position, not by their mids: an answer
always lists them in the offer's order, but Google's servers renumber the
mids (0, 1, 2...) instead of echoing the offer's. Matching by mid then gave
go2rtc the audio section as its video, and no media ever arrived.

The camera is offered a single H.264 payload type. go2rtc offers several but
passes on only the first, and a camera given the choice may send another
(Google's send High profile), so no video reaches ffmpeg. With one payload
type the camera has no choice: the profile the accessory asks for (see
AccessorySpec.camera_video_profile), else the first go2rtc lists.

Answers may also arrive without their ICE candidates, which the camera
sends separately ("trickled"); go2rtc's WHEP client needs them in the
answer, so they are added to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Copied from a media section to make the data channel's.
_TRANSPORT = ("a=ice-ufrag:", "a=ice-pwd:", "a=ice-options:", "a=fingerprint:",
              "a=setup:", "a=candidate:", "a=end-of-candidates")
_ORDER = {"audio": 0, "video": 1, "application": 2}
# H.264 profiles by name, as the first byte of profile-level-id (hex).
VIDEO_PROFILES = {"baseline": "42", "main": "4d", "high": "64"}


@dataclass
class Sdp:
    session: list[str]
    sections: list[list[str]]

    @classmethod
    def parse(cls, text: str) -> Sdp:
        session: list[str] = []
        sections: list[list[str]] = []
        for line in text.replace("\r\n", "\n").split("\n"):
            if not line:
                continue
            if line.startswith("m="):
                sections.append([line])
            elif sections:
                sections[-1].append(line)
            else:
                session.append(line)
        return cls(session, sections)

    def __str__(self) -> str:
        lines = [*self.session, *(line for s in self.sections for line in s)]
        return "\r\n".join(lines) + "\r\n"

    def set_bundle(self, mids: list[str]) -> None:
        """Bundle exactly these mids, if the SDP bundles at all."""
        for i, line in enumerate(self.session):
            if line.startswith("a=group:BUNDLE"):
                self.session[i] = " ".join(["a=group:BUNDLE", *mids])


def kind(section: list[str]) -> str:
    return section[0][2:].split(" ", 1)[0]


def mid(section: list[str]) -> str | None:
    return next((line[6:] for line in section if line.startswith("a=mid:")), None)


@dataclass
class Rewrite:
    """How an offer was changed, to change the answer back."""

    mids: list[str]  # the original offer's, in its order
    added: str | None  # the data channel's mid, if one was added
    sent: list[str] = field(default_factory=list)  # mids in the camera's offer, in order


def offer_for_camera(
    offer: str, *, data_channel: bool = False, video_profile: str | None = None
) -> tuple[str, Rewrite]:
    """`data_channel`: add one, as the frontend does when the camera's WebRTC
    settings ask for it. `video_profile`: see VIDEO_PROFILES."""
    sdp = Sdp.parse(offer)
    mids = [m or "" for m in map(mid, sdp.sections)]
    sections = sorted(sdp.sections, key=lambda s: _ORDER.get(kind(s), len(_ORDER)))
    sections = [_one_h264(s, video_profile) if kind(s) == "video" else s for s in sections]
    added = None
    if (data_channel and sections
            and not any(kind(s) == "application" for s in sections)):
        added = _free_mid(mids)
        first = sections[0]
        transport = [line for line in first if line.startswith(_TRANSPORT)]
        connection = next((line for line in first if line.startswith("c=")), "c=IN IP4 0.0.0.0")
        sections.append([
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
            connection,
            *transport,
            f"a=mid:{added}",
            "a=sctp-port:5000",
        ])
    sdp.sections = sections
    sent = [m or "" for m in map(mid, sections)]
    sdp.set_bundle([m for m in sent if m])
    return str(sdp), Rewrite(mids, added, sent)


def answer_for_go2rtc(answer: str, rewrite: Rewrite, candidates: list[tuple[str, str | None]]) -> str:
    """`candidates` are trickled (candidate line, mid) pairs to add."""
    sdp = Sdp.parse(answer)
    if len(sdp.sections) != len(rewrite.sent):
        # Not an answer to what we sent; let go2rtc make what it can of it.
        _add_candidates(sdp.sections, candidates, {})
        return str(sdp)
    # The answer's mid for each section -> the mid we offered in that position.
    ours = {}
    for section, offered in zip(sdp.sections, rewrite.sent, strict=True):
        if (theirs := mid(section)) is not None:
            ours[theirs] = offered
        _set_mid(section, offered)
    # go2rtc's WebRTC library (pion) takes the remote candidates only from
    # the first section in the BUNDLE group. Keep the camera's group order,
    # which starts with the section it put its candidates in, and gather all
    # candidates there.
    group = next((line.split()[1:] for line in sdp.session
                  if line.startswith("a=group:BUNDLE")), [])
    order = [ours.get(m, m) for m in group]
    found = [line for s in sdp.sections for line in s if line.startswith("a=candidate:")]
    by_mid = {mid(s): s for s in sdp.sections}
    sections = [by_mid[m] for m in rewrite.mids if m in by_mid]
    kept = [m for m in order if m in rewrite.mids and m in by_mid]
    kept += [m for m in rewrite.mids if m in by_mid and m not in kept]
    if kept:
        master = by_mid[kept[0]]
        master.extend(line for line in dict.fromkeys(found) if line not in master)
    _add_candidates([by_mid[m] for m in kept] or sections, candidates, ours)
    sdp.sections = sections
    sdp.set_bundle(kept)
    return str(sdp)


def _set_mid(section: list[str], value: str) -> None:
    for i, line in enumerate(section):
        if line.startswith("a=mid:"):
            section[i] = f"a=mid:{value}"
            return
    section.append(f"a=mid:{value}")


def _add_candidates(
    sections: list[list[str]], candidates: list[tuple[str, str | None]], ours: dict[str, str]
) -> None:
    if not sections:
        return
    for line, where in candidates:
        line = line if line.startswith("a=") else f"a={line}"
        if not line.startswith("a=candidate:"):
            continue
        where = ours.get(where, where) if where is not None else None
        target = next((s for s in sections if where is not None and mid(s) == where), sections[0])
        if line not in target:
            target.append(line)


def _one_h264(section: list[str], profile: str | None) -> list[str]:
    """The video section with only one H.264 payload type: the first of the
    profile asked for, else the first. Unchanged if it has no H.264."""
    h264 = [line.split(":", 1)[1].split(" ", 1)[0] for line in section
            if line.startswith("a=rtpmap:") and " H264/" in line]
    if not h264:
        return section
    profiles = {
        line.split(":", 1)[1].split(" ", 1)[0]: line.partition("profile-level-id=")[2][:2].lower()
        for line in section if line.startswith("a=fmtp:")
    }
    wanted = VIDEO_PROFILES.get(profile or "")
    keep = next((pt for pt in h264 if wanted and profiles.get(pt) == wanted), h264[0])
    head = section[0].split(" ")
    out = [" ".join([*head[:3], keep])]
    for line in section[1:]:
        if line.startswith(("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:")):
            if line.split(":", 1)[1].split(" ", 1)[0] != keep:
                continue
        out.append(line)
    return out


def _free_mid(mids: list[str]) -> str:
    numbers = [int(m) for m in mids if m.isdigit()]
    candidate = str(max(numbers) + 1) if numbers else str(len(mids))
    while candidate in mids:
        candidate += "x"
    return candidate
