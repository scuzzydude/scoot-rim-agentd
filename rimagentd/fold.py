"""Fold the event log into current state.

Deliberately a pure function of the log: no cached snapshot, no separate
"current state" file to fall out of sync. Restart recovery is therefore not a
special code path — it is just this fold, run after a reboot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Session:
    backend_id: str
    name: str = ""
    cwd: str = ""
    agent_session_id: str = ""
    origin: str = ""
    started_at: str = ""

    @property
    def bound(self) -> bool:
        """True when we know which conversation to resume, not merely which
        directory to reopen. An unbound session can only be restored
        approximately."""
        return bool(self.agent_session_id)


@dataclass
class State:
    live: dict[str, Session] = field(default_factory=dict)
    exited: dict[str, Session] = field(default_factory=dict)
    claims: dict[str, str] = field(default_factory=dict)      # resource -> backend_id
    deferred: dict[str, dict] = field(default_factory=dict)   # item -> record
    escalated: dict[str, dict] = field(default_factory=dict)  # question -> record

    def sessions_sorted(self) -> list[Session]:
        return sorted(self.live.values(), key=lambda s: (s.name.lower(), s.backend_id))


# Event types the fold acts on. Exported so a writer can be told when it used a
# type nothing consumes: the log is append-only and extensible, so an unknown
# type is legal and is kept -- but it also means a typo ("deferred" for "defer")
# is accepted in silence and the item never appears in `open`. Losing deferred
# work to silence is the exact failure this log exists to prevent, so the
# vocabulary is published rather than left implicit in an if-chain.
KNOWN_EVENTS = (
    "session_start", "session_exit", "agent_bound", "agent_bind_failed",
    "claim", "release",
    "defer", "resume",
    "escalate", "decide",
    "reconcile", "restored", "gate_warn",
)


def fold(records: Iterable[dict]) -> State:
    """Replay records oldest-first into current state.

    A session is live iff it has a session_start and no later session_exit.
    That set — not "everything ever started" — is the recovery set.
    """
    state = State()

    for rec in records:
        event = rec.get("event", "")
        bid = rec.get("backend_id", "")

        if event == "session_start" and bid:
            state.exited.pop(bid, None)
            state.live[bid] = Session(
                backend_id=bid,
                name=rec.get("name", ""),
                cwd=rec.get("cwd", ""),
                origin=rec.get("origin", ""),
                started_at=rec.get("ts", ""),
            )

        elif event == "session_exit" and bid:
            session = state.live.pop(bid, None)
            if session is None:
                # Exit without a start: a log that begins mid-history, or an
                # adopted session closed before it was ever recorded as started.
                session = Session(backend_id=bid, name=rec.get("name", ""))
            state.exited[bid] = session

        elif event == "agent_bound" and bid:
            session = state.live.get(bid)
            if session is not None:
                session.agent_session_id = rec.get("agent_session_id", "")
                if not session.cwd:
                    session.cwd = rec.get("cwd", "")

        elif event == "claim":
            resource = rec.get("resource", "")
            if resource:
                state.claims[resource] = bid

        elif event == "release":
            state.claims.pop(rec.get("resource", ""), None)

        elif event == "defer":
            item = rec.get("item", "")
            if item:
                state.deferred[item] = rec

        elif event == "resume":
            state.deferred.pop(rec.get("item", ""), None)

        elif event == "escalate":
            question = rec.get("question", "")
            if question:
                state.escalated[question] = rec

        elif event == "decide":
            state.escalated.pop(rec.get("question", ""), None)

    # A claim held by a session that is no longer live is stale by definition;
    # dropping it here keeps callers from having to re-derive that themselves.
    for resource, holder in list(state.claims.items()):
        if holder and holder not in state.live:
            del state.claims[resource]

    return state
