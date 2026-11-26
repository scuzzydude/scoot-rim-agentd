"""Infer which agent conversation belongs to which live session.

WHY THIS IS INFERENCE AND NOT A LOOKUP:

A session registered from the inside knows its own conversation id exactly (the
agent exports it into the shell). A session *adopted* from the outside does not,
and cannot be made to: the running agent keeps no file descriptor on its
transcript, and does not carry the id in its own environment — both verified by
reading /proc for live agents, not assumed.

What is left is evidence, and the honest thing is to weigh it and say how
strong it is rather than to guess and record the guess as fact. A wrong binding
is worse than no binding: `restore` would resume the wrong conversation
silently, whereas an absent binding makes it say so and refuse.

THE HARD CASE, WHICH IS THE COMMON CASE HERE:

The agent stores conversations in a directory named after the launch cwd, with
separators flattened. That mapping breaks whenever a directory is renamed after
a session starts, and it breaks in BOTH directions:

  - the session's recorded cwd is the NEW name, while its conversation is still
    filed under the OLD one (the project directory is never renamed);
  - or the process's cwd resolves to a name that no longer matches what the
    session was launched as, because the inode was renamed under it.

Observed live: one session of each kind out of twelve. So no single derivation
is correct, and this tries every source it has, then requires agreement between
independent signals before calling a match confident.

EXCLUSIVITY: two sessions must never be offered the same conversation. Matches
are assigned strongest-first, and a conversation already taken is withdrawn from
the weaker claimant, which is then reported as unresolved rather than given a
plausible-looking wrong answer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Where the agent runtime files conversations. Overridable because it is the one
# genuinely product-specific path in this package.
PROJECTS_DIR = Path(
    os.environ.get("SCOOT_RIM_AGENT_PROJECTS") or (Path.home() / ".claude" / "projects")
)

# A transcript touched this recently is being actively written, which is the
# single strongest signal that it belongs to a session that is still running.
ACTIVE_WINDOW_S = 45 * 60

# Minimum parent depth before the renamed-leaf fallback will match on a shared
# parent. A shallow parent ("/srv", "/home") is shared by most of the tree, so
# matching on it would pair a session with an unrelated conversation.
MIN_PARENT_DEPTH = 3


def encode_cwd(cwd: str) -> str:
    """Directory name the agent runtime uses for a given launch cwd.

    Both '/' and '_' collapse to '-', verified against real directories rather
    than inferred from one example — '_' mattering is the non-obvious half.
    """
    return re.sub(r"[/_]", "-", cwd)


def cwd_variants(cwd: str) -> list[str]:
    """A cwd's distinct spellings, literal first, then symlink-resolved.

    A session launched through a symlinked path has two names for one directory
    — on this host /srv/bregman is a symlink to /srv/steveai/bregman, so four
    sessions ran with a cwd under the former while their conversations are filed
    under the latter. Deriving the project directory from one spelling and
    looking for the other finds nothing, silently: the session shows up with no
    conversation bound and nothing reports an error.

    Both spellings are returned rather than picking one. Observed behaviour on
    this host is that the runtime files under the RESOLVED path, but that is one
    host's evidence and the wrong thing to bet the lookup on — trying both costs
    a stat and is correct either way. Literal stays first so a host with no
    symlinks in play behaves exactly as before.
    """
    out = [cwd]
    if cwd:
        try:
            real = os.path.realpath(cwd)
        except OSError:
            return out
        if real and real != cwd:
            out.append(real)
    return out


def _readlink(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _cmdline(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def claude_pid_and_cwd(screen_pid: str, max_depth: int = 3) -> tuple[str, str]:
    """The agent process beneath a screen session, and its cwd.

    Preferred over the screen process's own cwd: when a directory is renamed the
    two disagree, and the agent's is the one that resolves to a live inode.

    Two guards that are correctness, not just speed. The pid must actually be a
    session-backend process: a stale or synthetic backend_id can otherwise name
    pid 1, and walking init's children finds *some* unrelated agent and reports
    its cwd as this session's -- a wrong answer, arrived at expensively (it also
    took a test suite from 0.05s to 52s, which is how it was noticed). And the
    descent is depth- and breadth-bounded, because a process tree is not
    guaranteed shallow.
    """
    pid = str(screen_pid)
    if not pid.isdigit() or pid == "1":
        return "", ""
    if "screen" not in _cmdline(pid).lower():
        return "", ""

    frontier, seen = [pid], {pid}
    for _ in range(max_depth):
        if not frontier:
            break
        nxt: list[str] = []
        for parent in frontier:
            try:
                kids = subprocess.run(["pgrep", "-P", parent],
                                      capture_output=True, text=True).stdout.split()
            except FileNotFoundError:
                return "", ""
            for kid in kids:
                if kid in seen or len(seen) > 64:
                    continue
                seen.add(kid)
                if "claude" in _cmdline(kid):
                    return kid, _readlink(f"/proc/{kid}/cwd")
                nxt.append(kid)
        frontier = nxt
    return "", ""


def transcript_cwds(path: Path, tail_bytes: int = 200_000) -> set[str]:
    """Distinct cwds a transcript records, from its tail.

    Only the tail: transcripts reach hundreds of megabytes and the recent
    records are what pin the session's current position. Note this set includes
    directories a tool shell wandered into, so it is corroborating evidence
    only — never a match on its own.
    """
    found: set[str] = set()
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # discard the partial line
            for raw in fh:
                if b'"cwd"' not in raw:
                    continue
                try:
                    cwd = json.loads(raw).get("cwd")
                except Exception:
                    continue
                if cwd:
                    found.add(cwd)
    except OSError:
        pass
    return found


@dataclass
class Candidate:
    session_id: str
    path: Path
    mtime: float
    signals: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return len(self.signals)

    @property
    def confidence(self) -> str:
        # "high" requires the directory match AND evidence the transcript is
        # live AND no ambiguity within that directory. Any one of those alone is
        # not enough to write down as fact.
        if "dir-match" in self.signals and "active" in self.signals and (
            "sole-transcript" in self.signals or "cwd-recorded" in self.signals
        ):
            return "high"
        if "dir-match" in self.signals and ("active" in self.signals or "newest" in self.signals):
            return "medium"
        # Capped deliberately: a content match is weaker than a position match,
        # because a transcript records every directory a tool shell visited.
        if "found-by-cwd-scan" in self.signals or "sibling-dir" in self.signals:
            return "medium"
        return "low"


def candidates_for(cwd: str, screen_pid: str = "",
                   projects: Path | None = None) -> list[Candidate]:
    """Conversations that could belong to a session at `cwd`, best first."""
    projects = projects or PROJECTS_DIR
    now = time.time()

    # Every derivation of the project directory that this host can offer. The
    # raw (pre-strip) readlink values matter: a " (deleted)" suffix carries the
    # ORIGINAL name, which is often exactly the one the conversation is filed
    # under.
    sources: list[str] = list(cwd_variants(cwd))
    if screen_pid:
        raw_screen = _readlink(f"/proc/{screen_pid}/cwd")
        _, raw_agent = claude_pid_and_cwd(screen_pid)
        for raw in (raw_screen, raw_agent):
            if raw:
                for v in (raw, raw.removesuffix(" (deleted)")):
                    sources += cwd_variants(v)

    dirs: list[Path] = []
    for src in sources:
        if not src:
            continue
        d = projects / encode_cwd(src)
        if d.is_dir() and d not in dirs:
            dirs.append(d)

    out: list[Candidate] = []
    for d in dirs:
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for i, f in enumerate(files):
            st = f.stat()
            signals = ["dir-match"]
            if now - st.st_mtime < ACTIVE_WINDOW_S:
                signals.append("active")
            if len(files) == 1:
                signals.append("sole-transcript")
            elif i == 0:
                signals.append("newest")
            if cwd and any(v in transcript_cwds(f) for v in cwd_variants(cwd)):
                signals.append("cwd-recorded")
            out.append(Candidate(f.stem, f, st.st_mtime, signals))

    if not out and cwd:
        out = _search_sibling_dirs(cwd, projects, now)
    if not out and cwd:
        out = _search_by_recorded_cwd(cwd, projects, now)

    out.sort(key=lambda c: (c.score, c.mtime), reverse=True)

    # One conversation, one candidate. Two spellings of a cwd (§cwd_variants)
    # can encode to two project-directory names holding the same conversation
    # id, and offering it twice would corrupt the exclusive assignment
    # downstream — the rule there is that two sessions must never be offered
    # the same conversation. Deduped after the sort, so the strongest signal set
    # for a given id is the one kept.
    seen: set[str] = set()
    unique: list[Candidate] = []
    for c in out:
        if c.session_id in seen:
            continue
        seen.add(c.session_id)
        unique.append(c)
    return unique


def _search_sibling_dirs(cwd: str, projects: Path, now: float) -> list[Candidate]:
    """Handle a renamed leaf directory, positionally.

    The unreachable case: a session's directory is renamed, so every /proc cwd
    link reports the NEW leaf name while the conversation stays filed under the
    OLD one. Nothing on the host remembers the old leaf — but the PARENT path is
    untouched, and that is still positional evidence rather than content
    guessing.

    So: look for actively-written conversations filed under a directory that
    shares this cwd's parent. Exclusivity then does the rest — the parent's own
    session claims the parent's own directory, and a renamed child is typically
    the only sibling left over.

    Guarded, because a shallow parent would match most of the tree: the parent
    must be at least MIN_PARENT_DEPTH segments deep, and confidence is capped at
    medium so `--apply` will not record one of these without being asked twice.
    """
    parent = str(Path(cwd).parent)
    if parent in ("/", "", "."):
        return []
    if len([p for p in parent.split("/") if p]) < MIN_PARENT_DEPTH:
        return []
    prefixes = [encode_cwd(v) for v in cwd_variants(parent)]
    found: list[Candidate] = []
    try:
        entries = list(projects.iterdir())
    except OSError:
        return found
    for d in entries:
        if not d.is_dir() or not any(d.name.startswith(p) for p in prefixes):
            continue
        for f in d.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if now - mtime < ACTIVE_WINDOW_S:
                found.append(Candidate(f.stem, f, mtime, ["sibling-dir", "active"]))
    return found


def _search_by_recorded_cwd(cwd: str, projects: Path, now: float) -> list[Candidate]:
    """Last resort: find a conversation by what it says about itself.

    Needed for the rename case that no cwd derivation can reach. When a
    directory is renamed, every /proc cwd link reports the NEW name — the rename
    happened to the inode — while the conversation stays filed under the OLD one.
    Nothing outside the transcript remembers the old name, so the only way in is
    to read transcripts and see which one has been working in this directory.

    Deliberately narrow, because content matching is weaker than position:
      - only reached when no directory derivation matched at all;
      - only actively-written transcripts are considered;
      - a tie is reported as several candidates rather than resolved by guess;
      - the signal is named separately so confidence is capped below "high" —
        a transcript records every directory a tool shell wandered into, not
        just the session's own, so this can produce a coincidence.
    """
    found: list[Candidate] = []
    try:
        entries = list(projects.iterdir())
    except OSError:
        return found
    for d in entries:
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if now - mtime >= ACTIVE_WINDOW_S:
                continue
            if any(v in transcript_cwds(f) for v in cwd_variants(cwd)):
                found.append(Candidate(f.stem, f, mtime, ["found-by-cwd-scan", "active"]))
    return found


def suggest(sessions: list, projects: Path | None = None) -> dict[str, dict]:
    """Best conversation per session, assigned exclusively.

    `sessions` are fold Session objects. Returns backend_id -> result, where a
    result carries the chosen candidate (or None) plus every candidate seen, so
    a caller can show its work instead of just its answer.
    """
    per_session: dict[str, list[Candidate]] = {}
    for s in sessions:
        if s.bound or not s.cwd:
            continue
        per_session[s.backend_id] = candidates_for(
            s.cwd, s.backend_id.split(".", 1)[0], projects
        )

    # Assign strongest-first so a contested conversation goes to the session
    # with better evidence, and the loser is reported unresolved rather than
    # handed a plausible wrong answer.
    ranked = sorted(
        per_session.items(),
        key=lambda kv: (kv[1][0].score if kv[1] else 0, kv[1][0].mtime if kv[1] else 0),
        reverse=True,
    )
    taken: dict[str, str] = {}
    results: dict[str, dict] = {}
    for backend_id, cands in ranked:
        chosen = None
        for c in cands:
            if c.session_id not in taken:
                chosen = c
                taken[c.session_id] = backend_id
                break
        results[backend_id] = {
            "chosen": chosen,
            "candidates": cands,
            "contested": [c.session_id for c in cands if c.session_id in taken
                          and taken[c.session_id] != backend_id],
        }
    return results
