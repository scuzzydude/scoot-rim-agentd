"""Tests for the fold, the event log, and ownership resolution.

stdlib unittest only — the package has no dependencies and neither do its tests.
Run: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import subprocess
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

    def test_session_outside_root_is_flagged_not_given_a_fake_path(self):
        """Regression. `rel` used to fall back to the absolute path, which the
        tree then split on "/" — inventing a bogus "/ → home → steve" branch
        hanging off the root. Outside-the-tree must be reported as such."""
        lg = EventLog(self.log)
        lg.append("session_start", backend_id="9.out", name="elsewhere", cwd="/tmp")
        by = {s["name"]: s for s in self._snap()["sessions"]}
        self.assertFalse(by["elsewhere"]["in_root"])
        self.assertIsNone(by["elsewhere"]["rel"])
        self.assertTrue(by["alice"]["in_root"])

    def test_policy_is_marked_partial_when_node_claims_only_named_paths(self):
        """`shared` claims only dashboard.html, so a session sitting there is
        NOT wholesale governed by handoff — reporting a bare policy overstates
        it. components/* claims its subtree, so that one is not partial."""
        by = {s["name"]: s for s in self._snap()["sessions"]}
        self.assertTrue(by["dash"]["policy_partial"])
        self.assertFalse(by["alice"]["policy_partial"])

    def test_unrestorable_count_tracks_missing_cwds(self):
        EventLog(self.log).append("session_start", backend_id="9.g", name="gone",
                                  cwd=str(self.root / "vanished"))
        snap = self._snap()
        self.assertEqual(snap["counts"]["unrestorable"], 1)


class TestBinding(unittest.TestCase):
    """Inferring which conversation belongs to which adopted session.

    The cases here are the ones seen live, not invented: a clean match, a
    directory with several conversations, a renamed leaf, and two sessions
    competing for one conversation.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.projects = Path(self.dir.name) / "projects"
        self.projects.mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def _mk(self, cwd, session_id, age_s=60, cwds=()):
        from rimagentd.binding import encode_cwd
        d = self.projects / encode_cwd(cwd)
        d.mkdir(exist_ok=True)
        f = d / f"{session_id}.jsonl"
        f.write_text("".join(
            '{"sessionId":"%s","cwd":"%s"}\n' % (session_id, c) for c in cwds
        ) or '{"sessionId":"%s"}\n' % session_id)
        t = __import__("time").time() - age_s
        os.utime(f, (t, t))
        return f

    def _sessions(self, *pairs):
        from rimagentd.fold import Session
        return [Session(backend_id=f"{i+1}.{n}", name=n, cwd=c)
                for i, (n, c) in enumerate(pairs)]

    def test_sole_active_transcript_is_high_confidence(self):
        from rimagentd.binding import suggest
        self._mk("/t/a", "sid-a", cwds=("/t/a",))
        res = suggest(self._sessions(("a", "/t/a")), self.projects)
        chosen = res["1.a"]["chosen"]
        self.assertEqual(chosen.session_id, "sid-a")
        self.assertEqual(chosen.confidence, "high")

    def test_stale_transcript_is_not_high_confidence(self):
        """A conversation nobody has written to in hours probably is not the one
        a running session is using."""
        from rimagentd.binding import suggest
        self._mk("/t/a", "sid-old", age_s=60 * 60 * 24, cwds=("/t/a",))
        res = suggest(self._sessions(("a", "/t/a")), self.projects)
        self.assertNotEqual(res["1.a"]["chosen"].confidence, "high")

    def test_newest_wins_when_a_directory_has_several(self):
        from rimagentd.binding import suggest
        self._mk("/t/a", "sid-old", age_s=600, cwds=("/t/a",))
        self._mk("/t/a", "sid-new", age_s=30, cwds=("/t/a",))
        self.assertEqual(suggest(self._sessions(("a", "/t/a")), self.projects)["1.a"]
                         ["chosen"].session_id, "sid-new")

    def test_renamed_leaf_is_found_via_the_parent_but_only_at_medium(self):
        """The case no cwd derivation reaches: the conversation is filed under
        the OLD leaf name and nothing on the host remembers it. The parent path
        is intact, so it is still positional evidence — but weaker, so it must
        not be reported as high."""
        from rimagentd.binding import suggest
        self._mk("/srv/deep/tree/here/oldname", "sid-renamed", cwds=("/x",))
        res = suggest(self._sessions(("s", "/srv/deep/tree/here/newname")), self.projects)
        chosen = res["1.s"]["chosen"]
        self.assertEqual(chosen.session_id, "sid-renamed")
        self.assertEqual(chosen.confidence, "medium")

    def test_shallow_parent_does_not_match_the_whole_tree(self):
        """Guard on the fallback: a shallow parent would match nearly every
        directory, so it must not be used at all."""
        from rimagentd.binding import suggest
        self._mk("/a/b", "sid-x")
        res = suggest(self._sessions(("s", "/a/zz")), self.projects)
        self.assertIsNone(res["1.s"]["chosen"])

    def test_one_conversation_is_never_offered_to_two_sessions(self):
        from rimagentd.binding import suggest
        self._mk("/srv/deep/tree/here/only", "sid-one", cwds=("/srv/deep/tree/here/only",))
        res = suggest(self._sessions(("owner", "/srv/deep/tree/here/only"),
                                     ("other", "/srv/deep/tree/here/gone")), self.projects)
        # The session whose own directory it is has the stronger evidence and
        # takes it; the other must be left unresolved, not given a wrong answer.
        self.assertEqual(res["1.owner"]["chosen"].session_id, "sid-one")
        self.assertIsNone(res["2.other"]["chosen"])

    def test_already_bound_sessions_are_left_alone(self):
        from rimagentd.binding import suggest
        from rimagentd.fold import Session
        s = Session(backend_id="1.a", name="a", cwd="/t/a", agent_session_id="already")
        self.assertEqual(suggest([s], self.projects), {})

    def test_underscore_and_slash_both_collapse_in_the_directory_name(self):
        from rimagentd.binding import encode_cwd
        self.assertEqual(encode_cwd("/a/b_c/d"), "-a-b-c-d")


class TestEventSessionDerivation(unittest.TestCase):
    """`session` on a defer/escalate is what makes the item returnable later.

    Eleven of the first twelve defer/escalate events in the live log had no session,
    because every caller had to remember to pass one -- so an item could be parked with
    no record of who parked it, and nothing could hand it back. The CLI now derives it
    from $STY. These tests pin all four behaviours, including the two that are easy to
    break: explicit must win, and a missing raiser must be noisy rather than silent.

    Driven through the CLI as a subprocess, because that is where the derivation lives --
    deliberately not in EventLog.append(), whose internal callers set `session` to mean
    the SUBJECT of the event rather than its writer.
    """

    CLI = str(Path(__file__).resolve().parent.parent / "scoot-rim-agentd")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = str(Path(self.dir.name) / "events.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, args, sty=None):
        env = dict(os.environ)
        env.pop("STY", None)
        if sty:
            env["STY"] = sty
        return subprocess.run([sys.executable, self.CLI, "--log", self.log, "event", *args],
                              capture_output=True, text=True, env=env)

    def test_session_derived_from_sty(self):
        r = self._run(["defer", "item", "x", "reason", "y"], sty="12345.PTD")
        self.assertEqual(json.loads(r.stdout)["session"], "PTD")

    def test_explicit_session_beats_sty(self):
        """A supervisor writing on another session's behalf must not be overwritten."""
        r = self._run(["defer", "item", "x", "reason", "y", "session", "pat"], sty="12345.PTD")
        self.assertEqual(json.loads(r.stdout)["session"], "pat")

    def test_missing_raiser_warns(self):
        r = self._run(["defer", "item", "x", "reason", "y"])
        self.assertNotIn("session", json.loads(r.stdout))
        self.assertIn("cannot be handed back", r.stderr)

    def test_non_raiser_event_does_not_warn(self):
        r = self._run(["release", "resource", "foo"])
        self.assertNotIn("cannot be handed back", r.stderr)

    def test_sty_without_a_name_is_not_guessed(self):
        """A malformed $STY must leave session absent rather than invent one."""
        r = self._run(["defer", "item", "x", "reason", "y"], sty="nodot")
        self.assertNotIn("session", json.loads(r.stdout))


class TestSymlinkedCwd(unittest.TestCase):
    """A session launched through a symlinked path.

    Seen live: /srv/bregman is a symlink to /srv/steveai/bregman, and four
    sessions ran with a cwd under the symlink while their conversations were
    filed under the resolved path. The binder derived the project directory from
    the literal cwd, found nothing, and left agent_session_id empty — silently,
    with no error anywhere. Downstream, a display joining on that id drew no
    live row for those four.

    These tests pin both directions, because which spelling the runtime files
    under is the runtime's business and could change.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        # real/deep/tree  <-  link  (mirrors /srv/bregman -> /srv/steveai/bregman)
        self.real = self.root / "real" / "deep" / "tree"
        self.real.mkdir(parents=True)
        self.link = self.root / "link"
        self.link.symlink_to(self.root / "real")

    def tearDown(self):
        self.dir.cleanup()

    def _mk(self, cwd, session_id, age_s=60, cwds=()):
        from rimagentd.binding import encode_cwd
        d = self.projects / encode_cwd(str(cwd))
        d.mkdir(exist_ok=True)
        f = d / f"{session_id}.jsonl"
        f.write_text("".join(
            '{"sessionId":"%s","cwd":"%s"}\n' % (session_id, c) for c in cwds
        ) or '{"sessionId":"%s"}\n' % session_id)
        t = __import__("time").time() - age_s
        os.utime(f, (t, t))
        return f

    def test_cwd_variants_adds_resolved_form_literal_first(self):
        from rimagentd.binding import cwd_variants
        v = cwd_variants(str(self.link / "deep" / "tree"))
        self.assertEqual(len(v), 2)
        self.assertTrue(v[0].startswith(str(self.link)), "literal must stay first")
        self.assertEqual(v[1], str(self.real))

    def test_cwd_variants_is_a_noop_without_a_symlink(self):
        from rimagentd.binding import cwd_variants
        self.assertEqual(cwd_variants(str(self.real)), [str(self.real)])
        self.assertEqual(cwd_variants(""), [""])

    def test_transcript_filed_under_resolved_path_binds_from_symlinked_cwd(self):
        """The live failure. Session cwd is the symlink; conversation is filed
        under the resolved path."""
        from rimagentd.binding import candidates_for
        self._mk(self.real, "sid-real", age_s=5)
        got = candidates_for(str(self.link / "deep" / "tree"), projects=self.projects)
        self.assertEqual([c.session_id for c in got], ["sid-real"])

    def test_transcript_filed_under_symlinked_path_still_binds(self):
        """The other direction, so the fix does not depend on which spelling the
        runtime happens to use."""
        from rimagentd.binding import candidates_for
        link_cwd = str(self.link / "deep" / "tree")
        self._mk(link_cwd, "sid-link", age_s=5)
        got = candidates_for(link_cwd, projects=self.projects)
        self.assertEqual([c.session_id for c in got], ["sid-link"])

    def test_no_duplicate_candidate_when_both_spellings_resolve(self):
        """Literal and resolved can encode to two names for one directory; the
        same conversation must not be offered twice."""
        from rimagentd.binding import candidates_for
        self._mk(self.real, "sid-real", age_s=5)
        self._mk(self.link / "deep" / "tree", "sid-real", age_s=5)
        got = candidates_for(str(self.link / "deep" / "tree"), projects=self.projects)
        self.assertEqual(len([c for c in got if c.session_id == "sid-real"]), 1,
                         "one conversation, one candidate")


class TestRebootRecovery(unittest.TestCase):
    """The reboot path, which is the reason this log exists at all.

    After a reboot every session's backend is gone. Treating that as "the
    session ended" and closing them empties the recovery set — and because the
    reconcile timer is Persistent=true it fires AT boot, so it would do this
    before anything could restore. Verified against the real log: one pass
    closed all twelve.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = EventLog(Path(self.dir.name) / "events.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_session_from_a_previous_boot_is_pending_not_running(self):
        self.log.append("session_start", backend_id="1.a", name="a", cwd="/x",
                        boot_id="an-older-boot")
        state = fold(self.log.records())
        self.assertEqual([s.name for s in state.pending], ["a"])
        self.assertEqual(state.running, [])

    def test_session_from_this_boot_is_running_not_pending(self):
        self.log.append("session_start", backend_id="1.a", name="a", cwd="/x")
        state = fold(self.log.records())
        self.assertEqual([s.name for s in state.running], ["a"])
        self.assertEqual(state.pending, [])

    def test_boot_id_is_stamped_on_every_event(self):
        """Without it there is no way to tell an ended session from a rebooted
        host, so it must not be optional."""
        from rimagentd.events import current_boot_id
        self.log.append("session_start", backend_id="1.a", name="a")
        self.assertEqual(next(iter(self.log.records()))["boot_id"], current_boot_id())

    def test_missing_boot_id_falls_back_to_start_time_vs_boot_time(self):
        """Records written before boot_id existed must still classify. A start
        that predates the current boot cannot still be running."""
        self.log.append("session_start", backend_id="1.old", name="old", cwd="/x")
        rewritten = []
        for rec in self.log.records():
            rec.pop("boot_id", None)
            rec["ts"] = "2001-01-01T00:00:00+00:00"
            rewritten.append(json.dumps(rec))
        self.log.path.write_text("\n".join(rewritten) + "\n")
        self.assertEqual([s.name for s in fold(self.log.records()).pending], ["old"])

    def test_unparseable_timestamp_does_not_classify_as_pending(self):
        """Guessing here would either discard a live session or resurrect a dead
        one, so an unreadable timestamp must fall through to 'not pending'."""
        self.log.append("session_start", backend_id="1.a", name="a", cwd="/x")
        rewritten = []
        for rec in self.log.records():
            rec.pop("boot_id", None)
            rec["ts"] = "not-a-timestamp"
            rewritten.append(json.dumps(rec))
        self.log.path.write_text("\n".join(rewritten) + "\n")
        self.assertEqual(fold(self.log.records()).pending, [])


class TestCwdCorrection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = EventLog(Path(self.dir.name) / "events.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_cwd_correction_replaces_recorded_position(self):
        """A renamed directory leaves the recorded position pointing at nothing,
        which makes restore skip the session."""
        self.log.append("session_start", backend_id="1.a", name="a", cwd="/old")
        self.log.append("cwd_corrected", backend_id="1.a", cwd="/new")
        self.assertEqual(fold(self.log.records()).live["1.a"].cwd, "/new")

    def test_correction_for_an_unknown_session_is_ignored(self):
        self.log.append("cwd_corrected", backend_id="9.ghost", cwd="/new")
        self.assertEqual(fold(self.log.records()).live, {})

    def test_correction_without_a_cwd_does_not_blank_the_position(self):
        self.log.append("session_start", backend_id="1.a", name="a", cwd="/old")
        self.log.append("cwd_corrected", backend_id="1.a")
        self.assertEqual(fold(self.log.records()).live["1.a"].cwd, "/old")


class TestPlayersOnDemand(unittest.TestCase):
    """Players on demand (2026-11-25): after a reboot only captains come back;
    players stay held, bound and resumable until their captain starts them."""

    CLI = str(Path(__file__).resolve().parent.parent / "scoot-rim-agentd")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.logp = d / "events.jsonl"
        self.log = EventLog(self.logp)
        self.cwd = d / "work"; self.cwd.mkdir()
        self.priv_cwd = d / "private"; self.priv_cwd.mkdir()
        (d / "roster.toml").write_text(
            '[[captain]]\nname = "Zeta"\nplayers = ["zplayer"]\n')
        (d / "private.txt").write_text(f"{self.priv_cwd}\n")
        self.env = {**os.environ, "SCOOT_RIM_ROSTER": str(d / "roster.toml"),
                    "SCOOT_RIM_PRIVATE": str(d / "private.txt")}

    def tearDown(self):
        self.dir.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, self.CLI, "--log", str(self.logp), *args],
                              capture_output=True, text=True, env=self.env)

    def start(self, bid, name, cwd, bound=True, boot="an-older-boot"):
        self.log.append("session_start", backend_id=bid, name=name, cwd=str(cwd), boot_id=boot)
        if bound:
            self.log.append("agent_bound", backend_id=bid, agent_session_id=f"id-{name}",
                            cwd=str(cwd), boot_id=boot)

    def test_dormant_event_marks_and_new_start_clears(self):
        self.start("1.p", "p", self.cwd, boot=None or "x")
        self.log.append("dormant", backend_id="1.p")
        self.assertTrue(fold(self.log.records()).live["1.p"].dormant)
        self.log.append("session_start", backend_id="1.p", name="p", cwd=str(self.cwd))
        self.assertFalse(fold(self.log.records()).live["1.p"].dormant)

    def test_only_captains_restores_captains_holds_players_hides_private(self):
        self.start("901.[Captain]_Zeta", "[Captain]_Zeta", self.cwd)
        self.start("902.zplayer", "zplayer", self.cwd)
        self.start("903.secretproj", "secretproj", self.priv_cwd)
        out = self.cli("restore", "--dry-run", "--only-captains").stdout
        self.assertIn("WOULD [Captain]_Zeta: claude --resume id-[Captain]_Zeta", out)
        self.assertIn("HOLD  zplayer — player", out)
        self.assertNotIn("secretproj", out)          # private: never listed

    def test_unbound_session_is_skipped_alone_not_a_refusal(self):
        self.start("911.[Captain]_Zeta", "[Captain]_Zeta", self.cwd)
        self.start("912.loose", "loose", self.cwd, bound=False)
        r = self.cli("restore", "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("refusing", r.stdout)
        self.assertIn("WOULD [Captain]_Zeta", r.stdout)
        self.assertIn("SKIP  loose — UNBOUND", r.stdout)

    def test_reconcile_holds_a_dormant_record_from_this_boot(self):
        from rimagentd.events import current_boot_id
        self.start("921.zplayer", "zplayer", self.cwd, boot=current_boot_id())
        self.log.append("dormant", backend_id="921.zplayer")
        self.cli("reconcile", "--quiet")
        self.assertIn("921.zplayer", fold(self.log.records()).live)

    def test_live_json_reports_state_and_captain(self):
        self.start("931.zplayer", "zplayer", self.cwd)
        self.start("932.loose", "loose", self.cwd, bound=False)
        rows = {r["name"]: r for r in json.loads(self.cli("live", "--json").stdout)}
        self.assertEqual(rows["zplayer"]["state"], "dormant")
        self.assertEqual(rows["zplayer"]["captain"], "[Captain]_Zeta")
        self.assertEqual(rows["loose"]["state"], "broken")
        self.assertIsNone(rows["loose"]["captain"])
