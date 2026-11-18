# Contributing

Thank you for looking at MeshyChat. This is an early source release of a working system; expect rough edges.

## Before you open a pull request

1. Use CPython 3.13 and PostgreSQL 16 (see the Quick start in [README.md](README.md)).
2. Install what the repository declares:
   `pip install -r requirements.txt -r requirements-dev.txt -c requirements.lock.txt`
3. Run the tests that cover your change. The suite needs a PostgreSQL server it may create a database on:
   `MESHY_TEMPLATE_DSN=postgresql://USER@HOST:5432/postgres python -m pytest tests/ -q`
   The full suite takes a long time; the CI workflow (`.github/workflows/fast-checks.yml`) runs a small subset on
   every push and pull request. Say in the pull request which test files you ran and which you did not.

## Rules of the code base

- **The schema is plain SQL.** Migrations live in `sql/` and `sql_meshychat/`, are applied verbatim and are
  checksummed by `apps/core/sqlmigrate.py`. Never edit a migration that has been released: add a new numbered
  file. Do not convert them to Django migrations.
- **A fix comes with a test that fails without it.** Say so in the pull request.
- **Keep a change small and say what it does not cover.** One concern per pull request.

## Reading the comments

Comments and tests cite numbered "divergences" and use placeholder names for the people and agents who built the
system. Both are explained in the README, section "Design-log references".

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
