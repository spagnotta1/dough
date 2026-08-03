# Dough

A local web application for understanding your money, fronted by **Dough** —
the app's mascot, voice, and the name every intelligent feature answers to.
Connect your accounts (or upload CSV exports), and Dough categorizes your
transactions, explains what changed, watches your budgets, reviews your
portfolio, and answers questions about any of it in plain language.

## Dough

Dough is not decoration bolted onto an AI page. He is the product's
personality, and he is deliberately consistent everywhere:

- **One character, one voice.** Every generated word in the app comes from the
  shared `DOUGH_PERSONA` prompt in `dough/ai/persona.py`. Chat, the dashboard
  insight, the portfolio review and the one-line briefing all open with it, so
  they read as the same companion rather than four assistants with similar
  prompts.
- **Warm, never cute.** Dough encourages, never shames, and never uses fear.
  Going over budget is a fact to work with, not a failing. He is also barred
  from dog puns, emoji and filler openers — in a financial product the
  personality has to live in the stance and the clarity, not the decoration.
- **He appears wherever the app is being intelligent**, not just in chat:
  `Ask Dough` (the chat), `Dough's Insight` (dashboard), `Dough's Portfolio
  Review` (investments), his Budget Coach line, every empty state, and the
  waiting state of every model call.
- **The artwork is one file.** `brand/dough-master.jpg` is Dough — a finished
  brand asset, and the only one. It is archival: it sits outside `static/`, is
  never served, and is read only when the assets are rebuilt. Everything the
  app shows is a crop or a scale of it. `tools/build_dough_assets.py` lifts the
  cream background off by flooding from the image border (a colour key would
  punch holes in his eyes, whose whites are 25 levels from the background) and
  emits a full body, a head-and-ears crop, and the mark — his eyes and nose —
  which is what the 16/32/48px favicons carry, because no mascot illustration
  survives 16×16. `tools/build_icons.py` composites those onto tiles.
  `static/js/dough.js` only decides which crop a slot gets, and
  `templates/_dough.html` is the macro every page places him through.

  He was briefly a runtime SVG redraw "traced by measurement" from that
  reference, and it shipped a recognisably different dog. A test now rejects
  any `<path>` in `dough.js`, because "it matches the reference" is not
  something anyone can check later.
- **He does not follow the theme, and that is the point.** A photograph cannot
  be re-tinted without altering the artwork, so Dough is the same golden puppy
  on all 16 palettes and the theme lives in the disc, the bubble and the panel
  behind him. That inverts a constraint rather than removing it: a fixed mascot
  cannot be pushed away from a surface he clashes with, so
  `tests/browser/test_dough_theming.py` samples his real rendered pixels in a
  live engine and fails any theme where he stops separating from the panel —
  and the fix is that theme's `--panel`, never the drawing.
- **He is never the only signal.** Nothing important is carried by an
  expression alone — every mascot state has text beside it — and all of his
  motion is switched off under `prefers-reduced-motion`.

## Features

- **Automatic account synchronization** — connect via Plaid (any supported
  bank, brokerage, or crypto exchange) or Coinbase directly once; balances,
  holdings, and transactions then sync automatically every 12 hours (plus
  manual Refresh / Refresh All)
- Upload CSV exports from any bank or card — the importer reads the header and
  works out which columns are the date, the description and the amount
- Automatic transaction categorization based on keywords
- Customizable categorization rules
- Dashboard with spending breakdowns and charts
- Investments page with synced holdings (shares, avg cost, price, gain/loss)
- Net worth and allocation charts that update automatically after every sync
- Sync History page with per-run stats and error log
- Detailed transaction list with filtering and search
- Local storage using SQLite
- Sign-in, and a household you can invite people into — everyone in a household
  sees the same accounts, and no household can see another's data
- A public landing page, self-serve registration (off by default), password
  recovery by email, and an account page for changing your password, signing out
  everywhere, and managing API tokens

## Account Synchronization (finance_sync)

The `finance_sync/` package implements the Adapter Pattern: every institution
is a single adapter class registered in `finance_sync/adapters/`, and the
institution-agnostic `SyncEngine` runs the same pipeline for each —
refresh token → validate → fetch → normalize to canonical models → validate →
persist to SQLite → log.

- **Sandbox mode (default):** with no API credentials configured, adapters use
  deterministic simulated provider backends (`finance_sync/sandbox.py`) so the
  entire connect → sync → dashboard flow works locally.
- **Live mode:** set the institution's environment variables in `.env` and the
  adapter switches to the real API with no code changes:
  - Plaid (recommended): `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`
    (`sandbox` | `development` | `production`, default `sandbox`). Get
    credentials from the Plaid dashboard. Plaid is an aggregator — one
    connection lets you link many real institutions (banks, brokerages,
    crypto exchanges, including Capital One and most others) through an
    embedded widget (Plaid Link), each becoming its own row under "Connected
    Institutions".
  - Coinbase: `COINBASE_CLIENT_ID`, `COINBASE_CLIENT_SECRET` (OAuth2) — a
    direct integration independent of Plaid, useful if you'd rather not route
    crypto through the aggregator.

  Two other adapters (Capital One, E*Trade) and a designed-but-unbuildable
  Vanguard adapter were removed: Capital One has no self-serve public API,
  and E*Trade's developer access requires their partner program — both are
  better covered through Plaid anyway.
- **Tokens** are stored encrypted (Fernet). The key comes from
  `SYNC_ENCRYPTION_KEY` or is generated once into `.sync_encryption_key`.
  Usernames and passwords are never stored.
- **Settings:** `SYNC_INTERVAL_HOURS` (default 12), `SYNC_AUTO_ENABLED`
  (set `0` to disable background sync).
- **Adding an institution** requires exactly one new adapter class decorated
  with `@register_adapter` — no synchronization code changes.

Run the test suite with `python -m pytest tests/`.

## Requirements

- Python 3.11 or higher
- pip (Python package manager)

## Installation

1. Clone this repository:
```bash
git clone <repository-url>
cd dough
```

2. Create and activate a virtual environment:
```bash
# Windows
python -m venv venv
.\venv\Scripts\activate

# macOS/Linux
python -m venv venv
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Initialize the database:
```bash
python init_db.py
```

## Usage

1. Start the application:
```bash
flask --app app run
```

2. Open your web browser and navigate to `http://localhost:5000`

3. Upload a CSV export from any bank or card:
   - Go to the Upload page
   - Select the account these belong to
   - Choose your CSV file(s)
   - Click Upload

4. View and manage your transactions:
   - Dashboard: View spending summaries and charts
   - Transactions: Browse and filter your transaction history
   - Rules: Manage automatic categorization rules

## CSV Format

Any bank's export, as downloaded. The importer reads the header row and works
out which column is which, so there is nothing to rename or rearrange first.

It needs three things, under whatever name your bank uses for them:

| Role | Headers understood |
|---|---|
| **Date** | `Transaction Date`, `Posted Date`, `Posting Date`, `Date`, `Booking Date`, … |
| **Description** | `Description`, `Payee`, `Merchant`, `Memo`, `Narrative`, `Details`, `Name`, … |
| **Amount** | `Amount`, `Transaction Amount` — **or** a `Debit`/`Credit` pair (`Money Out`/`Money In`, `Withdrawal`/`Deposit`, …) |

Case, spacing and punctuation are ignored, so `posted_date` and `Posted Date`
are the same column. A `Balance` column is used when present and not required.

The two amount shapes are treated differently on purpose. A single column may
hold an unsigned magnitude, so the direction is inferred from the description
with the balance delta as a fallback. A **debit/credit pair states the
direction**, so it is taken as given — otherwise a row under Credit reading
"PAYMENT THANK YOU" would be booked as money out, which is what the wording
rules say about that word.

Currency symbols, thousands separators, accountants' `(45.00)` negatives and
European decimal commas are all read as written.

If a file cannot be mapped, the upload page says so and lists the headers it
did find, rather than failing with an error id. `tests/test_csv_import.py`
covers the exports of several banks.

## Customizing Categories

You can customize how transactions are automatically categorized:

A new account starts with **no rules at all**. Dough does not guess at your
banks and never copies anyone else's: open the Rules page with uncategorized
transactions and it reads your own descriptions and proposes rules built from
them, which you accept or ignore.

1. Go to the Rules page
2. Add new rules by specifying:
   - Category name
   - Keyword to match in transaction descriptions
3. Remove existing rules as needed, or **Clear all rules** to start over

Every rule edit recalculates the category on **every** transaction in the
household, so a rule fixes the past as well as the future. The cost is that a
category you set by hand on the Transactions page is not protected from it —
nothing in the schema tells the two apart, so the next rule edit can overwrite
it. `docs/rule-engine.md` explains why, with the worked example, and holds the
TODO for preserving manual overrides.

## Households and invitations

A **household** is the unit of isolation: everyone in one sees the same
accounts, transactions, balances and budgets, and nothing crosses between them.
The first run creates your household and makes you its owner.

To add someone, go to **Household** in the profile menu and create an invitation
link.

- The link is **single-use** and expires on its own (`INVITE_TTL_HOURS`,
  default 72). Revoke it any time before it is used.
- It is **shown once**. Only a hash of it is stored, so it genuinely cannot be
  recovered — if you lose it, revoke and issue another.
- **Anyone holding the link can join and see everything.** Send it the way you
  would send a password.
- **Owners** can invite, remove people, and change roles. **Members** can do
  everything else. A household always keeps at least one owner; the app refuses
  the change that would leave none.

One login belongs to one household. Somebody who already has a household cannot
accept an invitation into another — they are told so rather than moved, because
accepting would mean silently leaving, and the household left behind might be
one nobody else can administer. See `docs/adr/0009-authentication-and-membership.md`.

### Security settings worth knowing

| Variable | Default | What it does |
|---|---|---|
| `SESSION_IDLE_SECONDS` | 43200 (12 h) | Signed out after this long with no requests |
| `SESSION_ABSOLUTE_SECONDS` | 604800 (7 d) | Signed out this long after signing in, active or not |
| `INVITE_TTL_HOURS` | 72 | How long an unused invitation link stays valid |
| `TRUSTED_PROXIES` | 0 | How many reverse-proxy hops in front of the app are yours. Leave at 0 unless you run one — `X-Forwarded-For` is attacker-controlled, and believing it turns the login throttle into a no-op |
| `ALLOW_REGISTRATION` | 0 | Whether strangers may create an account. Off because this app fronts real bank data and is routinely exposed on a LAN. `/register` still exists when it is off, and says so |
| `PUBLIC_BASE_URL` | — | The canonical URL, used to build links in outbound mail. Unset means they are built from the request's `Host` header, which is client-controlled |

CSRF protection is always on and has no environment switch. Every unsafe request
must carry the session's token, including sign-in, registration and password
reset.

## Accounts

A **household** is the unit of isolation (below); an **account** is one login
inside one. Three ways one comes into existence, and they have not changed
shape — only grown a third:

- **`/setup`** — the first account on an installation, which owns the first
  household. Runs once and refuses afterwards.
- **`/join/<token>`** — an invitation into somebody else's household.
- **`/register`** — self-serve, and **off by default** (`ALLOW_REGISTRATION`).
  It creates an account *and* a household of its own, with the new account as
  owner. When registration is closed the route still answers and explains that
  invitations are how people get in, rather than 404ing at somebody who may
  simply have mistyped.

### Forgetting your password

`/forgot-password` takes the address on the account and mails a link.

The response is identical whether or not that address has an account — same
words, same page, same time taken. That is deliberate and it is the whole point
of the route's design: any difference would make it a free test of whether a
given person banks with Dough. If you are certain the address is right and
nothing arrives, the account may not have an address on file; check
**Account & security**.

Reset links work **once**, expire within the hour, and are spent the moment the
form loads — so if you fail the password rules you need a fresh link rather than
a second try. Using one signs out every browser and stops every API token the
account has issued, which is the point: the reason to need a reset is that
somebody else may have the old password.

### Account & security

In the profile menu. From there you can:

- change your password (the current one is required — an unlocked screen is a
  session, and this is the operation that locks the real owner out);
- add or change your email address, and re-send its confirmation;
- **sign out everywhere**, which ends every browser session *including this
  one* and stops every API token — the button to press if a device goes missing;
- create, list and revoke API tokens, with the date each was created and last
  used.

An API token is shown once, when it is created. Only a hash is stored, so it
genuinely cannot be recovered — the same property as an invitation link.

### Email

Verification and reset links go through `MAIL_BACKEND`:

| Value | What it does |
|---|---|
| `console` (default) | Prints the message to the terminal. Right for a self-hosted instance with no mail server — and the reason the flow works before anything is configured |
| `smtp` | Sends it. Needs `MAIL_SERVER`, and uses STARTTLS |
| `memory` | Keeps it in a list. The test suite's backend |

`console` in production is a warning at startup rather than an error: it means
reset links are printed to a terminal that nobody locked out of their account
can reach.

## Applying the multi-tenancy migration

`20260726_02_multitenancy` adds `household_id` to fourteen tables and rebuilds
each one. Rebuilds are the operation that can lose rows while leaving a
perfectly valid schema behind, so the migration is verified against a copy
before it touches anything real.

```bash
# 1. A fresh, verified backup (per-table row counts, deleted if they disagree).
python tools/backup_db.py

# 2. Capture the row counts the migration must preserve, and take a copy.
python tools/verify_tenancy.py checkbook.db --emit-baseline counts.json
cp checkbook.db copy.db

# 3. Migrate the copy, and check the invariants on it.
AUTO_UPGRADE_DB=0 DATABASE_URL="sqlite:///$PWD/copy.db" flask db upgrade
python tools/verify_tenancy.py copy.db --baseline counts.json

# 4. Only if that reports "All N tenancy invariants hold", do the real one.
AUTO_UPGRADE_DB=0 flask db upgrade
python tools/verify_tenancy.py checkbook.db --baseline counts.json

# 5. Then the suite, then a sync.
python -m pytest -q
```

`tools/verify_tenancy.py` exits non-zero on any failure, so steps 3 and 4 drop
into a script without anyone having to read the output. It checks structure
(the column exists, is `NOT NULL`, is indexed, has its foreign key), data (no
orphaned or unresolvable `household_id`, no child row whose household disagrees
with its parent's), membership (every household has an owner, every user has a
household), uniqueness (the six constraints that had to gain a household column
did, and the three that must stay global did not), and row counts against the
baseline.

### Verifying a sync

The same tool checks a sync, with one difference: a sync is *supposed* to insert
rows, so the question becomes whether it inserted them in the right households
and only in the tables a sync writes.

```bash
python tools/backup_db.py --label pre-sync
python tools/verify_tenancy.py checkbook.db --emit-baseline counts.json
# ... run one sync ...
python tools/verify_tenancy.py checkbook.db --baseline counts.json --may-grow sync
```

`--may-grow sync` lets the sync-written tables gain rows while every other table
must still match exactly, and nothing may shrink in either mode. A sync that
inserted a `budgets` row fails this check, which is the point.

### The invitations migration

`20260726_03_invitations` adds one empty table and touches nothing that holds
data, so it needs none of the ceremony above:

```bash
python tools/backup_db.py
AUTO_UPGRADE_DB=0 flask db upgrade
python tools/verify_tenancy.py checkbook.db
```

The backup is still worth taking. The verification is not optional either —
`household_invites` is tenant-scoped like any other table, and the verifier
checks that it really did get its `NOT NULL`, its foreign key and its index
rather than assuming a hand-written `create_table` remembered all three.

If step 4 goes wrong, recovery is a backup restore, not a downgrade — although
unlike the reconciliation revision, this one *does* have a working downgrade
(`flask db downgrade 20260726_01_reconcile`). It refuses to run once a second
household exists, because dropping the column at that point would merge two
families' ledgers with nothing left to tell them apart.

## Deployment

**Run exactly one application worker.**

```bash
gunicorn --workers 1 'app:create_app()'
```

This is not a performance suggestion, and it is not enforced by the code. The
background sync scheduler is started lazily inside the serving process, so each
worker starts its own. Two workers means two schedulers refreshing the same
connections at the same time — duplicated provider API calls against per-item
rate limits, overlapping writes to the same accounts, and a `sync_history` that
misreports what happened because two runs interleave in one table.

Nothing will tell you this is happening. The development server is one process
and the test suite disables the scheduler, so the first environment in which the
problem exists is production, and the symptom arrives as provider errors rather
than as anything naming the process count.

If throughput ever needs more than one worker, the fix is to move scheduled work
into its own process rather than to add a lock so several schedulers can coexist.
See `OPS-0012` in `docs/security.md` for the decision and its alternatives.

Until the process model is settled, `SYNC_AUTO_ENABLED=0` is set in `.env`.
Manual **Refresh** and **Refresh All** work regardless — only the unattended
12-hour cycle is off.

### Rotating the AI credential

`ANTHROPIC_API_KEY` is a live secret in `.env`. If it is ever printed — by a
failing test, a traceback, a log — treat it as compromised and rotate it:

1. Revoke the old key at <https://console.anthropic.com/settings/keys>.
2. Create a new one and put it in `.env`.
3. Restart the app.
4. **If this installation is deployed, set the key on the deployment as well:**
   ```bash
   railway variable set "ANTHROPIC_API_KEY=<the new key>"
   ```
5. Ask Dough one question and confirm you get an answer — on the deployment as
   well as locally, since they hold the key separately.
6. Confirm the *old* key now fails:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/v1/models \
     -H "x-api-key: $OLD_KEY" -H "anthropic-version: 2023-06-01"   # expect 401
   ```

Step 6 is the one people skip. Creating a new key does not disable the old one;
without the revoke, both work and only one of them is in your `.env`.

Step 4 is the one that has actually bitten this installation. `ANTHROPIC_API_KEY`
is a Railway *service variable*, written once by `tools/set_railway_env.ps1`
reading your local `.env`. Deploying sends code and nothing else, so a rotation
that stops at step 3 leaves the container presenting the key you revoked in step
1 — and because step 1 worked, it is presenting a key that is now definitively
invalid. The symptom is Dough saying *"My API key was rejected"* on the
deployment while working perfectly on your machine, with a 401
`authentication_error` from `dough.ai.service` in the Railway logs.

Two traps in that one command:

- **Use the argument form, not `railway variable set --stdin`.** The CLI
  prepends a byte-order mark to anything fed on stdin, storing a value three
  bytes wrong with nothing reporting a problem. See the comment in
  `tools/set_railway_env.ps1` for the measurement.
- **Do not re-run `tools/set_railway_env.ps1` to fix one variable.** It
  generates a fresh `SECRET_KEY` on every run, which invalidates every session
  and CSRF token in flight — a re-login for everyone, to change a value the
  script was not being run for.

Verify by comparing the deployed value against `.env` with an **Ordinal** string
comparison or a hash, never `-eq`: PowerShell's default comparison treats a
byte-order mark as zero-weight, so a BOM-corrupted key compares equal to a clean
one. `docs/deploy-railway.md` covers this at more length.

### Health checks

Two endpoints, and pointing your supervisor at the wrong one causes the outage
it was meant to prevent:

| Endpoint | Answers | Point this at |
| --- | --- | --- |
| `GET /health/live` | Is the process running? Touches nothing. | A supervisor that **restarts** on failure |
| `GET /health/ready` | Should traffic be sent here? Checks the database, the migration head, and required configuration. 503 if any fails. | A load balancer that **removes from rotation** |

Never point a restart policy at `/health/ready`. A readiness check fails when a
*dependency* is down, and restarting a healthy application does not bring the
database back — it just takes down the process that was still serving cached
pages.

Both are public, deliberately: a probe holds no session, and a readiness check
that returned 302-to-login would tell the balancer everything was fine. The body
is therefore check names and booleans and nothing else — no versions, no
configuration, no error text. The detail is in the log line with the same trace
id.

### Logs

One JSON object per line in production, human-readable when `DEBUG` is on.
`LOG_JSON` overrides both ways, so the production format can be reproduced
locally when the question is about the logs themselves. `LOG_LEVEL` defaults to
`INFO`.

Every line carries a `trace_id`, and every response carries it back in the
`X-Request-ID` header. An error page shows the same id and asks the user to
quote it, so one string finds everything:

```bash
grep '"trace_id": "a1b2c3d4"' app.log | jq .
```

An inbound `X-Request-ID` is honoured — that is what makes a proxy's access log
and this application's log line up — but only if it matches
`[A-Za-z0-9._-]{1,64}`. It is an attacker-controlled value that gets echoed back
and written to whatever reads the logs.

Background work (a sync, a scheduled job) gets its own id under the same field
rather than inheriting the request's, so a worker's lines never get filed under
a request it has nothing to do with.

**What is never logged:** passwords, API keys, tokens, account numbers, query
strings (the filters on this app *are* the query string), AI prompts and
completions, and exception tracebacks — the type and message only. Redaction
runs at the formatter, so it applies to log lines nobody remembered to think
about.

### The audit trail

`audit_events` records who did what: sign-ins and failures, invitations,
membership and role changes, connections added and removed, syncs, AI requests,
and balance edits. It is append-only, enforced by a SQLAlchemy hook — the
application cannot rewrite its own history, including by accident.

Reading it in the application goes through `dough.services.audit.recent()`,
which filters on the current household. This is the one table whose
`household_id` is nullable (a failed login belongs to no household), so it is
outside the ORM tenancy backstop and that function *is* its isolation. See
ADR-0011.

Nothing is ever deleted from it and there is no retention policy —
`OPS-0013` in `docs/security.md`.

## Development

- The application uses Flask for the backend
- SQLite with SQLAlchemy for data storage
- Tailwind CSS for styling
- Chart.js for visualizations
- Alpine.js for simple interactivity

`AGENTS.md` at the repository root is the brief for coding agents — the mascot
rules, the architecture rules and the tenancy constraint, each stated in the
form the test that enforces it checks. Read it before changing code.

### Where code goes

```
app.py                  the factory: config, db, request hooks, error handlers
dough/blueprints/       every HTML route, grouped by what it is responsible for
dough/api/              the versioned JSON API — envelope, errors, auth, v1/
dough/services/         the queries and rules, callable without a request
dough/ai/               the model provider, prompts, caching, output formatting
dough/auth.py           authentication, authorization, CSRF
dough/tenancy.py        household scoping and the ORM backstop
models.py               the schema
finance_sync/           the institution adapters and the sync pipeline
```

Three services are worth knowing about by name, because each is a seam rather
than a query:

- `dough/services/identity.py` — creating an account, proving an address,
  replacing a password, invalidating every credential. Three surfaces call it,
  which is the duplication it exists to prevent.
- `dough/services/email.py` — `console` / `memory` / `smtp` behind one `send()`.
- `dough/services/ratelimit.py` — the policy table and a memory backend, with
  the interface a Redis one would implement.

Four rules, each with a test behind it rather than a convention:

- **A blueprint may not import `app`.** Whatever a route needs from the
  application it gets from `current_app`; whatever it needs from the domain it
  gets from `dough/services/`. `app` imports the blueprints, so the reverse is a
  cycle.
- **A service may not import `app`, `anthropic`, or Flask's response helpers.**
  A service returns data. Turning data into a response is the route's job, and a
  service that renders a template cannot be called by the scheduler.
- **Endpoint names are `blueprint.view`** — `url_for('transactions.index')`, not
  `url_for('transactions')`. URLs themselves are frozen by
  `tests/test_url_map_snapshot.py`, which pins every (rule, methods) pair and
  deliberately ignores endpoint names.
- **No business logic in `dough/api/v1/`.** A resource module reads the request,
  calls a service, and shapes a response — the *same* service the HTML blueprint
  calls. That is what makes "the API and the page agree" structural rather than
  something anybody has to maintain, and
  `tests/test_services.py::test_api_resource_holds_no_business_logic` rejects
  any database write issued from a resource module.

Adding a page means a view in the right blueprint, its query in a service, and a
line in `EXPECTED_RULES`. If the change needs `app.py`, it is probably wiring —
and if it isn't, it likely belongs somewhere else.

Adding an API endpoint means the same, plus an entry in `docs/api/openapi.yaml`
in the same commit — `tests/test_openapi.py` fails a route that is served but
undocumented, and a documented one that is not served.

## The API

`/api/v1` is the stable contract for every client — the web UI, and anything
native or third-party that comes later. One response envelope, one error
vocabulary, one pagination convention, and the same service layer underneath as
the pages.

```bash
# Get a token (shown once — only its hash is stored)
curl -X POST https://localhost:5000/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"sal","password":"…","device_name":"iPhone"}'

# Use it
curl https://localhost:5000/api/v1/transactions?page_size=25 \
     -H 'Authorization: Bearer dgh_…'
```

Tokens are per-device, scoped (`read` / `write`), revocable individually, and
never stored in recoverable form. Revoking one does not sign anybody else out.

- `docs/api/README.md` — the conventions, and what to do about the unversioned
  endpoints (which still work and are not deprecated).
- `docs/api/openapi.yaml` — the formal specification.
- `docs/adr/0012-versioned-api-contract.md` — why versioned, why opaque tokens
  rather than JWT, and what it cost.
- `docs/adr/0013-credential-generations.md` — how one counter invalidates every
  session and every token at once.
- `docs/adr/0014-public-surface-and-identity-lifecycle.md` — why `/` branches
  instead of moving the dashboard, why a reset link is spent by *loading* the
  form, and why registration is off by default but the route still exists.

## License

This project is licensed under the MIT License - see the LICENSE file for details. 