# Release notes

## First public release (November 2026)

This is the first public cut of MeshyChat. It is the code that runs in daily use, with organisation-specific
names, hosts, addresses and internal documents removed or replaced by neutral placeholders.

### Known gaps

These are known and are not yet fixed. Issues and pull requests are welcome.

- **Placeholder text in the docs.** Many documents under `docs/` were written for the original deployment. Where
  they named people, machines or internal systems, those names are replaced by neutral placeholders
  (`sampleco`, `example.com`, private test addresses such as `192.168.50.10`, home folders such as `C:/Users/user`), so some passages read awkwardly or refer to setups you
  will not have.
- **Environment variables are not all documented.** The settings module reads more `MESHYCHAT_*` variables than
  the README describes. Until they are, `meshychat/settings.py` is the reference.
- **Two tool defaults name the placeholder owner.** A small number of command-line defaults still point at the
  placeholder GitHub owner `sampleco`; pass your own value.
- **Not wire-compatible with the original deployment.** Project-specific identifiers were replaced throughout
  this cut: database roles and tables, migration file names, environment variables, the protocol id, header
  names, token prefixes and the signing domains. A node built from this tree does not interoperate with the
  deployment the code came from, and a database created by these migrations is its own lineage. The
  known-answer tests pin the bytes this tree produces. Each environment variable has one name here: a variable
  set under any other spelling is ignored, not refused.
- **The bundled word list.** The invite-code word list is derived from the EFF large word list and is not
  byte-identical to it; see `NOTICE` for its licence (CC BY 4.0).
- **The test suite.** The full suite is large (about 12,200 tests, PostgreSQL 16 required). Tests that cannot
  hold in this cut are deselected by `tests/conftest.py` from the list in `tests/public_cut_deselect.txt`, which
  gives the reason for each group. There are three kinds: tests that check internal-only material this cut
  leaves out; one test that needs a commit of the original history, which a one-commit tree does not have; and
  four tests of the migration that renames older catalog names (`tests/test_p2_meshychat_names.py`, T1 to T4),
  which are known failures of this cut. T1 fails because its old-name pattern is this cut's own product word.
  T2, T3 and T4 fail at one shared comparison of function grants; the same word mapping is the likely cause,
  but that is not proven, so what those three check (grant equality and row-level security, two migrations at
  once, down then up) is not verified in this cut. The full
  suite has not been run on this exact commit. A full run on a tree a few small edits before it, in three
  parallel shards, had 13 failures of 12,168: those four migration tests, now deselected; three that need a
  database role (`meshychat_owner`) which that run's fresh database did not have; one that runs the test
  runner against a committed tree; and five that are timing-sensitive under parallel load (four passed when run
  alone, one fails about one time in ten).

### Not included

- **The hosted task-board adapter.** The original deployment connects nodes to a hosted task board (open items,
  work claims, a ranked queue). That adapter talks to one organisation's private service, so
  `tools/meshy_node/board_{tasks,queue,claim,dispatch}.py` are stubs here: the client still builds and runs, and
  every board command reports that the board is not available. Replace the four modules with an adapter for your
  own board if you want that feature. MeshyChat's own board view (`apps/board`, derived from declared acts) is
  included.
- Most deployment scripts for the original hosts, internal design and review documents, and anything that only made
  sense inside the original organisation. Most internal ticket numbers, machine names and decision references in
  code comments are replaced by neutral placeholders (`TKT-1234`, host names such as `HQB` or `HZ`, "a recorded
  decision"). Not all: some numbered issue and design-rule references remain where a dated incident is part of
  the explanation (for example in `docs/V1-DESIGN-RULES.md` and in comments of the SQL migrations); those
  numbers point at a private tracker. Names of people and agents are placeholders of the same length (`kxxx`), and message keys,
  peer ids and other record ids in examples and fixtures are replaced by made-up values.
