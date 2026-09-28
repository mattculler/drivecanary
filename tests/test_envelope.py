from __future__ import annotations

import pytest

from drivecanary.envelope import EnvelopeError, parse_envelope


def frame(name: str, rc: int, out: bytes, err: bytes = b"", extra: str = "") -> bytes:
    head = f"FRAME name={name} rc={rc} out={len(out)} err={len(err)}{(' ' + extra) if extra else ''}\n".encode()
    return head + out + err + b"\n"


HEADER = (
    b"DRIVECANARY-ENVELOPE 1\ngate_version=1\nhostname=atlas\ntz=America/New_York\n"
    b"collected_at=1700000000\nHEADER-END\n"
)


def test_round_trip_with_binary_and_newlines() -> None:
    body = HEADER
    body += frame("smartctl.version", 0, b"smartctl 7.4\n")
    body += frame("smartctl.dev:/dev/sda:sat", 216, b'{"a":\n1}\n', b"\x00\xff\n\nweird\n")
    body += b"PROBE-END version=1 frames=2\n"
    body += frame("attrlog:attrlog.X-Y.ata.csv", 0, b"line\n", extra="inode=5 size=5 offset=0")
    body += b"END frames=1\n"
    env = parse_envelope(body)
    assert env.version == 1 and env.header["hostname"] == "atlas" and env.complete
    assert env.probe_version == 1 and env.probe_frames == 2 and env.gate_frames == 1
    assert [f.name for f in env.frames] == [
        "smartctl.version",
        "smartctl.dev:/dev/sda:sat",
        "attrlog:attrlog.X-Y.ata.csv",
    ]
    f = env.frame("smartctl.dev:/dev/sda:sat")
    assert f is not None and f.rc == 216 and f.out == b'{"a":\n1}\n' and f.err == b"\x00\xff\n\nweird\n"
    a = env.prefixed("attrlog:")[0]
    assert a.attrs == {"inode": "5", "size": "5", "offset": "0"}


def test_truncated_is_not_complete_but_keeps_frames() -> None:
    body = HEADER + frame("smartctl.version", 0, b"x\n") + b"PROBE-END version=1 frames=1\n"
    env = parse_envelope(body)
    assert not env.complete and env.probe_ran and len(env.frames) == 1


def test_truncated_frame_is_an_error() -> None:
    body = HEADER + b"FRAME name=x rc=0 out=100 err=0\nshort"
    with pytest.raises(EnvelopeError, match="truncated"):
        parse_envelope(body)


def test_not_an_envelope() -> None:
    with pytest.raises(EnvelopeError, match="forced command"):
        parse_envelope(b"uid=1000(ops) gid=1000\n")
    with pytest.raises(EnvelopeError):
        parse_envelope(HEADER + b"garbage line\n")
