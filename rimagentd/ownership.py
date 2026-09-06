"""Ownership: which session may write a given path.

This is the only part a host configures, and it is the reason the engine stays
generic. The engine knows "a tree of nodes, each with an owner and a policy".
Which directory maps to which owner is site configuration, supplied via
$SCOOT_RIM_OWNERSHIP and never committed here.

Two rules, both deliberate:

  Ownership is POSITIONAL. The owner of a file is the session whose working
  directory contains it, resolved most-specific-first. It is never a session
  *name*, because names change across restarts and directories do not.

  Ownership governs who WRITES, never who is right. Nothing here ranks
  technical judgment; it serialises writes to contended files and nothing more.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# What a node may do with the files it owns.
#   own-subtree : free to write anything beneath itself; no coordination.
#   handoff     : contended. One writer (the owner); others hand off a payload.
#   strict      : gated. Writes AND side effects (service restarts) need the
#                 owner, because a collision costs more than recoverable text.
POLICIES = ("own-subtree", "handoff", "strict")

DEFAULT_POLICY = "own-subtree"


class OwnershipError(Exception):
    pass


@dataclass
class Node:
    path: str                       # relative to root, may contain * / **
    policy: str = DEFAULT_POLICY
    owns: list[str] = field(default_factory=list)  # globs relative to path
    note: str = ""

    @property
    def depth(self) -> int:
        return len([p for p in self.path.split("/") if p not in ("", ".")])


@dataclass
class Decision:
    allowed: bool
    reason: str
    node: Node | None = None
    owner_cwd: str = ""     # where the owning session must be, when denied

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Ownership:
    root: str
    nodes: list[Node] = field(default_factory=list)

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Ownership":
        cfg_path = path or os.environ.get("SCOOT_RIM_OWNERSHIP")
        if not cfg_path:
            raise OwnershipError(
                "no ownership map configured; set $SCOOT_RIM_OWNERSHIP "
                "(see examples/ownership.toml)"
            )
        cfg_path = Path(cfg_path)
        if not cfg_path.exists():
            raise OwnershipError(f"ownership map not found: {cfg_path}")

        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)

        root = data.get("root", "")
        if not root:
            raise OwnershipError(f"{cfg_path}: 'root' is required")

        nodes: list[Node] = []
        for raw in data.get("node", []):
            if "path" not in raw:
                raise OwnershipError(f"{cfg_path}: every [[node]] needs a 'path'")
            policy = raw.get("policy", DEFAULT_POLICY)
            if policy not in POLICIES:
                raise OwnershipError(
                    f"{cfg_path}: unknown policy {policy!r} for {raw['path']!r}; "
                    f"expected one of {', '.join(POLICIES)}"
                )
            nodes.append(
                Node(
                    path=raw["path"].rstrip("/"),
                    policy=policy,
                    owns=list(raw.get("owns", [])),
                    note=raw.get("note", ""),
                )
            )
        return cls(root=root.rstrip("/"), nodes=nodes)

    # -- resolution -------------------------------------------------------

    def relative(self, target: str) -> str | None:
        """Path relative to root, or None if outside the managed tree.
        Symlinks are resolved first so an aliased root maps to the same node."""
        try:
            resolved = Path(target).resolve()
            root = Path(self.root).resolve()
        except OSError:
            return None
        try:
            return str(resolved.relative_to(root))
        except ValueError:
            return None

    def _matches(self, node: Node, rel: str) -> bool:
        """Does `rel` fall under this node? A node with `owns` claims only those
        globs; a node without claims its whole subtree."""
        node_path = node.path
        if node_path in ("", "."):
            under, remainder = True, rel
        elif fnmatch.fnmatch(rel, node_path) or fnmatch.fnmatch(rel, node_path + "/*"):
            under = True
            remainder = rel[len(node_path):].lstrip("/") if rel.startswith(node_path) else ""
        else:
            # Glob node paths (e.g. "a/*/c") need per-segment matching.
            under = PurePosixPath(rel).match(node_path + "/**") if "*" in node_path else False
            remainder = ""
            if not under:
                return False

        if not node.owns:
            return under
        return any(fnmatch.fnmatch(remainder, g) or fnmatch.fnmatch(rel, g) for g in node.owns)

    def owner_of(self, target: str) -> Node | None:
        """Most specific node claiming `target`. Deeper paths win; among equals,
        a node with explicit `owns` beats one claiming a whole subtree, because
        naming a file is a stronger claim than containing it."""
        rel = self.relative(target)
        if rel is None:
            return None
        candidates = [n for n in self.nodes if self._matches(n, rel)]
        if not candidates:
            return None
        return max(candidates, key=lambda n: (n.depth, bool(n.owns)))

    def check(self, target: str, session_cwd: str) -> Decision:
        """May a session at `session_cwd` write `target`?"""
        node = self.owner_of(target)
        if node is None:
            return Decision(True, "outside any managed node")

        if node.policy == "own-subtree":
            return Decision(True, f"own-subtree ({node.path})", node)

        owner_cwd = str(Path(self.root) / node.path) if node.path not in ("", ".") else self.root
        cwd_rel = self.relative(session_cwd)

        # The owner is the session sitting at, or inside, the owning node.
        #
        # Normalising "." to "" matters: a node at the root owns its named
        # paths on behalf of a session sitting AT the root, not on behalf of
        # every session anywhere beneath it. Treating a root node as owned-by-
        # anyone would silently allow every caller through the strictest
        # policy in the map — the opposite of what it is for.
        node_path = "" if node.path in ("", ".") else node.path
        cwd_norm = "" if cwd_rel == "." else (cwd_rel or "")
        is_owner = cwd_rel is not None and (
            cwd_norm == node_path
            or (node_path != "" and cwd_norm.startswith(node_path + "/"))
        )
        if is_owner:
            label = node.path or "the root"
            return Decision(True, f"caller owns {label} (policy {node.policy})", node, owner_cwd)

        return Decision(
            False,
            f"{node.path} is {node.policy}; caller is at "
            f"{cwd_rel if cwd_rel is not None else session_cwd}. Hand the change to "
            f"the session working in {node.path} rather than writing it here.",
            node,
            owner_cwd,
        )
