"""Push: a host's agent POSTs the envelope the gate would otherwise have sent over ssh.

For hosts that are only up on demand, and for hosts that should hold no inbound key at all (the hypervisor
the hub runs on). The envelope, its ingest and what is recorded about the attempt are the same as for a pull
(collect.ingest_result); what differs is who starts it and how the host proves who it is: a token per host,
of which the hub keeps only the hash.

The reply is plain text, because a POSIX shell reads it:

    ok host=pve payload_id=... runs=4 pools=1 attrlog_lines=12
    cursor attrlog.MODEL-SERIAL.ata.csv=123456

The cursors are how far the hub has read each of the host's attrlog files; the agent sends from there next
time. The hub stays the authority on them in both transports.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import zlib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from drivecanary.collect import SshResult, ingest_result
from drivecanary.config import Config
from drivecanary.logging import get_logger
from drivecanary.models import AttrlogCursor, FailureClass, Host, HostAttempt, HostState, Transport
from drivecanary.timeutil import utcnow

log = get_logger(__name__)

_PAYLOAD_ID = re.compile(rb"^payload_id=([0-9A-Za-z]{8,64})$", re.M)
#: failure classes that mean the payload itself is unusable: the agent is told to drop it
_UNUSABLE = (FailureClass.ENVELOPE, FailureClass.TOO_LARGE)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """(the token, to be shown once and put on the host; its hash, which is all the hub keeps)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


@dataclass
class PushReply:
    status: int
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


class BodyTooLarge(Exception):
    pass


def inflate(body: bytes, encoding: str | None, limit: int) -> bytes:
    """The request body as the envelope's bytes. gzip is inflated no further than `limit`: a request of a
    few kilobytes must not be able to become gigabytes in memory."""
    if len(body) > limit:
        raise BodyTooLarge
    if not encoding or encoding.lower() == "identity":
        return body
    if encoding.lower() != "gzip":
        raise ValueError(f"Content-Encoding {encoding!r} is not understood (gzip or none)")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise BodyTooLarge
    return out


def host_for_token(session: Session, token: str) -> Host | None:
    wanted = hash_token(token)
    host = session.scalar(select(Host).where(Host.push_token_hash == wanted, Host.transport == Transport.PUSH.value))
    if host is None or host.push_token_hash is None or not hmac.compare_digest(host.push_token_hash, wanted):
        return None
    return host


def cursor_lines(session: Session, host_id: int) -> list[str]:
    rows = session.scalars(
        select(AttrlogCursor).where(AttrlogCursor.host_id == host_id).order_by(AttrlogCursor.file_name)
    )
    return [f"cursor {c.file_name}={c.offset}" for c in rows if c.offset > 0]


def receive(
    cfg: Config,
    factory: sessionmaker[Session],
    *,
    token: str | None,
    body: bytes,
    encoding: str | None = None,
    agent_failures: int = 0,
    agent_last_failure: str | None = None,
) -> PushReply:
    if not token:
        return PushReply(401, ["refused: no token (Authorization: Bearer ...)"])
    with factory() as s:
        host = host_for_token(s, token)
        if host is None:
            return PushReply(403, ["refused: this token belongs to no push host"])
        if host.state in (HostState.PAUSED.value, HostState.RETIRED.value):
            return PushReply(403, [f"refused: host {host.name} is {host.state} on the hub"])
        host_id, name = host.id, host.name

    limit = cfg.collect.max_output_mb * 1024 * 1024
    try:
        data = inflate(body, encoding, limit)
    except BodyTooLarge:
        _failed(factory, host_id, FailureClass.TOO_LARGE, f"payload exceeds {cfg.collect.max_output_mb} MB inflated")
        return PushReply(413, [f"refused: larger than {cfg.collect.max_output_mb} MB"])
    except (ValueError, zlib.error) as e:
        _failed(factory, host_id, FailureClass.ENVELOPE, f"body could not be read: {e}")
        return PushReply(400, [f"refused: {e}"])

    m = _PAYLOAD_ID.search(data[:4096])
    payload_id = m.group(1).decode() if m else None
    try:
        with factory() as s:
            if payload_id is not None:
                seen = s.scalar(
                    select(HostAttempt.id).where(
                        HostAttempt.host_id == host_id, HostAttempt.payload_id == payload_id, HostAttempt.ok
                    )
                )
                if seen is not None:  # a retry of something already stored: say so, and say where we are
                    return PushReply(
                        200, [f"ok host={name} payload_id={payload_id} duplicate=1", *cursor_lines(s, host_id)]
                    )
            now = utcnow()
            attempt = HostAttempt(host_id=host_id, started_at=now, transport=Transport.PUSH.value)
            s.add(attempt)
            pushed = s.get(Host, host_id)
            assert pushed is not None
            pushed.last_attempt_at = now
            s.commit()
            attempt_id = attempt.id
        outcome = ingest_result(cfg, factory, host_id, attempt_id, SshResult(rc=0, out=data, err=b""))
        with factory() as s:
            if agent_failures:
                a = s.get(HostAttempt, attempt_id)
                assert a is not None
                note = f"agent: {agent_failures} delivery failure(s) before this one"
                if agent_last_failure:
                    note += f"; last: {agent_last_failure[:200]}"
                a.reason = f"{a.reason}\n{note}" if a.reason else note
                s.commit()
            cursors = cursor_lines(s, host_id)
    except OperationalError as e:  # the database is busy beyond the wait: the agent keeps the payload
        log.warning("push.busy", host=name, error=str(e))
        return PushReply(503, ["busy: the hub's database is locked; try again"])

    if outcome.ok and outcome.result is not None:
        r = outcome.result
        head = f"ok host={name} payload_id={payload_id} runs={r.runs} pools={r.pools} attrlog_lines={r.attrlog_lines}"
        return PushReply(200, [head, *(f"warning {w}" for w in r.warnings), *cursors])
    fclass = outcome.failure_class or FailureClass.UNKNOWN
    line = f"failed class={fclass.value} reason={outcome.reason}"
    # delivered, and nothing to retry: either the payload is unusable, or the host's probe did not run
    return PushReply(400 if fclass in _UNUSABLE else 200, [line, *cursors])


def _failed(factory: sessionmaker[Session], host_id: int, fclass: FailureClass, reason: str) -> None:
    with factory() as s:
        host = s.get(Host, host_id)
        assert host is not None
        now = utcnow()
        s.add(
            HostAttempt(
                host_id=host_id,
                started_at=now,
                finished_at=now,
                transport=Transport.PUSH.value,
                ok=False,
                failure_class=fclass.value,
                reason=reason,
            )
        )
        host.last_attempt_at = now
        host.last_attempt_class = fclass.value
        host.last_attempt_reason = reason
        host.state = HostState.BROKEN.value
        s.commit()
    log.warning("push.refused", host_id=host_id, failure_class=fclass.value, reason=reason)
