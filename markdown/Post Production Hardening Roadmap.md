# Dough — Post Production Hardening Roadmap

## Purpose

This document defines the next engineering initiatives after completion of the current Phased.
The objective is to evolve Dough from a mature personal finance application into a production-grade fintech platform.

The current architecture already includes:

- Multi-tenant household isolation
- Tenant-aware ORM boundaries
- CSRF protection
- Authentication hardening
- Audit logging
- AI provider abstraction
- Service-layer architecture
- Blueprint extraction
- Database migrations
- Design system foundation
- Structured logging
- Health checks

The remaining work focuses on:

- Protecting financial data
- Operational maturity
- Scalability
- Privacy controls
- Production infrastructure readiness

## Priority Classification

### Critical Before Public Production

These items address direct financial/security risk.

- Financial data encryption
- Plaid credential protection
- Secret management
- Session lifecycle security

### Required Before Scaling

These items address reliability and operational growth.

- Database production readiness
- Background job architecture
- Backup and disaster recovery
- Observability

### Enterprise Maturity Enhancements

These improve compliance, maintainability, and long-term growth.

- Privacy/data governance
- AI governance
- Security automation
- Mobile/API readiness

---

## Phase 10 — Financial Data Security Hardening

> **Note, 2026-07-30.** Phase 10 as actually built was **the versioned API
> contract** — what this document lists further down as Phase 18, Mobile/API
> readiness. It was pulled forward deliberately: the backend was judged mature
> enough after Phase 9 to expose a contract without churning it, and the
> service-layer extraction it required is a prerequisite for several of the
> items below rather than a consequence of them.
>
> Shipped: `/api/v1` (49 endpoints), a response envelope, a closed error
> vocabulary, pagination and filtering conventions, opaque bearer tokens with
> per-token scopes and revocation, an OpenAPI specification with a drift test,
> and four services extracted so the web UI and the API share one
> implementation. See `docs/adr/0012-versioned-api-contract.md` and
> `docs/api/README.md`.
>
> It also delivered part of §10.2's spirit ahead of schedule — API tokens are
> hashed at rest and never logged — and left two findings behind, SEC-0017
> (bearer tokens have no second factor) and SEC-0018 (no rate limit beyond the
> login throttle), both recorded in `docs/security.md`.
>
> **Update, 2026-07-30 — Phase 10.5 closed the rest of this section.** What was
> "not done" above is now done, with one correction worth recording because the
> plan was wrong about it: **§10.1 was already true.** Plaid and Coinbase
> credentials have been Fernet-encrypted in `connected_accounts.auth_blob` since
> the sync package was written; `finance_sync/crypto.py` did it and nobody had
> checked. What was actually missing was everything *around* the encryption —
> a key that production refuses to generate (generating one is silent, succeeds,
> and makes every stored token unreadable at the next sync), an error that says
> which of "no key" and "wrong key" happened, and any test at all. Those exist
> now: `tests/test_encryption.py`, and SEC-0024 / OPS-0025 in `docs/security.md`.
>
> §10.2 (secret management) shipped as `config.REQUIRED_SECRETS` — a table
> rather than a chain of `if not X: raise`, so a deployment missing three
> variables is told about three variables rather than one per restart. See the
> "Secrets" section of `docs/security.md`.
>
> **§10.3 was done separately, as Phase 10.5 (2026-07-30).** The note that used
> to sit here said `session_version` should also cover API tokens; it now does,
> and the counter it describes was built at the same time because it did not
> exist either. `AppUser.session_version` stamps every credential — the session
> cookie and every `api_tokens` row — and raising it invalidates all of them on
> both surfaces at once. The bump is a `before_flush` listener rather than a
> call, so a password-change route written later inherits it without knowing it
> exists. See `docs/adr/0013-credential-generations.md` and SEC-0019.
>
> What §10.3 asked for that was still open when this note was written — the
> **triggers** — shipped in the same phase: `/settings/password`,
> `/settings/sessions/revoke` ("sign out everywhere"), and a completed password
> reset all raise the counter. Account deletion, role changes and household
> removal were reviewed and need nothing: they are already covered by
> `api_tokens.authenticate` re-reading the user on every request.
>
> Phase 10.5 also delivered the identity lifecycle the roadmap never listed,
> because until there was a sign-up button the application did not need one: a
> public landing page at `/`, `/register`, `/forgot-password`,
> `/reset-password/<token>`, `/verify-email/<token>`, an account settings page,
> an `EmailService` abstraction, and the `RateLimiter` seam that narrows
> SEC-0018. See `docs/adr/0014-public-surface-and-identity-lifecycle.md`.
>
> Two defects were found and fixed during that work, both in code that predated
> it: SEC-0020 (`@public` skipped the session-lifetime check, which only became
> exploitable once a public view also rendered signed-in content) and SEC-0021
> (the bearer actor outlived the request that set it, so a session request
> following an API request skipped its own session check).

### Objective

Protect sensitive financial information even if the database, backups, or credentials are compromised.

### 10.1 Encrypt Plaid Access Tokens — done, 2026-07-30 (Phase 10.5)

> **The premise below was wrong, and finding that out was the work.** "Verify
> how Plaid credentials are stored" turned out to be the whole task: they were
> *already* encrypted. `finance_sync/crypto.py` has wrapped `auth_blob` in
> Fernet since the sync package was written, and nobody had checked, so this
> section was planned as though the encryption did not exist.
>
> What was genuinely missing was everything around it, and each item is a
> failure that only appears in production:
>
> - **A key production refuses to generate.** Generating one when the file is
>   absent is silent, succeeds, and makes every already-stored token unreadable
>   — and the failure arrives at the *next sync*, reported as an encryption
>   error rather than as the missing file that caused it. On a container
>   filesystem that starts empty on each deploy, that is every connection
>   breaking on every deploy with nothing naming the cause.
> - **Errors that distinguish "no key" from "wrong key".** They have different
>   recoveries, and the old message said neither.
> - **`ENCRYPTION_KEY` as the documented name**, with `SYNC_ENCRYPTION_KEY`
>   still winning when both are set so existing installations need no change.
> - **Any test at all.** `tests/test_encryption.py` now asserts the stored
>   *column* differs from the plaintext (not just that the cipher works — the
>   cipher being correct proves nothing if a write path stops calling it), that
>   a round trip through separate service objects succeeds, and that a missing
>   key fails safely.
>
> Residual: there is no in-place key rotation. See OPS-0025.

**Current Risk**

Verify how Plaid credentials are stored.

Potential issue:

```
Database
  |
Plaintext access_token
```

If the database is compromised, an attacker could access connected financial institutions.

**Target State**

```
Plaid Token
     |
Encryption Service
     |
Encrypted Database Value
```

**Implementation Goals**

Create: `dough/security/encryption.py`

Responsibilities:

- `encrypt()`
- `decrypt()`
- Validate encryption key
- Support future key rotation

Requirements:

- Never log decrypted tokens
- Never expose tokens in audit logs
- Never store encryption keys in the database

**Validation**

Tests should prove:

- Stored values are encrypted
- Sync can decrypt correctly
- Invalid keys fail safely
- Migration preserves existing connections

### 10.2 Production Secret Management — done, 2026-07-30 (Phase 10.5)

> Shipped as `config.REQUIRED_SECRETS`: a table of every secret with what it
> protects, whether production requires it, and the command that produces a
> value. `ProductionConfig.validate()` reads that table and reports **every**
> missing entry at once — an `if not X: raise` chain reports the first, the
> operator sets it and redeploys, and is told the second, which turns one
> five-minute fix into three deploys.
>
> Two decisions worth recording:
>
> - **Messages name variables, never values.** This runs at boot and its
>   exception goes to the log, so a validator that quoted what it found invalid
>   would put a real secret into the line reporting that a secret was wrong.
> - **Warnings are separate from errors.** `MAIL_BACKEND=console` in production,
>   a memory rate limiter, and an unset `PUBLIC_BASE_URL` are all reported at
>   WARNING and none of them stop a boot. Each has a legitimate use, and
>   refusing to start over a judgement call is how operators learn to set an
>   override variable — which then hides the checks that were not judgement
>   calls.
>
> `docs/security.md` has the table and the rotation notes;
> `tests/test_secret_hygiene.py` asserts the code, `.env.example` and the docs
> do not drift.

**Current State**

```
.env
  |
config.py
  |
Application
```

**Future State**

```
Secret Provider
      |
Configuration Layer
      |
Application
```

**Goals**

Create a secret abstraction layer.

Examples:

- Environment variables
- AWS Secrets Manager
- Azure Key Vault
- HashiCorp Vault

**Requirements**

Document:

- Secret creation
- Secret rotation
- Secret revocation
- Emergency replacement procedure

Secrets covered:

- Anthropic API key
- Plaid credentials
- Flask `SECRET_KEY`
- Database credentials
- Encryption keys

### 10.3 Session Security Lifecycle — done, 2026-07-30 (Phase 10.5)

> Built as described, and extended to API tokens. The one gap left is that
> nothing yet *changes* a password, so the counter has no user-facing trigger.
> See the note at the top of this phase.


**Objective**

Ensure sensitive account changes invalidate old sessions.

Review:

- Password changes
- Account deletion
- Household removal
- Role changes

**Recommended Approach**

Add `session_version` to users.

Example:

**User:**
- id
- username
- password_hash
- session_version

**Session:**
- session_version = 3

Changing password: `session_version += 1`

Old sessions automatically become invalid.

---

## Phase 11 — Database Production Readiness

### Objective

Prepare Dough for growth beyond SQLite.

### 11.1 SQLite Production Assessment

Review:

- SQLite locking behavior
- Concurrent writes
- Transaction contention
- Scheduler interactions

Document:

- Current limitations
- Future migration plan

### 11.2 PostgreSQL Migration Preparation

Do **not** immediately migrate.

Prepare:

- PostgreSQL development environment
- Alembic compatibility testing
- Schema comparison
- Performance testing

**Success criteria:**

SQLite schema = PostgreSQL schema for:

- Tables
- Constraints
- Indexes
- Relationships

---

## Phase 12 — Background Job Architecture

### Objective

Remove the single-worker production limitation.

**Current Architecture**

```
Flask
  |
Scheduler
  |
Plaid Sync
```

**Future Architecture**

```
Flask Application
       |
   Job Queue
       |
Worker Processes
```

**Candidate Technologies**

Evaluate:

- Celery
- RQ
- Dramatiq

**Jobs To Move**

- Plaid synchronization
- AI report generation
- Imports
- Notifications
- Scheduled insights

**Requirements**

Jobs must support:

- Retries
- Failures
- Idempotency
- Status tracking
- Audit history

---

## Phase 13 — Backup and Disaster Recovery

### Objective

Guarantee recovery from catastrophic failures.

**Backup Automation**

Create:

- Scheduled backups
- Encrypted backups
- Retention policies
- Verification checks

**Restore Drill**

Regularly test:

1. Restore backup
2. Apply migrations
3. Verify tenancy
4. Verify data integrity
5. Run application smoke tests

**Document:** Disaster Recovery Runbook

Include:

- Failed migration recovery
- Corrupted database recovery
- Credential compromise response

---

## Phase 14 — Observability and Monitoring

### Objective

Turn operational data into actionable visibility.

**Metrics To Track**

*Application*
- Request volume
- Errors
- Response times

*Security*
- Failed logins
- CSRF failures
- Throttling events
- Suspicious activity

*Financial Sync*
- Plaid failures
- Sync duration
- Connection health

*AI*
- Requests
- Latency
- Token usage
- Cache hit rate

**Current Foundation**

Already exists:

- Structured logs
- Trace IDs
- Health endpoints

**Next step:** Add dashboards.

---

## Phase 15 — Data Privacy Governance

### Objective

Define how financial information is handled throughout its lifecycle.

Create: `docs/privacy/data-policy.md`

Define:

**Data Retention**

Examples:

- Transactions: retention period
- Audit events: retention period
- AI conversations: retention policy

**Account Deletion**

Define workflow:

```
Delete Account
     |
Revoke financial connections
     |
Remove personal data
     |
Retain required audit records
```

---

## Phase 16 — AI Governance

### Objective

Make AI usage safe and predictable.

**AI Data Policy**

Document:

- What financial information is sent to AI providers
- Caching behavior
- Retention behavior
- User controls

**AI Usage Tracking**

Track:

- Household
- User
- Model
- Tokens
- Latency
- Cost estimate

**Cost Controls**

Potential additions:

- Daily token limits
- Monthly usage limits
- Household quotas

---

## Phase 17 — Security Automation

### Objective

Prevent regressions automatically.

**CI Enhancements**

Add:

*Dependency Security*
- pip-audit
- Dependabot

*Code Security*
- CodeQL
- Secret Detection
- GitHub secret scanning

**Security Regression Tests**

Maintain automated coverage for:

- Tenancy
- Authorization
- CSRF
- XSS
- Authentication
- Encryption

---

## Phase 18 — Mobile/API Readiness

> **Delivered as Phase 10, 2026-07-30.** Everything in this section shipped —
> `/api/v1/`, API authentication (opaque bearer tokens), versioning, and
> mobile-friendly responses. The one item that did *not* ship is rate limiting;
> it is SEC-0018 in `docs/security.md` and is grouped with Phase 16's AI cost
> controls, because both need the same shared counter that this application does
> not yet have. See the note at Phase 10 above.

### Objective

Prepare Dough for native mobile clients.

**API Architecture**

Move toward `/api/v1/`

Examples:

- `/api/v1/accounts`
- `/api/v1/transactions`
- `/api/v1/net-worth`
- `/api/v1/chat`

**Requirements**

Support:

- API authentication
- Versioning
- Rate limits
- Mobile-friendly responses

---

## Recommended Order After Phase 9

**First Wave — Security**
1. Plaid token encryption
2. Secret management
3. Session lifecycle controls

**Second Wave — Reliability**
1. Backup/disaster recovery
2. Observability
3. Background job architecture

**Third Wave — Scale**
1. PostgreSQL preparation
2. Database migration planning

**Fourth Wave — Product Maturity**
1. Privacy governance
2. AI governance
3. Security automation
4. Mobile API readiness

---

## Final Production Readiness Checklist

Before public launch, Dough should have:

**Security**
- [ ] Encrypted financial credentials
- [ ] Secret rotation process
- [ ] Session invalidation
- [ ] Security automation

**Data**
- [ ] Verified backups
- [ ] Restore drills
- [ ] Retention policy
- [ ] Deletion workflow

**Infrastructure**
- [ ] Background worker architecture
- [ ] Monitoring dashboards
- [ ] Production database strategy

**AI**
- [ ] Privacy documentation
- [ ] Usage tracking
- [ ] Cost controls

**Mobile**
- [ ] Versioned API layer

---

## End State Goal

Dough should transition from:

> "A very well architected personal finance application"

into:

> "A production-grade fintech platform capable of safely supporting multiple households, financial integrations, AI features, and future mobile clients."

---

