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

## Migrations are manual

`ProductionConfig` assigns `AUTO_UPGRADE_DB = False` as a class attribute, so
under `APP_ENV=production` the environment variable is read and then discarded.
Setting `AUTO_UPGRADE_DB=1` looks like it enables boot-time migrations and does
nothing whatsoever.

That is the intended design — two workers racing the same migration is the
hazard `config.py` documents — but it means a schema change needs the chain run
deliberately, over SSH:

```bash
railway ssh "cd /app && FLASK_APP=app:create_app flask db upgrade"
```

The uploaded database is already stamped at the head revision
(`20260730_07_identity`), so nothing is needed today.

## 7. Re-register the Plaid redirect URIs

In the Plaid dashboard, add the Railway URL alongside the localhost entries.
The values carried over from `.env` point at localhost and will not work.

Worth noticing that `PLAID_ENV` carries over as `production`, so this deployment
talks to live Plaid, not the sandbox.

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

## Known gaps

- **Back up the volume.** Nothing here replicates `checkbook.db`.
  `tools/backup_db.py` exists; run it on a schedule and pull the output off with
  `railway volume files --volume dough-volume download`.
