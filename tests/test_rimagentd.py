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

    def test_governing_node_finds_policy_for_a_directory_owner_of_misses(self):
        """The regression this method exists for.

        `shared` lists `owns`, so it claims only dashboard.html — asking
        owner_of() about the DIRECTORY finds no handoff node, and a session
        sitting there looks ungoverned. governing_node() is keyed on position
        and must still report handoff."""
        self.assertNotEqual(
            getattr(self.own.owner_of(str(self.root / "shared")), "policy", None),
            "handoff",
        )
        node = self.own.governing_node(str(self.root / "shared"))
        self.assertEqual(node.policy, "handoff")

    def test_governing_node_prefers_the_deeper_node(self):
        """Root is a node here ("."), so every directory matches it. A session
        inside components/a must report that node's policy, not root's."""
        node = self.own.governing_node(str(self.root / "components/a"))
        self.assertEqual(node.policy, "own-subtree")
        self.assertEqual(node.path, "components/*")

    def test_governing_node_outside_root_is_none(self):
        self.assertIsNone(self.own.governing_node("/etc"))

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


class TestApiSnapshot(unittest.TestCase):
    """The HTTP view must agree with the CLI, and must stay read-only."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.root = d / "root"
        for sub in ("components/a", "shared"):
            (self.root / sub).mkdir(parents=True)
        self.cfg = d / "own.toml"
        self.cfg.write_text(
            f'root = "{self.root}"\n'
            '[[node]]\npath = "components/*"\npolicy = "own-subtree"\n'
            '[[node]]\npath = "shared"\nowns = ["dashboard.html"]\npolicy = "handoff"\n'
        )
        self.log = d / "events.jsonl"
        lg = EventLog(self.log)
        lg.append("session_start", backend_id="1.a", name="alice",
                  cwd=str(self.root / "components/a"))
        lg.append("agent_bound", backend_id="1.a", agent_session_id="sess-a")
        lg.append("session_start", backend_id="2.dash", name="dash",
                  cwd=str(self.root / "shared"))
        lg.append("defer", item="drawings", reason="awaiting review")
        lg.append("escalate", question="which version", to="steve")

    def tearDown(self):
        self.dir.cleanup()

    def _snap(self):
        from rimagentd.api import snapshot
        return snapshot(self.log, self.cfg)

    def test_snapshot_reports_policy_per_position(self):
        snap = self._snap()
        by = {s["name"]: s for s in snap["sessions"]}
        self.assertEqual(by["alice"]["policy"], "own-subtree")
        # The regression governing_node() exists for, now reached over HTTP:
        # `shared` claims only dashboard.html, so owner_of() would say nothing.
        self.assertEqual(by["dash"]["policy"], "handoff")

    def test_snapshot_paths_are_relative_to_root(self):
        by = {s["name"]: s for s in self._snap()["sessions"]}
        self.assertEqual(by["alice"]["rel"], "components/a")

    def test_snapshot_marks_unbound_and_restorable(self):
        by = {s["name"]: s for s in self._snap()["sessions"]}
        self.assertTrue(by["alice"]["bound"])
        self.assertFalse(by["dash"]["bound"])
        self.assertTrue(by["dash"]["restorable"])

    def test_snapshot_carries_open_items(self):
        snap = self._snap()
        self.assertEqual([x["item"] for x in snap["deferred"]], ["drawings"])
        self.assertEqual([x["question"] for x in snap["escalated"]], ["which version"])
        self.assertEqual(snap["counts"]["unbound"], 1)

    def test_snapshot_works_with_no_ownership_map(self):
        """A host with no site config must still get its sessions."""
        from rimagentd.api import snapshot
        snap = snapshot(self.log, self.dir.name + "/absent.toml")
        self.assertEqual(len(snap["sessions"]), 2)
        self.assertEqual(snap["root"], "")
        self.assertTrue(all(s["policy"] == "" for s in snap["sessions"]))

    def test_api_declares_no_mutating_route(self):
        """Read-only is a design constraint, not an oversight. If a POST/PUT/
        DELETE handler is ever added, this test should fail and the author
        should have to justify it."""
        from rimagentd import api
        handlers = [n for n in dir(api._Handler) if n.startswith("do_")]
        self.assertEqual(sorted(handlers), ["do_GET", "do_HEAD", "do_POST"])
        # do_POST exists only to answer 405.
        import inspect
        self.assertIn("read-only", inspect.getsource(api._Handler.do_POST))
