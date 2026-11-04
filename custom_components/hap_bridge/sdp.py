"""Rewriting WebRTC offers and answers between go2rtc and a camera.

go2rtc's WHEP client offers video then audio and no data channel. Cameras
that only stream over WebRTC were written for Home Assistant's frontend,
which offers audio, video and a data channel, in that order; Nest refuses
anything else. So the offer is reordered and given a data channel on the
way to the camera, and the answer is put back the way go2rtc offered it,
without the data channel, on the way back. Everything is bundled on one
transport, so dropping the data channel's section changes nothing else.

Answers may also arrive without their ICE candidates, which the camera
sends separately ("trickled"); go2rtc's WHEP client needs them in the
answer, so they are added to it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Copied from a media section to make the data channel's.
_TRANSPORT = ("a=ice-ufrag:", "a=ice-pwd:", "a=ice-options:", "a=fingerprint:",
              "a=setup:", "a=candidate:", "a=end-of-candidates")
_ORDER = {"audio": 0, "video": 1, "application": 2}


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


def offer_for_camera(offer: str) -> tuple[str, Rewrite]:
    sdp = Sdp.parse(offer)
    mids = [m or "" for m in map(mid, sdp.sections)]
    sections = sorted(sdp.sections, key=lambda s: _ORDER.get(kind(s), len(_ORDER)))
    added = None
    if sections and not any(kind(s) == "application" for s in sections):
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
    sdp.set_bundle([m for m in map(mid, sections) if m is not None])
    return str(sdp), Rewrite(mids, added)


def answer_for_go2rtc(answer: str, rewrite: Rewrite, candidates: list[tuple[str, str | None]]) -> str:
    """`candidates` are trickled (candidate line, mid) pairs to add."""
    sdp = Sdp.parse(answer)
    by_mid = {mid(s): s for s in sdp.sections}
    sections = [by_mid[m] for m in rewrite.mids if m in by_mid]
    if len(sections) != len(rewrite.mids):
        # Mids missing from the answer: keep the camera's order, minus ours.
        sections = [s for s in sdp.sections if rewrite.added is None or mid(s) != rewrite.added]
    if not sections:
        return str(sdp)
    for line, where in candidates:
        line = line if line.startswith("a=") else f"a={line}"
        if not line.startswith("a=candidate:"):
            continue
        target = next((s for s in sections if where is not None and mid(s) == where), sections[0])
        if line not in target:
            target.append(line)
    sdp.sections = sections
    sdp.set_bundle([m for m in map(mid, sections) if m is not None])
    return str(sdp)


def _free_mid(mids: list[str]) -> str:
    numbers = [int(m) for m in mids if m.isdigit()]
    candidate = str(max(numbers) + 1) if numbers else str(len(mids))
    while candidate in mids:
        candidate += "x"
    return candidate
