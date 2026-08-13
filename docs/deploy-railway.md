# Deploying to Railway

This application is a single always-on process with a SQLite file on a
persistent disk. That is not an accident of how it grew — three of its parts
keep state in the serving process, and the deployment has to preserve that.

An earlier attempt targeted Vercel. It cannot work: serverless functions get a
read-only filesystem outside `/tmp`, `/tmp` is per-instance and wiped on freeze,
and `*.db` is gitignored so the database is not even in the deployed tree.
`pyproject.toml` and `vercel.json` were removed when that path was abandoned;
`requirements.txt` is the single dependency source again.

The order below is the one that works. It is not the order you would guess:
the database has to be uploaded *after* the first deploy, because Railway's
volume file commands need a running deployment to talk to.

## 1. Create the project and service

```bash
npm i -g @railway/cli
railway login
railway init                    # creates the project only
railway add --service dough     # a project has no service until you add one
railway service link dough
```

`railway init` creates a project and nothing else. Until a service exists there
is no Networking tab and no way to generate a domain — which is confusing if you
go looking for one in Project Settings, where it will never be.

The builder detects Python from `requirements.txt`, reads `.python-version` for
the interpreter, and runs the `Procfile`. There is no Dockerfile and none is
needed.

`.python-version` says 3.11 rather than the newest available. That is the
highest version `.github/workflows/ci.yml` actually runs the suite against
(3.10 and 3.11), and the pins in `requirements.txt` were verified there.
Deploying on an interpreter no test has ever run under is a change to make
deliberately, with a CI matrix entry to back it, rather than by taking the
platform default.

## 2. Add the volume

```bash
railway volume add -m /data
```

Run this from PowerShell, not Git Bash: MSYS rewrites a leading `/data` into a
Windows path before the CLI ever sees it, and the CLI rejects it with "mount
path must start with a `/`", which is not a helpful description of what went
wrong.

This is the whole reason for choosing a host with disks. `checkbook.db` lives
here, outside the image, so a deploy replaces the code and leaves the data
alone.

## 3. Generate the domain

```bash
railway variable set "PORT=8080" --skip-deploys
railway domain --port 8080
```

Both sides pinned to the same number, so routing is a fact rather than a
detection. Do this before step 4, which needs the URL.

## 4. Set the environment variables

```powershell
.\tools\set_railway_env.ps1 -DryRun -PublicBaseUrl https://<your-service>.up.railway.app
```

Check the list, then run it again without `-DryRun`. It verifies itself
afterwards and refuses to report success if anything is wrong.

The script exists so that `ENCRYPTION_KEY` is copied from
`.sync_encryption_key` rather than retyped or regenerated. That key decrypts the
Plaid access tokens in `connected_accounts.auth_blob`. Getting it wrong does not
fail at boot — the app starts normally and the stored connections become
permanently unreadable at the next sync, recoverable only by re-linking every
institution.

### Why the values are passed as arguments

`railway variable set --stdin KEY` exists exactly so a secret never appears in a
command line, and it is the obvious thing to reach for. **On CLI 5.30.1 it
corrupts every value it sets**, storing `EF BB BF` — a UTF-8 byte-order mark —
in front of the real one. Measured: a process was fed exactly six bytes with no
BOM and nine came back, so the CLI adds it and no encoding care on the calling
side prevents it.

Nothing reports this. The variable exists, the dashboard renders it correctly,
and the deployment is three bytes wrong. A 45-character Fernet key is not a
valid Fernet key.

Two things make it worse to find:

- `railway variable list --kv` does not reveal it. Only `--json` does.
- PowerShell's `-eq` and `-ceq` compare culture-sensitively, and U+FEFF is a
  zero-weight character in that collation — so a BOM-prefixed key compares
  **equal** to the clean one. Verification has to use
  `[string]::Equals(..., [StringComparison]::Ordinal)` or hash both sides.

So the script uses the argument form and verifies with an Ordinal comparison
afterwards. The values do not reach PowerShell's history — history records the
line you typed, not the arguments the script builds for its children.

### What gets set

| Variable | Value | Why |
|---|---|---|
| `APP_ENV` | `production` | Anything else lets `config.py` generate and *write* key files. Also what turns on `ProductionConfig.validate`. |
| `SECRET_KEY` | generated | Required in production. Generated fresh, not copied — reusing the dev key would let a copy of the working tree forge sessions. |
| `ENCRYPTION_KEY` | from `.sync_encryption_key` | See above. The one value that must not be regenerated. |
| `DATABASE_URL` | `sqlite:////data/checkbook.db` | Four slashes. Three is a *relative* path, which lands the database inside the image. |
| `UPLOAD_FOLDER` | `/data/uploads` | The default is inside the app directory, so uploaded statements would vanish on the next deploy. |
| `PORT` | `8080` | Matches the generated domain's target port. |
| `SYNC_AUTO_ENABLED` | `1` | The background scheduler genuinely works here, which it could not on serverless. |
| `APP_HTTPS` | `1` | Sets `SESSION_COOKIE_SECURE`. |
| `TRUSTED_PROXIES` | `1` | Railway terminates TLS at one proxy. At `0`, `dough/auth.py` ignores `X-Forwarded-For`, so every client looks like the proxy — one shared login-throttle bucket, and audit rows recording the proxy as the actor. |
| `PUBLIC_BASE_URL` | your URL | Otherwise outbound mail links are built from the client-controlled `Host` header. |

`ANTHROPIC_API_KEY`, the `PLAID_*` values and the `MAIL_*` values are forwarded
from your local `.env` if present. Absent ones are skipped rather than set
empty, because `config.py` treats unset as "that feature is off" and empty the
same way, with more noise.

`ALLOW_REGISTRATION` is never forwarded from `.env` — it is set explicitly by
the script, to `0` unless you pass `-AllowRegistration`. Explicitly either way,
because a variable left unset is a state that depends on what a previous run
happened to leave behind, and this one decides whether strangers can sign up.

This deployment runs with it **on**; see "Registration is open" below.

## 5. Deploy from GitHub

```bash
railway service source connect --repo spagnotta1/dough --branch main --service dough
```

This deploys from the pushed commit and pushes again on every future commit to
`main`.

Prefer it to `railway up`, which uploads the working directory. `.env`,
`checkbook.db` and `.sync_encryption_key` are all gitignored and so would be
excluded — but "would be excluded" is a property of the ignore file rather than
of the command, and the blast radius of getting it wrong is every secret this
installation has.

## 6. Move the database across

Only now, because volume file commands report
`Service dough has no active deployment` until something is running.

Expect the first boot to serve 500s with `no such table: app_users`. That is
correct behaviour, not a fault — see "Migrations are manual" below. SQLAlchemy
creates an empty `/data/checkbook.db` on its first connection and nothing
populates it.

Volume file commands go over SSH, so a key has to be registered first:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
railway ssh keys add -k ~/.ssh/id_ed25519.pub -n dough-deploy
```

Then replace the empty database with the real one:

```bash
railway volume files --volume dough-volume upload ./checkbook.db /checkbook.db --overwrite
railway service restart
```

Two CLI shapes worth knowing: `--volume` goes on `volume files`, *before* the
subcommand — after it, the CLI calls it an unexpected argument. And `upload`
refuses an existing path unless given `--overwrite`.

The restart matters. Replacing the file underneath a running process leaves
gunicorn's pooled connections holding the old inode.

Verify rather than assume:

```bash
railway ssh "python -c \"import sqlite3;c=sqlite3.connect('/data/checkbook.db');print(c.execute('select count(*) from transactions').fetchone())\""
```

## Migrations run before the deploy, not inside the app

`ProductionConfig` assigns `AUTO_UPGRADE_DB = False` as a class attribute, so
under `APP_ENV=production` the environment variable is read and then discarded.
Setting `AUTO_UPGRADE_DB=1` looks like it enables boot-time migrations and does
nothing whatsoever. **That has not changed and should not** — two workers racing
the same migration is the hazard `config.py` documents, and a process about to
answer requests should not mutate a schema other processes are serving.

The chain runs from the start command instead, in `Procfile`:

```
web: flask db upgrade && gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT wsgi:app
```

Three properties, and each one is the reason this is not `AUTO_UPGRADE_DB`:

- **It runs before traffic.** `&&` is sequential: the migration finishes before
  gunicorn is executed, so nothing is listening on `$PORT` until the schema is
  current. Railway holds traffic on the previous deployment until the new one
  passes its healthcheck, and a failed migration means gunicorn never starts, the
  healthcheck never passes, and the old version keeps serving. A migration that
  cannot apply is a release that should not go out.
- **It runs once, and nothing races it.** This is a shell step in front of the
  server, not code inside `create_app()`. Gunicorn has not forked when it runs,
  so there is exactly one process regardless of `--workers`. The hazard
  `config.py` documents is N workers each executing the boot-time upgrade; that
  hazard needs N processes and there are none yet.
- **It is outside the application.** `ProductionConfig.AUTO_UPGRADE_DB` is still
  `False` and `create_app()` is unchanged, so nothing about how the app behaves
  when a *person* starts it has moved.

No `FLASK_APP` is needed: Flask's discovery finds `create_app` in `app.py` at the
image's working directory.

### Why not a pre-deploy command

Because it cannot work here, and it fails silently. From Railway's volume
documentation:

> Volumes are not mounted during pre-deploy time, if your pre-deploy command
> attempts to read or write data to a volume, it should be done as part of the
> start command.

The database is on the volume. A pre-deploy `flask db upgrade` therefore creates
an empty SQLite file inside the throwaway pre-deploy container, migrates that,
and exits zero. This was measured, not assumed: the first attempt logged
`Running upgrade  -> 3b62eabfb5e5` and every revision after it — the entire chain
from nothing — while `/data/checkbook.db` sat at head and untouched.

`railway.toml` keeps the pre-deploy command anyway, relabelled as what it
actually is: a check that the chain applies cleanly from an empty database, which
fails the deploy if a revision is broken before the start command reaches the
real data with it.

Running the chain by hand is still the way to migrate without deploying:

```bash
railway ssh --service dough "cd /app && flask db upgrade"
```

### Why this exists

On 2026-08-02, `/rules` and `/goals` returned 500 for every request on
production. The code was fine. Phase 11A and 11B shipped
`20260802_08_category_rules` and `20260802_09_goals`, the application deployed,
and the migration chain did not run — production sat at `20260730_07_identity`,
so `category_rules`, `goals` and `goal_contributions` did not exist. The two
pages that select from those tables were the only two that failed, which is why
the dashboard looked healthy throughout.

The deploy step was documented, in this section, and was simply not performed. A
step that depends on somebody remembering it is a step that eventually does not
happen, and the failure stays invisible until a user opens the one page that
needs the new table.

## 7. Re-register the Plaid redirect URIs

In the Plaid dashboard, add the Railway URL alongside the localhost entries.
The values carried over from `.env` point at localhost and will not work.

Worth noticing that `PLAID_ENV` carries over as `production`, so this deployment
talks to live Plaid, not the sandbox.

## 8. Set `PLAID_WEBHOOK_URL`

```
railway variables --set PLAID_WEBHOOK_URL=https://<your-app>.up.railway.app/api/plaid/webhook
```

Skippable in the sense that the app boots and syncs without it, which is why it
is worth being explicit about what skipping costs.

Plaid does not finish fetching an Item's transaction history when Link closes.
It returns the most recent ~30 days and keeps backfilling the requested two
years in the background, for minutes at a small institution and considerably
longer at some. This webhook is the only way it tells us that finished.

Without it, `finance_sync/plaid_backfill.py` re-syncs each new connection on a
fixed schedule that gives up after three hours and marks the connection
`partial`. That is a backstop, not an equivalent: a slow bank outlasts it, and
the user is left with a fraction of their history and a note saying so. UAT
round 1 produced exactly that — two testers at the same bank, one with two years
of transactions and one with a single month.

Nothing else needs configuring. The endpoint is registered on every Item linked
from then on, and existing Items are updated at the next startup
(`plaid_backfill.install`). It is unauthenticated in the session sense and has
to be, since Plaid has no session with us; each request carries an ES256
signature over its own body, verified fail-closed before anything runs.

Verify from the Plaid dashboard's webhook log after the next connect, or watch
for `Plaid webhook TRANSACTIONS/...` in `railway logs`.

## Why one worker

`Procfile` pins `--workers 1 --threads 4`. This is correctness, not thrift:

- **The sync scheduler** ([app.py:687](../app.py#L687)) starts a background
  thread in the serving process. Two workers means two schedulers on the same
  timer, hitting Plaid twice.
- **The rate limiter** is in-memory (`RATELIMIT_BACKEND=memory`, SEC-0010), so
  each worker keeps private counters and the effective limit becomes N times the
  policy.
- **`AUTO_UPGRADE_DB`** runs Alembic at boot. Two workers racing the same
  migration is the exact hazard `config.py` documents.

Concurrency comes from threads, which share one process and therefore one
scheduler, one set of counters, and one SQLite connection pool. For a
single-household app this is ample; SQLite serialises writes, and four threads
will not contend meaningfully at this size.

Scaling past one instance means replacing all three: a Redis rate limiter, an
external scheduler, and Postgres. `config.py` already normalises
`postgres://` → `postgresql+psycopg://`, so the database half is one variable
and adding `psycopg[binary]` to `requirements.txt`.

## Two instances now sync the same accounts

The deployment runs its own scheduler against the same Plaid connections your
local installation uses, writing to a different database. Within minutes of the
first boot it had pulled rows the local copy does not have.

Nothing reconciles them, and nothing will. Pick one as authoritative and stop
the other from syncing — `SYNC_AUTO_ENABLED=0` in the loser's environment. Two
databases both convinced they are current is worse than either being stale.

## Registration is open

```bash
railway variable set "ALLOW_REGISTRATION=1"
```

Unlike `AUTO_UPGRADE_DB`, this one is a plain environment variable all the way
down — `ProductionConfig` does not override it, so setting it is enough. It is
read in a class body at import time, though, so the running process keeps the
old value: `railway redeploy --yes` (or any deploy), not `railway service
restart`.

A closed instance still serves `/register` and returns 403 with a page
explaining itself, which is why the URL looks like it works when it does not.
Check the page body, not the status code.

What opening it does and does not expose:

- `identity.register_account` creates a **household per registration** and makes
  the new user its owner, in one transaction. A stranger's account starts empty
  and every scoped query is bound to their own household, so they cannot see
  your accounts, transactions, or connections.
- It does mean a URL fronting real bank data accepts signups from anyone who
  finds it. There is no invitation gate on this path — `/join` is the invited
  route, `/register` is the open one.
- The `register` rate limit is spent before the form is parsed and before
  `scrypt` runs, so the route cannot be used as a CPU amplifier. That limiter is
  in-memory and resets on deploy (SEC-0010).

`REQUIRE_EMAIL_VERIFICATION` is still off, so an unverified address does not
block sign-in. Turning it on with `MAIL_BACKEND=console` would lock out every
new registrant — the two settings have to move together. Mail now goes out over
Postmark (below), so that switch is finally *safe* to flip; it is still a
separate decision, and worth making only after you have watched real
verification mail arrive.

## Mail goes out through Postmark

```
MAIL_BACKEND=postmark
MAIL_PASSWORD=<Postmark server API token>
MAIL_FROM=no-reply@dough-financial.com
```

### Railway blocks outbound SMTP, so this does not use SMTP

The block covers 587 and 2525 alike — 2525 is the conventional escape hatch and
it is closed here too, so there is no port to move to. This is ordinary for a
platform that would otherwise make a convenient spam relay, and it is not
configurable from this side.

`MAIL_BACKEND=postmark` sends the same message over Postmark's HTTP API on 443
instead, which nothing filters. It is one variable: `PostmarkBackend` reads the
server API token from `MAIL_PASSWORD`, because with Postmark the SMTP password
*is* the API token, so there is no second copy of the credential to keep in
step. The `MAIL_SERVER`/`MAIL_PORT` values can stay in place, unused, for an
installation that later runs somewhere SMTP works.

Measured both ways from the same code and credentials:

| Transport | From a laptop | From Railway |
|---|---|---|
| SMTP :587 | reaches Postmark | times out at 30s |
| SMTP :2525 | reaches Postmark | times out at 30s |
| HTTPS :443 | reaches Postmark | reaches Postmark |

The symptom is worth writing down because it names itself badly. `/settings`
reports "the confirmation email could not be sent", the log says `Verification
mail could not be sent`, and both are true and unhelpful — they describe every
mail failure there is. The line that identifies *this* one is the request
duration:

```
[WARN] Verification mail could not be sent  endpoint="settings.change_email"
[INFO] request  duration_ms=30134.7  path="/settings/email"  status=302
```

Thirty seconds, repeatable to within a tenth. `SmtpBackend` uses a 10-second
socket timeout, `smtp.postmarkapp.com` resolves to three addresses, and
`socket.create_connection` spends the timeout on each in turn: 3 × 10s. A mail
server that *rejects* a message answers in well under a second, so a round
multiple of ten seconds means the connection never opened at all.

The other half of the confirmation is on Postmark's side. A refused message
appears in the server's Activity as a bounce; a blocked connection leaves no
record anywhere, because nothing arrived. Checking that distinguishes "Postmark
said no" from "we never reached Postmark" without touching the deployment:

```bash
curl -s -H "X-Postmark-Server-Token: $TOKEN" \
     https://api.postmarkapp.com/deliverystats
```

### A pending account can only mail its own domain

Postmark approves new accounts by hand. Until yours is approved every recipient
must share the `From` domain, and anything else is refused per message:

```
ErrorCode: '412' — While your account is pending approval, all recipient
addresses must share the same domain as the 'From' address.
```

This is invisible from the SMTP side. Postmark accepts the session, returns
success, and turns the message into a bounce of type `SMTPApiError` afterwards
— so code that checks only whether `smtplib` raised will report a successful
send that never happened. That is a genuine limit of the SMTP path rather than
a bug here: the outcome is not known when the connection closes. The Activity
list and `/deliverystats` are the ground truth.

Over the HTTP API the outcome is in the response, so `PostmarkBackend` checks
it and a rejection is a rejection: the same message fails in about 0.2 seconds
with `ErrorCode 412` instead of appearing to succeed. That difference is the
second reason to prefer this transport, after the one that forced it.

Which means testing mail delivery to an outside address (a Gmail account, say)
proves nothing until the account is approved. Send to an address on
`dough-financial.com` first.

One token is the entire credential. Postmark's SMTP endpoint authenticates with
the Server API token in *both* the username and password fields, and the HTTP
API uses the same string as its `X-Postmark-Server-Token` header — so
`MAIL_PASSWORD` carries it whichever transport is in use, and it belongs in
`.env` and in Railway and nowhere else.

`MAIL_FROM` has to be a Sender Signature confirmed in the Postmark dashboard,
or an address on a domain verified there. Postmark refuses any other `From`
**per message**, so a wrong value here is a send-time failure and not a startup
one: the service boots clean, connects, authenticates, and then rejects every
message. `MAIL_DEFAULT_SENDER` is read as a second name for it, because that is
the name `flask_mail` uses and therefore the one Postmark's own setup page
prints — this application does not use `flask_mail`, and an operator following
those instructions would otherwise set a variable nothing reads and fall back to
`dough@localhost`, which is exactly the address Postmark refuses.

Until Phase 10.5 `tools/set_railway_env.ps1` carried no `MAIL_*` variable at
all, so a service configured before that ran on `console` no matter what `.env`
said. Check with `railway variable list` rather than assuming.

The confirmation is `/settings`: change your address and the link arrives in the
new inbox. Nothing else in the application reports the difference, because
sending to `console` *succeeds* — which is why this was invisible for so long.

To watch it from the outside:

```bash
railway logs | grep "dough.email"
```

A successful send logs `Sent verify_email mail to <address> via smtp`. A failure
logs `Verification mail could not be sent` from `dough/blueprints/auth.py`. The
link itself appears in neither — it is a bearer credential and is deliberately
kept out of the log stream, so a log that looks uninformative is the intended
result rather than a gap.

## Backups

**Snapshots are taken automatically.** `dough/services/backup.py` runs a daemon
thread in the serving process: one snapshot a minute after startup, then every
`BACKUP_INTERVAL_HOURS` (default 24), keeping `BACKUP_KEEP` (default 7). Each one
is verified — `PRAGMA integrity_check` plus per-table row counts against the
source — and a snapshot that fails either check is deleted rather than kept,
because a corrupt file that looks like a backup is the one you would plan around.

They are written **beside the database**, so on this deployment that is
`/data/backups` and not a directory inside the image. That distinction is the
whole point: a backup in the image is erased by the next deploy, and nothing
reports it until a restore.

| Variable | Default | Notes |
|---|---|---|
| `BACKUP_AUTO_ENABLED` | `1` | Off under `TESTING`. Separate from `SYNC_AUTO_ENABLED` on purpose — turning off bank polling must not turn off backups. |
| `BACKUP_INTERVAL_HOURS` | `24` | |
| `BACKUP_KEEP` | `7` | Whole copies of the database, so N snapshots is N times its size. |
| `BACKUP_DIR` | *(beside the database)* | Set only to override. |

Watch them with `railway logs | grep dough.backup`. A success logs `Backup ok`
with the size, table count and row count; a failure logs `Backup failed` with
the reason and the loop retries at the next interval rather than dying.

Take one by hand, any time:

```bash
python tools/backup_db.py --label pre-migration
```

### What this still does not cover

- **Losing the volume.** These snapshots sit on the same disk as the database,
  so they protect against corruption, a bad migration and an accidental delete —
  not against the disk going. Copying them off-host is still manual:
  `railway volume files --volume dough-volume download`. **This is the next piece
  of work, and until it is done a single Railway volume is the only copy of every
  household's financial history.**
- **`.sync_encryption_key`.** It is not in the database, and every stored
  institution credential is unreadable without it. A restore without it starts
  cleanly and cannot sync. See docs/runbooks/disaster-recovery.md.

## Known gaps

- **Off-site copies**, above.
- **Two workers would take two sets of snapshots** into one directory and prune
  each other's — the same single-process constraint as the sync scheduler
  (OPS-0012), and it breaks in the same way for the same reason.
