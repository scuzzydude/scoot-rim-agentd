# scoot-rim-agentd

Session integration for multi-agent work. Part of the `scoot` family
(`scoot`, `scoot-chat`, `scoot-pmp`, `scoot-win-term`).

When several coding-agent sessions work one tree at once, the information that
determines whether a write is safe **is present** — another session is mid-edit,
a rule was superseded an hour ago, a decision is pending a human — and it is
destroyed at every hand-off, because a hand-off between sessions is a message
with no shared state behind it.

This keeps that state in one place: an append-only event log, a fold over it,
and a positional ownership map.

**Generic by construction.** The engine knows only *"a tree of nodes, each with
an owner and a policy"*. Which directory maps to which owner is site
configuration supplied via `$SCOOT_RIM_OWNERSHIP` and **never committed here**,
so the same code runs on unrelated hosts with no content leaking between them.

Python 3.11+ (`tomllib`), standard library only, no dependencies.

## What problem this actually solves

Observed, not hypothetical — from a day of four sessions on one shared file:

- **`git status` reports clean while another session is mid-edit.** It reflects
  committed state only. Every session checked before writing, checked
  correctly, and still collided. Two collisions stayed clean purely by where
  the edits landed.
- **Rules written in content files don't load.** Conventions in markdown inside
  the work tree are read only when a session is told to read them.
- **Deferred work is lost on context turnover.** "Come back to it later"
  survives only if it is written somewhere that outlives the conversation.
- **A snapshot cannot answer "what was running before the reboot".** An ended
  session is simply absent from it.

## Design

| Concern | Where |
|---|---|
| Truth | `~/.scoot-rim/events.jsonl` — append-only, never rewritten |
| Current state | A fold over the log. No cached snapshot to fall out of sync. |
| Ownership | Site config (`$SCOOT_RIM_OWNERSHIP`), positional, not by name |

Two rules that are load-bearing rather than stylistic:

**Ownership is positional.** The owner of a file is the session whose working
directory contains it. Never a session *name* — names change across restarts,
directories don't.

**Ownership governs who writes, never who is right.** Nothing here ranks
technical judgment. It serialises writes to contended files and stops there.
On the day this came from, three times a session with no standing overruled the
designated owner correctly; a hierarchy empowered to settle technical questions
would have made all three outcomes worse.

**Non-blocking by construction.** Contended resources have one writer. Others
don't wait — they hand off a payload and keep working, and the owner applies
queued changes serially. Four sessions produced eight substantial artifacts
against one contended file with zero idle time this way; a lock would have
serialised the authoring too, which is the expensive part and the part that is
safe to parallelise.

## Usage

```bash
./scoot-rim-agentd live            # sessions started and not exited — the recovery set
./scoot-rim-agentd tree            # the same set grouped by position, annotated with policy
./scoot-rim-agentd doctor          # what would make this log untrustworthy to act on
./scoot-rim-agentd restore-plan    # what a restore WOULD run; prints only, never acts
./scoot-rim-agentd open            # deferred/escalated with no resume/decide
./scoot-rim-agentd claims          # outstanding resource claims
./scoot-rim-agentd tail 20

# ownership (needs $SCOOT_RIM_OWNERSHIP)
./scoot-rim-agentd owner  path/to/file
./scoot-rim-agentd check  path/to/file --cwd /where/the/session/is
```

`tree` groups by *position* rather than listing by name, because position is
what determines ownership, policy and restore everywhere else in this design:

```
$ scoot-rim-agentd tree
/tree
  * root-session  (strict)
  components/
    a/
      * alice  (own-subtree)
  shared/
    * dash  (handoff)  [UNBOUND]
```

`check` exits 0 for allow, 1 for deny — it is the decision a pre-write gate
makes, exposed as a plain command so the policy can be reviewed and tested
*before* anything enforces it.

```
$ scoot-rim-agentd check shared/dashboard.html --cwd components/a
DENY  shared/dashboard.html
       shared is handoff; caller is at components/a. Hand the change to the
       session working in shared rather than writing it here.
       owner cwd: /tree/shared
```

### Ownership map

See `examples/ownership.toml`. Three policies:

| Policy | Meaning |
|---|---|
| `own-subtree` | Free to write anything beneath itself. No coordination. |
| `handoff` | Contended. One writer; others hand off a payload. |
| `strict` | Gated. Writes *and* side effects, where a collision costs more than recoverable text. |

A node listing `owns` claims only those globs; a node without `owns` claims its
whole subtree. Most specific wins, and naming a file beats containing it.

## Relationship to `scoot-win-term`

`scoot-win-term` writes the log today — it is already in the interactive shell
path and needs no service. This reads it, and grows into the daemon.

The schema is compatible in both directions and verified so: events written by
the shell layer are read here, and vice versa. Nothing about `sn`/`sls`
changes.

## Status

Early. What works: the event log, the fold, ownership resolution, and the
query CLI, with 24 tests.

Not built yet: the daemon itself (service + timer reconciliation), the pre-write
gate that would call `check`, an HTTP API, and clients. `restore-plan` prints
commands and deliberately does not run them — acting belongs in the daemon,
where it can be gated and logged as an action rather than triggered by accident
from a shell.

```bash
python3 -m unittest discover -s tests
```
