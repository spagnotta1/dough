# Deploying to Railway

This application is a single always-on process with a SQLite file on a
persistent disk. That is not an accident of how it grew — three of its parts
keep state in the serving process, and the deployment has to preserve that. The
order below matters; step 4 will fail if step 2 has not been done.

An earlier attempt targeted Vercel. It cannot work: serverless functions get a
read-only filesystem outside `/tmp`, `/tmp` is per-instance and wiped on freeze,
and `*.db` is gitignored so the database is not even in the deployed tree.
`pyproject.toml` and `vercel.json` were removed when that path was abandoned;
`requirements.txt` is the single dependency source again.

## 1. Create the service

```bash
npm i -g @railway/cli
railway login
railway init          # or `railway link` for an existing project
```

Railway's Nixpacks builder detects Python from `requirements.txt`, reads
`.python-version` for the interpreter, and runs the `Procfile`. There is no
Dockerfile and none is needed.

`.python-version` says 3.11 rather than the newest available. That is the
highest version `.github/workflows/ci.yml` actually runs the suite against
(3.10 and 3.11), and the pins in `requirements.txt` were verified there.
Deploying on an interpreter no test has ever run under is a change to make
deliberately, with a CI matrix entry to back it, rather than by taking the
platform default.

## 2. Add the volume

In the dashboard, or `railway volume add`. **Mount it at `/data`.** Anything
else means passing `-VolumePath` to the script in step 3 to match.

This is the whole reason for choosing a host with disks. `checkbook.db` lives
here, outside the image, so a deploy replaces the code and leaves the data
alone.

## 3. Set the environment variables

```powershell
.\tools\set_railway_env.ps1 -PublicBaseUrl https://<your-service>.up.railway.app
```

Add `-DryRun` first to see what it will set without setting it.

The script exists so that `ENCRYPTION_KEY` is copied from
`.sync_encryption_key` rather than retyped or regenerated. That key decrypts the
Plaid access tokens in `connected_accounts.auth_blob`. Getting it wrong does not
fail at boot — the app starts normally and the four stored connections become
permanently unreadable at the next sync, recoverable only by re-linking every
institution.

What it sets, and why each one is not a default:

| Variable | Value | Why |
|---|---|---|
| `APP_ENV` | `production` | Anything else lets `config.py` generate and *write* key files. Also what turns on `ProductionConfig.validate`. |
| `SECRET_KEY` | generated | Required in production. Generated fresh, not copied — reusing the dev key would let a copy of the working tree forge sessions. |
| `ENCRYPTION_KEY` | from `.sync_encryption_key` | See above. The one value that must not be regenerated. |
| `DATABASE_URL` | `sqlite:////data/checkbook.db` | Four slashes. Three is a *relative* path, which lands the database inside the image. |
| `UPLOAD_FOLDER` | `/data/uploads` | The default is inside the app directory, so uploaded statements would vanish on the next deploy. |
| `AUTO_UPGRADE_DB` | `1` | Production defaults this off because two workers racing Alembic is a real failure. One worker, no race. |
| `SYNC_AUTO_ENABLED` | `1` | The background scheduler genuinely works here, which it could not on serverless. |
| `APP_HTTPS` | `1` | Sets `SESSION_COOKIE_SECURE`. |
| `TRUSTED_PROXIES` | `1` | Railway terminates TLS at one proxy. At `0`, `dough/auth.py` ignores `X-Forwarded-For`, so every client looks like the proxy — one shared login-throttle bucket, and audit rows recording the proxy as the actor. |
| `PUBLIC_BASE_URL` | your URL | Otherwise outbound mail links are built from the client-controlled `Host` header. |

`ANTHROPIC_API_KEY` and the `PLAID_*` values are forwarded from your local
`.env` if present. Absent ones are skipped rather than set empty, because
`config.py` treats unset as "that feature is off" and empty the same way, with
more noise.

## 4. Deploy, then move the database across

```bash
railway up
```

The first boot creates an empty schema from the Alembic chain. To bring your
existing 1,378 rows over instead:

```bash
railway volume browse /        # TUI; upload checkbook.db into /data
```

Do this **before** the first request touches the app, or `AUTO_UPGRADE_DB` will
have stamped a fresh empty database and your upload will collide with it. If
that happens, delete the file through the same TUI and re-upload.

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

## Known gaps

- **Mail is `console`.** Password-reset and verification links print to the
  deploy logs instead of being sent. Fine while you are the only account; set
  `MAIL_BACKEND=smtp` and `MAIL_SERVER` before inviting anyone.
- **Back up the volume.** Nothing here replicates `checkbook.db`.
  `tools/backup_db.py` exists; run it on a schedule and pull the output off the
  volume.
- **Plaid redirect URIs** must be re-registered in the Plaid dashboard against
  the Railway URL. The values in `.env` point at localhost.
