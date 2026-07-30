# ADR-0007: Alembic as the sole schema authority

- **Status:** Accepted
- **Date:** 2026-07-26
- **Phase:** 2

## Context

This project had two schema authorities, and neither one knew it.

The first was the Alembic chain in `migrations/versions/` — six revisions ending
at `b1c2d3e4f5a6`. The second was a ~90-line block at the end of `create_app()`
that ran on every boot: a raw `CREATE TABLE IF NOT EXISTS holdings`, thirteen
`ALTER TABLE ... ADD COLUMN` statements each wrapped in its own bare
`try/except: pass`, a hand-written `connected_accounts` rebuild via
`RENAME TO connected_accounts_old`, a `db.create_all()`, and two
`CREATE UNIQUE INDEX IF NOT EXISTS`.

Every line of that block was added for a defensible local reason: a column was
needed, the migration workflow was not working, and an idempotent `ALTER` at boot
shipped the feature. Collectively they replaced the migration chain.

Three specific consequences were found by measuring the live database rather
than reading the code:

1. **`flask db upgrade` had never run in this repository.** `migrations/env.py`
   called `fileConfig(config.config_file_name)` on `migrations/alembic.ini`, a
   file that does not exist here (the ini lives at the repo root, for the
   `alembic` CLI). `fileConfig` on a missing file raises
   `KeyError: 'formatters'`, and it happened at env.py *import* time — so the
   command died before selecting a revision. This is the root cause of
   everything else: the chain was unrunnable, so the bootstrap had to be the
   authority.
2. **The live database carried 27 dangling foreign keys** pointing at
   `connected_accounts_old`, a table that no longer existed.
   `ALTER TABLE ... RENAME TO` in SQLite rewrites foreign-key references *in
   other tables* to follow the rename. The bootstrap's rebuild renamed the
   original out of the way, and five other tables silently repointed at the
   corpse. `PRAGMA foreign_key_check` reported 27 violations.
3. **The live schema had drifted from `models.py` in six ways a brand-new
   install was also getting**, because `db.create_all()` never modifies an
   existing table and the bootstrap's raw `ALTER`s produced columns without the
   foreign keys and indexes that `models.py` declares.

`downgrade()` also has to be decided here, because the reconciliation revision
cannot have a meaningful inverse.

## Decision

**The Alembic chain is the only thing permitted to change the schema.** The
inline bootstrap is deleted, not reduced.

1. **`migrations/env.py` is fixed** so the chain is runnable at all: logging
   configuration is best-effort (`_configure_logging()` checks the file exists
   and has a `[formatters]` section before calling `fileConfig`), and
   `render_as_batch` is set — via `setdefault`, because Flask-Migrate already
   injects the key and passing it explicitly raises "got multiple values for
   keyword argument". Logging setup is not worth failing a migration over.
2. **The live database is adopted with a one-off
   `flask db stamp b1c2d3e4f5a6`.** Stamping asserts "the schema this revision
   produces is already present," which is true and checkable: `b1c2d3e4f5a6`'s
   only job is `create_table('holdings')`, and `holdings` exists. Never
   `stamp head` — that would assert the reconciliation revision had also run.
3. **Revision `20260726_01_reconcile` converges every reachable state onto one
   schema.** It is inspector-driven and idempotent: create the ten missing
   tables if absent, add the twelve missing columns if absent, create the
   missing indexes if absent, then rebuild six tables through
   `batch_alter_table(copy_from=...)` to fix nullability, types, defaults and
   foreign keys. It must run correctly against a fresh empty file, against the
   live drifted database, and against a database it has already migrated.
4. **`copy_from` is passed explicitly declared frozen `sa.Table` objects**, not
   reflection. Reflecting the live table would faithfully reproduce the drift —
   including the missing foreign keys — which is the opposite of the point. The
   frozen definitions are pinned in the revision file and never track
   `models.py`, so a future model change cannot retroactively alter what this
   revision does.
5. **`AUTO_UPGRADE_DB` replaces the bootstrap** (`app._upgrade_database`): true
   in development, false in production, where migrations are a deliberate deploy
   step and an app process should not mutate a schema other processes are
   serving.
6. **Tests keep `db.create_all()`**, confined to `TESTING`, with
   `tests/test_migrations.py::test_chain_from_empty_matches_create_all`
   asserting the two produce identical schemas. That is what keeps the shortcut
   from hiding drift the way the bootstrap did.
7. **`20260726_01_reconcile.downgrade()` is a deliberate no-op.** See below.

### Why the downgrade does nothing

A mechanical inverse would have to un-create ten tables and drop twelve columns
containing live data, and it cannot restore what the revision repaired — the
27 broken foreign keys are the thing being fixed, and "restore the corruption"
is not a rollback anyone wants. So the revision drops nothing and the downgrade
is empty.

**Recovery from a bad reconciliation is a backup restore, not a downgrade.**
`tools/backup_db.py` takes a verified online backup (`Connection.backup()`, safe
while the scheduler is running) with per-table row-count verification, deleting
the backup if verification fails, and rotating five copies.

`tests/test_migrations.py::test_reconcile_downgrade_is_a_deliberate_no_op`
asserts the downgrade leaves the schema byte-identical. Its purpose is to make
"does nothing" read as a decision rather than an unfinished stub, and to force
anyone who later writes a real downgrade to come here and say so.

Revision `20260726_02_multitenancy` (Phase 5) is different — it is additive and
reversible, and its downgrade **is** implemented and round-trip tested.

## Consequences

**Good.** One authority. `flask db upgrade` works, so the next schema change is
a normal revision instead of another boot-time `ALTER`. The 27 dangling foreign
keys are gone (`PRAGMA foreign_key_check`: 27 → 0). A fresh install and the
five-year-old live database now produce byte-identical schemas, which is the
precondition for Phase 5 being able to add `household_id` to thirteen tables at
all — an inspector-driven tenancy migration on top of unknown drift would have
been guesswork. `app.py` lost ninety lines of DDL and the boot path no longer
depends on thirteen swallowed exceptions.

**Bad.** Adopting an existing database now requires a manual step. A developer
with a database built by the old bootstrap cannot just pull and run; they must
back up and stamp. `_upgrade_database` detects both such states — stamped
`a1b2c3d4e5f6` with `holdings` already present, and tables present with no
`alembic_version` at all — and raises with the exact commands in the message,
rather than letting `upgrade()` die on "table holdings already exists". That
turns a mysterious crash into an instruction, but it is still a manual step, and
it is tested (`test_upgrade_database_refuses_the_legacy_bootstrap_state`).

Batch mode is also a sharp tool. It rebuilds a table as create-temp / copy /
drop / rename, and **indexes survive only if they are declared in `copy_from`** —
the first validation run of this revision silently dropped ten indexes for
exactly that reason. Anyone writing a future `batch_alter_table` needs to know
this; the frozen table definitions in the revision are the worked example.

**Accepted risk.** The reconciliation revision is long and mostly conditional,
which makes it hard to read and hard to unit-test in pieces. The mitigation is
that it is verified end-to-end against a copy of the real database — schema
convergence from three directions, row counts per table, foreign-key check, and
data spot checks — rather than by inspection. It also only has to be correct
once: after every database in existence has run it, it is history.

The frozen `copy_from` definitions will drift from `models.py` over time, and
that is intended, but it means a reader comparing the two will find differences
that are not bugs.
