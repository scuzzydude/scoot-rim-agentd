"""Append-only event log of agent-session state transitions.

Reads and writes the same newline-delimited JSON log that the shell layer
writes, so the two can coexist during migration: the shell side is the writer
today (it is already in the interactive path and needs no daemon), and this is
the reader that grows into one.

Compatibility contract with the shell writer:
  - one JSON object per line, append-only, never rewritten
  - keys: schema_version, ts, event, host, then event-specific string fields
  - every value is a JSON string (the shell side has no type information)
  - writes are serialised with an flock on "<log>.lock"

State is a fold over this log rather than a stored snapshot. That is the whole
point: a snapshot cannot answer "what was live before the reboot, and what was
left unfinished", because an ended session is simply absent from it.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


def current_boot_id() -> str:
    """Identifier for the running kernel boot; changes on every reboot.

    Preferred over comparing timestamps to boot time because it is immune to the
    clock moving — this host has had its clock jump by a day, which would make a
    timestamp comparison silently misclassify sessions.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def boot_time() -> float:
    """Unix time the host booted, or 0.0 if unavailable.

    Fallback for records written before boot_id was recorded: a session whose
    start predates the current boot cannot still be running.
    """
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0.0


def default_log_path() -> Path:
    """Log location, overridable so tests never touch the real one."""
    home = os.environ.get("SCOOT_RIM_HOME") or str(Path.home() / ".scoot-rim")
    return Path(os.environ.get("SCOOT_RIM_EVENTS") or Path(home) / "events.jsonl")


class EventLog:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_log_path()
        self.lock_path = Path(str(self.path) + ".lock")

    # -- writing ----------------------------------------------------------

    def append(self, event: str, **fields: Any) -> dict:
        """Append one event. Empty/None fields are dropped, matching the shell
        writer, which omits a key rather than emitting an empty string."""
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "event": event,
            "host": socket.gethostname().split(".")[0],
            # Which boot this happened under. Load-bearing for recovery: after a
            # reboot every session's backend is gone, and without this there is
            # no way to tell "this session ended" from "the host restarted under
            # it". Conflating the two lets a reconcile pass close the entire
            # recovery set before anything can restore it.
            "boot_id": current_boot_id(),
        }
        for key, value in fields.items():
            if value is None or value == "":
                continue
            record[key] = str(value)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(self.lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return record

    # -- reading ----------------------------------------------------------

    def records(self) -> Iterator[dict]:
        """Every well-formed record, oldest first.

        A malformed line is skipped rather than fatal: this is an append-only
        log written by more than one process, and one bad line must not make
        the whole history unreadable. Bad lines are counted by malformed().
        """
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj

    def malformed(self) -> int:
        """Count of unparseable non-empty lines — surfaced by `doctor` rather
        than hidden, since silent skipping could otherwise mask corruption."""
        if not self.path.exists():
            return 0
        bad = 0
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
        return bad
