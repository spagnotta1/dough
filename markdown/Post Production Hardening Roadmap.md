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

### Objective

Protect sensitive financial information even if the database, backups, or credentials are compromised.

### 10.1 Encrypt Plaid Access Tokens

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

### 10.2 Production Secret Management

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

### 10.3 Session Security Lifecycle

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

