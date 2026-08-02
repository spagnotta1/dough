# Disaster Recovery Runbook

Last drilled: **2026-07-29**, end-to-end, all checks passing (log at the
bottom). This document is written so that someone who is not the author can
execute it under pressure: every step is a command, every command says what
it must print, and everything that can be verified by a tool is.

## What must exist for recovery to be possible

A restorable installation is **four files**, not one:

| File | What it is | If it is lost |
|---|---|---|
| `checkbook.db` (or a `backups/` copy) | every household's data | unrecoverable — this is the reason backups exist |
| `.sync_encryption_key` | Fernet key for stored bank/Plaid tokens | data survives, but **every financial connection is dead**: `auth_blob` values cannot be decrypted, and each institution must be re-linked by hand |
| `.flask_secret_key` (dev) / `SECRET_KEY` env (prod) | session-signing key | data survives; every user is signed out once and signs back in |
| `.env` | provider credentials (Plaid, Anthropic, mail), settings | data survives; AI and live sync are down until the values are re-entered |

> **The drill's most important finding:** `tools/backup_db.py` backs up the
> *database only*. The three key files are small, change rarely, and are not
> in git (correctly — they are secrets). They must be captured by whatever
> backs up the host, and a restore that forgets `.sync_encryption_key`
> looks healthy right up until the first sync. Step 8 below is the check
> that catches it.

Backups written to the local `backups/` directory protect against a bad
migration or a fat-fingered delete, **not** against losing the machine.
Copy them somewhere that is not this disk.

## Taking a backup (routine)

**Since Phase 10.6 this happens by itself.** The serving process runs a backup
thread (`dough/services/backup.py`): one snapshot a minute after startup, then
every `BACKUP_INTERVAL_HOURS` (default 24), keeping `BACKUP_KEEP` (default 7),
written beside the database file — `/data/backups` in production. Watch it with
`railway logs | grep dough.backup`; a failure logs `Backup failed` and the loop
retries at the next interval rather than dying.

That changes what this runbook is for. The routine case is covered; what is
below is the recovery, and the manual command remains the right thing to run
before anything risky:

```
python tools/backup_db.py                      # -> backups/checkbook-<stamp>.db
python tools/backup_db.py --label pre-upgrade  # before anything risky
```

The tool uses SQLite's online backup API (safe while the app runs), then
verifies `PRAGMA integrity_check` and per-table row counts against the
source, and **deletes the copy if either fails**. Expected output ends:

```
   verified: integrity_check ok, 19 tables, NNNN rows
```

A backup that was never verified is a hope, not a backup:

```
python tools/backup_db.py --verify-only backups/checkbook-<stamp>.db
```

### What the test suite checks on every commit

The drill below is the human procedure and still has to be run by hand,
because most of what it proves involves real key files and a real deployment.
Two links in the chain are now machine-checked, in `tests/test_backup.py`:

- `test_a_backup_restores_after_the_database_is_destroyed` overwrites the live
  file with zeroes, copies the snapshot back, and asserts the rows are there.
  It corrupts rather than deletes, because that is the failure that actually
  happens.
- `test_a_restored_database_still_runs_the_application` boots a real app
  against the restored file and requires `/health/ready` to answer 200 — a
  database can pass `integrity_check` and still be at a schema revision the
  code cannot serve.

Neither replaces the drill. They mean a regression in the restore *mechanism*
fails a test rather than waiting for the next incident.

## The recovery procedure

Work in a scratch directory first. Nothing touches the live paths until
step 9.

### 1. Capture what "before" looked like (if the old DB is still readable)

```
python tools/verify_tenancy.py checkbook.db --emit-baseline baseline.json
```

If the live file is unreadable, use the newest verified backup for the
baseline instead — the point is to have row counts to compare the restore
against.

### 2. Restore the backup to a fresh location

```
mkdir restored
cp backups/checkbook-<stamp>.db  restored/checkbook.db
cp .sync_encryption_key .flask_secret_key  restored/     # from host backup
```

### 3. Verify the copy is intact

```
python tools/backup_db.py --verify-only restored/checkbook.db
```

Must print `integrity_check ok`.

### 4. Bring the schema to head

```
DATABASE_URL=sqlite:///$PWD/restored/checkbook.db python -m flask --app app db upgrade
```

On a backup taken at the current release this is a no-op (Alembic prints
its two context lines and exits 0). After restoring an *older* backup it
replays the missing migrations — which is exactly when step 5 matters most.

### 5. Verify tenancy and row counts

```
python tools/verify_tenancy.py restored/checkbook.db --baseline baseline.json
```

Must end `All 153 tenancy invariants hold.` This is the check that catches
a batch-migration rebuild that silently lost rows (right schema, missing
data), orphaned households, cross-household FK mismatches, and leftover
`_alembic_tmp_*` tables.

### 6–9. Verify every service against the restored data

```
python tools/dr_drill.py restored
```

Expected output, all eight `[ok]`:

```
  [ok  ] health/live                             200 {"status":"ok"}
  [ok  ] health/ready                            200 {"checks":{"configuration":true,"database":true,"migrations":true},"status":"ok"}
  [ok  ] login page renders                      GET /login -> 200
  [ok  ] restored password hash verifies         wrong password rejected with the generic message: True
  [ok  ] audit logging records on restored DB    N -> N+1 rows, last=('auth.login.failed', None)
  [ok  ] stored connection credentials decrypt   4/4 connections decrypt; failures: []
  [ok  ] AI adapter constructed                  service=AIService, adapter=AnthropicAdapter, ...
  [ok  ] scheduler honours SYNC_AUTO_ENABLED=0   SYNC_AUTO_ENABLED=False (OPS-0012 single-worker decision)
```

What each line proves is documented in the tool's own docstring. Two notes:

* **No live password is needed.** Auth is verified by submitting a *wrong*
  password through the real form and requiring the generic rejection; audit
  is verified by that same attempt landing in `audit_events`. The
  `household_id: None` on that row is by design (a failed login belongs to
  no household).
* The probe **writes one audit row to the restored copy**. If a pristine
  file matters, re-run step 2 before promoting.

### 10. Promote

Stop the application, then:

```
python tools/backup_db.py --label pre-promote        # the OLD live file, if readable
cp restored/checkbook.db  checkbook.db
```

Start the application (one worker — OPS-0012) and confirm
`GET /health/ready` returns 200. Have a user sign in and open the
dashboard, transactions, and connections pages.

## Specific failure modes

**A migration failed halfway.** SQLite batch migrations rebuild tables via
create-temp/copy/drop/rename; dying mid-way leaves a `_alembic_tmp_*` table
and possibly a half-populated real one. Do not try to repair in place:
restore the newest verified backup (procedure above) and re-run the
migration. `verify_tenancy.py` explicitly checks for abandoned temp tables.

**The live database is corrupt.** `PRAGMA integrity_check` says so, or the
app 500s on every query. Restore the newest verified backup. Anything
written between that backup and the corruption is lost — which is the
argument for running `tools/backup_db.py` on a schedule rather than by hand.

**A credential was compromised.**
* `SECRET_KEY` / `.flask_secret_key`: generate a new value, restart. All
  sessions are invalidated (this is the *feature*); no data is affected.
* `.sync_encryption_key`: there is no rotation path yet (Phase 10 work).
  Today: disconnect every institution, replace the key, re-link each one.
  The old blobs are unreadable garbage to the new key, which is exactly
  what you want if the key leaked.
* Provider keys in `.env` (Plaid, Anthropic): revoke at the provider,
  replace the value, restart.

## Drill log

| Date | Backup | Restore | Upgrade | Tenancy | Services | Notes |
|---|---|---|---|---|---|---|
| 2026-07-29 | ok — 19 tables, 1,337 rows, verified | ok | ok (no-op at `20260727_04_audit`) | 153/153 | 8/8 | First full drill. Findings: (1) key files are outside `backup_db.py`'s coverage — documented above; (2) audit event name is `auth.login.failed` (dots); (3) CSRF token round-trip doubles as a restored-SECRET_KEY check. Drill scripts promoted to `tools/dr_drill.py`. |
