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
    offer, rewrite = offer_for_camera(OFFER, data_channel=True)
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


def test_no_data_channel_unless_the_camera_asks():
    offer, rewrite = offer_for_camera(OFFER)
    assert [kind(s) for s in Sdp.parse(offer).sections] == ["audio", "video"]
    assert rewrite.added is None


def test_offer_with_a_data_channel_already_gets_none_added():
    with_data = OFFER + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:7\r\n"
    offer, rewrite = offer_for_camera(with_data, data_channel=True)
    assert [kind(s) for s in Sdp.parse(offer).sections] == ["audio", "video", "application"]
    assert rewrite.added is None


def test_answer_goes_back_to_go2rtcs_order_without_the_data_channel():
    _, rewrite = offer_for_camera(OFFER, data_channel=True)
    answer = _answer([("1", "audio"), ("0", "video"), ("2", "application")])
    back = Sdp.parse(answer_for_go2rtc(answer, rewrite, []))
    assert [mid(s) for s in back.sections] == ["0", "1"]
    assert [kind(s) for s in back.sections] == ["video", "audio"]
    # The camera's bundle order: go2rtc reads candidates from its first section.
    assert "a=group:BUNDLE 1 0" in back.session


def test_trickled_candidates_are_added_to_the_answer():
    _, rewrite = offer_for_camera(OFFER, data_channel=True)
    answer = _answer([("1", "audio"), ("0", "video"), ("2", "application")], candidates=False)
    text = answer_for_go2rtc(answer, rewrite, [
        ("candidate:5 1 udp 9 5.6.7.8 3478 typ srflx", "0"),
        ("candidate:6 1 udp 9 5.6.7.9 3478 typ srflx", "2"),  # the dropped data channel
        ("candidate:7 1 udp 9 5.6.7.9 3478 typ srflx", None),
        ("", None),  # end of candidates
    ])
    video, audio = Sdp.parse(text).sections
    assert "a=candidate:5 1 udp 9 5.6.7.8 3478 typ srflx" in video
    # Bundled: candidates for sections go2rtc doesn't know go on the bundle's
    # first section, the audio here, where go2rtc reads them.
    assert "a=candidate:6 1 udp 9 5.6.7.9 3478 typ srflx" in audio
    assert "a=candidate:7 1 udp 9 5.6.7.9 3478 typ srflx" in audio
    assert text.endswith("\r\n") and "\r\n\r\n" not in text


def test_answer_that_renumbers_mids_is_matched_by_position():
    """Google answers with mids 0, 1, 2 whatever we offered: its "0" is the
    audio we offered as "1", which must not reach go2rtc as its video "0"."""
    _, rewrite = offer_for_camera(OFFER, data_channel=True)
    answer = _answer([("0", "audio"), ("1", "video"), ("2", "application")], candidates=False)
    text = answer_for_go2rtc(answer, rewrite, [
        ("candidate:5 1 udp 9 5.6.7.8 3478 typ srflx", "1"),  # the camera's "1": video
    ])
    back = Sdp.parse(text)
    assert [(mid(s), kind(s)) for s in back.sections] == [("0", "video"), ("1", "audio")]
    assert "a=group:BUNDLE 1 0" in back.session
    assert "a=candidate:5 1 udp 9 5.6.7.8 3478 typ srflx" in back.sections[0]


def test_candidates_end_up_where_go2rtc_reads_them():
    """Like Google: candidates only in the answer's first (audio) section."""
    _, rewrite = offer_for_camera(OFFER, data_channel=True)
    answer = "\r\n".join([
        "v=0", "o=- 0 2 IN IP4 127.0.0.1", "s=-", "t=0 0", "a=group:BUNDLE 0 1 2", "a=ice-lite",
        "m=audio 19305 UDP/TLS/RTP/SAVPF 101", "a=candidate: 1 udp 2113932031 74.125.247.225 19305 typ host generation 0",
        "a=ice-ufrag:u", "a=mid:0", "a=sendonly", "a=ssrc:6666 cname:6666",
        "m=video 9 UDP/TLS/RTP/SAVPF 96", "a=ice-ufrag:u", "a=mid:1", "a=sendonly",
        "a=ssrc:7777 cname:7777",
        "m=application 9 DTLS/SCTP 5000", "a=ice-ufrag:u", "a=mid:2",
    ]) + "\r\n"
    back = Sdp.parse(answer_for_go2rtc(answer, rewrite, []))
    video, audio = back.sections
    assert kind(video) == "video" and "a=ssrc:7777 cname:7777" in video
    assert kind(audio) == "audio" and "a=ssrc:6666 cname:6666" in audio
    first = back.session[-2].split()[1]  # the bundle's first mid
    assert "a=group:BUNDLE 1 0" in back.session and first == "1" == mid(audio)
    assert any(line.startswith("a=candidate:") for line in audio)


def test_answer_with_a_different_number_of_sections_is_passed_through():
    _, rewrite = offer_for_camera(OFFER, data_channel=True)
    answer = _answer([("a", "audio"), ("v", "video")])
    back = Sdp.parse(answer_for_go2rtc(answer, rewrite, []))
    assert [mid(s) for s in back.sections] == ["a", "v"]


def test_camera_is_offered_one_h264_payload_of_the_profile_asked_for():
    """go2rtc passes on only its first video codec; Google sent High on another."""
    offer = "\r\n".join([
        "v=0", "s=-", "a=group:BUNDLE 0 1",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98 100", "a=mid:0",
        "a=rtpmap:96 H264/90000", "a=fmtp:96 profile-level-id=42001f", "a=rtcp-fb:96 nack",
        "a=rtpmap:97 H264/90000", "a=fmtp:97 profile-level-id=42e01f",
        "a=rtpmap:98 H264/90000",
        "a=fmtp:98 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032",
        "a=rtcp-fb:98 nack", "a=rtpmap:100 H265/90000", "a=rtcp-fb:100 nack",
        "a=extmap:1 urn:ietf:params:rtp-hdrext:sdes:mid", "a=recvonly",
        "m=audio 9 UDP/TLS/RTP/SAVPF 101 0 8", "a=mid:1", "a=rtpmap:101 opus/48000/2",
        "a=rtpmap:0 PCMU/8000",
    ]) + "\r\n"
    sent = Sdp.parse(offer_for_camera(offer, data_channel=True, video_profile="high")[0])
    audio, video, _data = sent.sections
    assert video[0] == "m=video 9 UDP/TLS/RTP/SAVPF 98"
    assert [line for line in video if line.startswith(("a=rtpmap", "a=fmtp", "a=rtcp-fb"))] == [
        "a=rtpmap:98 H264/90000",
        "a=fmtp:98 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032",
        "a=rtcp-fb:98 nack",
    ]
    assert "a=extmap:1 urn:ietf:params:rtp-hdrext:sdes:mid" in video
    # Audio is left alone.
    assert audio[0] == "m=audio 9 UDP/TLS/RTP/SAVPF 101 0 8"


def test_by_default_the_first_h264_is_kept():
    offer = "\r\n".join([
        "v=0", "s=-", "m=video 9 UDP/TLS/RTP/SAVPF 96 98", "a=mid:0",
        "a=rtpmap:96 H264/90000", "a=fmtp:96 profile-level-id=42001f",
        "a=rtpmap:98 H264/90000", "a=fmtp:98 profile-level-id=640032",
    ]) + "\r\n"
    video = Sdp.parse(offer_for_camera(offer)[0]).sections[0]
    assert video[0] == "m=video 9 UDP/TLS/RTP/SAVPF 96"


def test_without_the_profile_asked_for_the_first_h264_is_kept():
    offer = "\r\n".join([
        "v=0", "s=-", "m=video 9 UDP/TLS/RTP/SAVPF 96 97", "a=mid:0",
        "a=rtpmap:96 H264/90000", "a=fmtp:96 profile-level-id=42001f",
        "a=rtpmap:97 H264/90000", "a=fmtp:97 profile-level-id=42e01f",
    ]) + "\r\n"
    video = Sdp.parse(offer_for_camera(offer, video_profile="high")[0]).sections[0]
    assert video[0] == "m=video 9 UDP/TLS/RTP/SAVPF 96"
    assert "a=rtpmap:97 H264/90000" not in video
