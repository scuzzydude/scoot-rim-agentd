"""Tests for the fold, the event log, and ownership resolution.

stdlib unittest only — the package has no dependencies and neither do its tests.
Run: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rimagentd import EventLog, Ownership, OwnershipError, fold  # noqa: E402


class TestFold(unittest.TestCase):
    def test_live_set_excludes_exited(self):
        state = fold([
            {"event": "session_start", "backend_id": "1.a", "name": "a", "cwd": "/x"},
            {"event": "session_start", "backend_id": "2.b", "name": "b", "cwd": "/y"},
            {"event": "session_exit", "backend_id": "2.b"},
        ])
        self.assertEqual(set(state.live), {"1.a"})
        self.assertEqual(set(state.exited), {"2.b"})

    def test_restart_after_exit_is_live_again(self):
        """A backend id can be reused; the later start must win."""
        state = fold([
            {"event": "session_start", "backend_id": "1.a", "name": "a"},
            {"event": "session_exit", "backend_id": "1.a"},
            {"event": "session_start", "backend_id": "1.a", "name": "a"},
        ])
        self.assertIn("1.a", state.live)
        self.assertNotIn("1.a", state.exited)

    def test_agent_bound_marks_session_restorable(self):
        state = fold([
            {"event": "session_start", "backend_id": "1.a", "name": "a", "cwd": "/x"},
            {"event": "agent_bound", "backend_id": "1.a", "agent_session_id": "sid-1"},
        ])
        self.assertTrue(state.live["1.a"].bound)
        self.assertEqual(state.live["1.a"].agent_session_id, "sid-1")

    def test_unbound_session_is_not_restorable(self):
        state = fold([{"event": "session_start", "backend_id": "1.a", "name": "a"}])
        self.assertFalse(state.live["1.a"].bound)

    def test_exit_without_start_does_not_crash(self):
        """The log may begin mid-history; an orphan exit must be tolerated."""
        state = fold([{"event": "session_exit", "backend_id": "9.z", "name": "z"}])
        self.assertIn("9.z", state.exited)
        self.assertEqual(state.live, {})

    def test_defer_resume_and_escalate_decide(self):
        state = fold([
            {"event": "defer", "item": "task-1", "reason": "blocked"},
            {"event": "defer", "item": "task-2"},
            {"event": "resume", "item": "task-1"},
            {"event": "escalate", "question": "q1", "to": "human"},
            {"event": "escalate", "question": "q2"},
            {"event": "decide", "question": "q2", "outcome": "yes"},
        ])
        self.assertEqual(set(state.deferred), {"task-2"})
        self.assertEqual(set(state.escalated), {"q1"})

    def test_claim_released_and_stale_claim_dropped(self):
        state = fold([
            {"event": "session_start", "backend_id": "1.a", "name": "a"},
            {"event": "claim", "backend_id": "1.a", "resource": "held"},
            {"event": "claim", "backend_id": "2.gone", "resource": "orphan"},
            {"event": "claim", "backend_id": "1.a", "resource": "freed"},
            {"event": "release", "resource": "freed"},
        ])
        # "orphan" is dropped: its holder was never live, so the claim cannot
        # be real. Otherwise a crashed session would block a resource forever.
        self.assertEqual(set(state.claims), {"held"})


class TestEventLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = EventLog(Path(self.dir.name) / "events.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_and_field_omission(self):
        self.log.append("session_start", name="a", backend_id="1.a", purpose="")
        rec = next(iter(self.log.records()))
        self.assertEqual(rec["event"], "session_start")
        self.assertEqual(rec["name"], "a")
        # Empty values are omitted, matching the shell writer's behaviour.
        self.assertNotIn("purpose", rec)
        self.assertEqual(rec["schema_version"], 1)

    def test_values_with_quotes_tabs_newlines_survive(self):
        nasty = 'has "quotes" and \\back and\ttab and\nnewline'
        self.log.append("defer", item=nasty)
        self.assertEqual(next(iter(self.log.records()))["item"], nasty)

    def test_malformed_line_skipped_not_fatal(self):
        self.log.append("session_start", backend_id="1.a", name="a")
        with open(self.log.path, "a") as fh:
            fh.write("this is not json\n")
        self.log.append("session_start", backend_id="2.b", name="b")
        # One bad line must not make the rest of history unreadable.
        self.assertEqual(len(list(self.log.records())), 2)
        self.assertEqual(self.log.malformed(), 1)

    def test_missing_log_reads_as_empty(self):
        self.assertEqual(list(EventLog(Path(self.dir.name) / "nope.jsonl").records()), [])


class TestOwnership(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "root"
        for sub in ("components/a", "components/b", "shared", "deploy"):
            (self.root / sub).mkdir(parents=True)
        self.cfg = Path(self.dir.name) / "own.toml"
        self.cfg.write_text(
            f'root = "{self.root}"\n'
            '[[node]]\npath = "components/*"\npolicy = "own-subtree"\n'
            '[[node]]\npath = "shared"\nowns = ["dashboard.html"]\npolicy = "handoff"\n'
            '[[node]]\npath = "."\nowns = ["deploy/*"]\npolicy = "strict"\n'
        )
        self.own = Ownership.load(self.cfg)

    def tearDown(self):
        self.dir.cleanup()

    def test_non_owner_denied_on_contended_file(self):
        d = self.own.check(str(self.root / "shared/dashboard.html"),
                           str(self.root / "components/a"))
        self.assertFalse(d.allowed)
        self.assertEqual(d.owner_cwd, str(self.root / "shared"))

    def test_owner_allowed_on_contended_file(self):
        d = self.own.check(str(self.root / "shared/dashboard.html"),
                           str(self.root / "shared"))
        self.assertTrue(d.allowed)

    def test_own_subtree_is_free(self):
        d = self.own.check(str(self.root / "components/a/notes.md"),
                           str(self.root / "components/a"))
        self.assertTrue(d.allowed)

    def test_unclaimed_file_in_handoff_node_is_not_contended(self):
        """The node lists `owns`, so it claims only those files — not its
        whole subtree. Anything else there stays free."""
        node = self.own.owner_of(str(self.root / "shared/scratch.txt"))
        self.assertNotEqual(getattr(node, "policy", None), "handoff")

    def test_strict_node_gates_its_named_paths(self):
        d = self.own.check(str(self.root / "deploy/app.service"),
                           str(self.root / "components/a"))
        self.assertFalse(d.allowed)

    def test_path_outside_root_is_allowed(self):
        d = self.own.check("/etc/hosts", str(self.root / "shared"))
        self.assertTrue(d.allowed)
        self.assertIsNone(d.node)

    def test_symlinked_root_alias_resolves_to_same_node(self):
        """Sessions refer to the tree by more than one path; an alias must not
        change the answer."""
        alias = Path(self.dir.name) / "alias"
        alias.symlink_to(self.root)
        d = self.own.check(str(alias / "shared/dashboard.html"),
                           str(alias / "components/a"))
        self.assertFalse(d.allowed)

    def test_bad_policy_is_rejected_at_load(self):
        bad = Path(self.dir.name) / "bad.toml"
        bad.write_text(f'root = "{self.root}"\n[[node]]\npath = "x"\npolicy = "nonsense"\n')
        with self.assertRaises(OwnershipError):
            Ownership.load(bad)

    def test_missing_root_is_rejected_at_load(self):
        bad = Path(self.dir.name) / "noroot.toml"
        bad.write_text('[[node]]\npath = "x"\n')
        with self.assertRaises(OwnershipError):
            Ownership.load(bad)

    def test_missing_config_is_reported_clearly(self):
        with self.assertRaises(OwnershipError):
            Ownership.load(Path(self.dir.name) / "does-not-exist.toml")


if __name__ == "__main__":
    unittest.main()
