"""The byte stream the gate sends back, and nothing else parses it.

deploy/host/gate writes it, wrapping what deploy/host/probe wrote:

    DRIVECANARY-ENVELOPE 1
    key=value                       gate_version, hostname, machine_id, boot_id, collected_at (host epoch),
    ...                             tz, utc_offset, payload_id
    HEADER-END
    FRAME name=<n> rc=<r> out=<o> err=<e> [k=v ...]
    <o bytes of stdout><e bytes of stderr>
    PROBE-END version=<v> frames=<n>      after the probe's own frames
    FRAME name=attrlog:<file> ... inode=<i> size=<s> offset=<o>
    ...
    END frames=<n>                        the gate's own frame count

Frames are length-prefixed so any command's output, binary or not, travels untouched and the hub does all
the parsing. A stream without END is truncated: the frames before the break are kept for diagnosis and the
attempt fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VERSION = 1
MAGIC = b"DRIVECANARY-ENVELOPE"


class EnvelopeError(ValueError):
    pass


@dataclass
class Frame:
    name: str
    rc: int
    out: bytes
    err: bytes
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")

    @property
    def err_text(self) -> str:
        return self.err.decode("utf-8", "replace")


@dataclass
class Envelope:
    version: int
    header: dict[str, str]
    frames: list[Frame] = field(default_factory=list)
    probe_version: int | None = None
    probe_frames: int | None = None
    gate_frames: int | None = None
    complete: bool = False

    def frame(self, name: str) -> Frame | None:
        for f in self.frames:
            if f.name == name:
                return f
        return None

    def prefixed(self, prefix: str) -> list[Frame]:
        return [f for f in self.frames if f.name.startswith(prefix)]

    @property
    def probe_ran(self) -> bool:
        return self.probe_version is not None


def _kv(tokens: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in tokens:
        k, sep, v = tok.partition("=")
        if sep:
            out[k] = v
    return out


def parse_envelope(data: bytes) -> Envelope:
    pos = 0
    n = len(data)

    def line() -> str | None:
        nonlocal pos
        if pos >= n:
            return None
        end = data.find(b"\n", pos)
        if end < 0:
            end = n
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    first = line()
    if first is None or not first.startswith(MAGIC.decode()):
        raise EnvelopeError("no envelope header (is the forced command the gate?)")
    try:
        version = int(first.split()[1])
    except (IndexError, ValueError) as e:
        raise EnvelopeError(f"bad envelope header line {first!r}") from e
    env = Envelope(version=version, header={})
    while True:
        s = line()
        if s is None:
            raise EnvelopeError("envelope ends inside the header")
        if s == "HEADER-END":
            break
        k, sep, v = s.partition("=")
        if not sep:
            raise EnvelopeError(f"bad header line {s!r}")
        env.header[k] = v
    while True:
        s = line()
        if s is None:
            break  # truncated: no END
        if s.startswith("FRAME "):
            kv = _kv(s.split()[1:])
            try:
                name, rc, out_len, err_len = kv["name"], int(kv["rc"]), int(kv["out"]), int(kv["err"])
            except (KeyError, ValueError) as e:
                raise EnvelopeError(f"bad frame line {s!r}") from e
            if pos + out_len + err_len > n:
                raise EnvelopeError(f"frame {name!r} truncated")
            out = data[pos : pos + out_len]
            err = data[pos + out_len : pos + out_len + err_len]
            pos += out_len + err_len
            if pos < n and data[pos : pos + 1] == b"\n":
                pos += 1
            extra = {k: v for k, v in kv.items() if k not in ("name", "rc", "out", "err")}
            env.frames.append(Frame(name=name, rc=rc, out=bytes(out), err=bytes(err), attrs=extra))
        elif s.startswith("PROBE-END"):
            kv = _kv(s.split()[1:])
            env.probe_version = int(kv["version"]) if kv.get("version", "").isdigit() else 0
            env.probe_frames = int(kv["frames"]) if kv.get("frames", "").isdigit() else None
        elif s.startswith("END"):
            kv = _kv(s.split()[1:])
            env.gate_frames = int(kv["frames"]) if kv.get("frames", "").isdigit() else None
            env.complete = True
            break
        elif s.strip() == "":
            continue
        else:
            raise EnvelopeError(f"unexpected line in envelope: {s[:120]!r}")
    return env
