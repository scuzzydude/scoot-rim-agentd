"""scoot-rim-agentd — session integration for multi-agent work.

Generic by construction: the engine knows only "a tree of nodes, each with an
owner and a policy". Site-specific mappings live in an ownership map supplied
via $SCOOT_RIM_OWNERSHIP and are never committed here.
"""

from .events import EventLog, default_log_path
from .fold import KNOWN_EVENTS, Session, State, fold
from .ownership import Decision, Node, Ownership, OwnershipError

__all__ = [
    "EventLog", "default_log_path",
    "fold", "State", "Session", "KNOWN_EVENTS",
    "Ownership", "Node", "Decision", "OwnershipError",
]
__version__ = "0.1.0"
