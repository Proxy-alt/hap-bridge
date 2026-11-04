"""Rewriting go2rtc's WebRTC offer for a camera, and the camera's answer back."""

from hb.sdp import Sdp, answer_for_go2rtc, kind, mid, offer_for_camera

# Shaped like go2rtc's (pion's) WHEP offer: video first, no data channel.
OFFER = "\r\n".join([
    "v=0", "o=- 1 2 IN IP4 0.0.0.0", "s=-", "t=0 0",
    "a=group:BUNDLE 0 1", "a=extmap-allow-mixed", "a=msid-semantic: WMS",
    "m=video 9 UDP/TLS/RTP/SAVPF 96", "c=IN IP4 0.0.0.0",
    "a=setup:actpass", "a=mid:0", "a=ice-ufrag:uf", "a=ice-pwd:pw",
    "a=fingerprint:sha-256 AA:BB", "a=rtpmap:96 H264/90000", "a=recvonly",
    "a=candidate:1 1 udp 2130706431 192.168.1.5 50000 typ host", "a=end-of-candidates",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111", "c=IN IP4 0.0.0.0",
    "a=setup:actpass", "a=mid:1", "a=ice-ufrag:uf", "a=ice-pwd:pw",
    "a=fingerprint:sha-256 AA:BB", "a=rtpmap:111 opus/48000/2", "a=recvonly",
]) + "\r\n"


def _answer(mids_kinds, candidates=True):
    lines = ["v=0", "o=- 3 4 IN IP4 1.2.3.4", "s=-", "t=0 0",
             "a=group:BUNDLE " + " ".join(m for m, _ in mids_kinds)]
    for m, k in mids_kinds:
        lines += [f"m={k} 9 UDP/TLS/RTP/SAVPF 96", "c=IN IP4 1.2.3.4", f"a=mid:{m}",
                  "a=ice-ufrag:x", "a=ice-pwd:y", "a=setup:passive"]
        if candidates:
            lines.append("a=candidate:9 1 udp 1 1.2.3.4 443 typ host")
    return "\r\n".join(lines) + "\r\n"


def test_offer_is_audio_video_data_like_the_frontend():
    offer, rewrite = offer_for_camera(OFFER)
    sdp = Sdp.parse(offer)
    assert [kind(s) for s in sdp.sections] == ["audio", "video", "application"]
    assert [mid(s) for s in sdp.sections] == ["1", "0", "2"]
    assert "a=group:BUNDLE 1 0 2" in sdp.session
    data = sdp.sections[2]
    # Same transport as the media it's bundled with.
    for line in ("a=ice-ufrag:uf", "a=ice-pwd:pw", "a=fingerprint:sha-256 AA:BB",
                 "a=setup:actpass", "a=sctp-port:5000"):
        assert line in data
    assert rewrite.mids == ["0", "1"] and rewrite.added == "2"
    # The media sections themselves are untouched.
    assert "a=rtpmap:96 H264/90000" in sdp.sections[1]


def test_offer_with_a_data_channel_already_gets_none_added():
    with_data = OFFER + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:7\r\n"
    offer, rewrite = offer_for_camera(with_data)
    assert [kind(s) for s in Sdp.parse(offer).sections] == ["audio", "video", "application"]
    assert rewrite.added is None


def test_answer_goes_back_to_go2rtcs_order_without_the_data_channel():
    _, rewrite = offer_for_camera(OFFER)
    answer = _answer([("1", "audio"), ("0", "video"), ("2", "application")])
    back = Sdp.parse(answer_for_go2rtc(answer, rewrite, []))
    assert [mid(s) for s in back.sections] == ["0", "1"]
    assert [kind(s) for s in back.sections] == ["video", "audio"]
    assert "a=group:BUNDLE 0 1" in back.session


def test_trickled_candidates_are_added_to_the_answer():
    _, rewrite = offer_for_camera(OFFER)
    answer = _answer([("1", "audio"), ("0", "video"), ("2", "application")], candidates=False)
    text = answer_for_go2rtc(answer, rewrite, [
        ("candidate:5 1 udp 9 5.6.7.8 3478 typ srflx", "0"),
        ("candidate:6 1 udp 9 5.6.7.9 3478 typ srflx", "2"),  # the dropped data channel
        ("candidate:7 1 udp 9 5.6.7.9 3478 typ srflx", None),
        ("", None),  # end of candidates
    ])
    video, audio = Sdp.parse(text).sections
    assert "a=candidate:5 1 udp 9 5.6.7.8 3478 typ srflx" in video
    # Bundled: candidates for sections go2rtc doesn't know go on the first one.
    assert "a=candidate:6 1 udp 9 5.6.7.9 3478 typ srflx" in video
    assert "a=candidate:7 1 udp 9 5.6.7.9 3478 typ srflx" in video
    assert not any(line.startswith("a=candidate") for line in audio)
    assert text.endswith("\r\n") and "\r\n\r\n" not in text


def test_answer_with_unexpected_mids_keeps_the_cameras_order():
    _, rewrite = offer_for_camera(OFFER)
    answer = _answer([("a", "audio"), ("v", "video"), ("2", "application")])
    back = Sdp.parse(answer_for_go2rtc(answer, rewrite, []))
    assert [mid(s) for s in back.sections] == ["a", "v"]
