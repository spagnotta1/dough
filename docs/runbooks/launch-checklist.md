# Launch Checklist

Two gates, not one. **Invite-only** and **open registration** fail differently,
so they are checked separately — and most of the work between them is not code.

Everything here is either a command that prints something checkable or a
statement somebody has to be able to answer honestly. Where a check is automated
it says so; where it is a judgement call it says that too, because a checklist
whose items cannot fail is decoration.

The rule for both gates: **do not tick an item you have not seen work.** The
whole value of this document is that it is a record of what was actually
verified, not what was believed.

---

## Gate 1 — Invite-only beta

The audience is people you know, who will tell you when something is broken and
will forgive it. What has to be true is that their data is safe and recoverable.

### Data safety

- [ ] **`ENCRYPTION_KEY` is set in production and backed up somewhere off the
      host.** Losing it does not lose the data — it makes every stored
      institution token permanently unreadable, and every household must
      re-link every bank. Verify it is set:
      `railway variable list --json | grep ENCRYPTION_KEY`
- [ ] **`SECRET_KEY` is set and was generated fresh**, not copied from a working
      tree. A copy means anybody with that tree can forge sessions.
- [ ] **The database is on the volume**, not in the image:
      `DATABASE_URL=sqlite:////data/checkbook.db` — four slashes. Three is a
      relative path and the database is erased on the next deploy.
- [ ] **`UPLOAD_FOLDER=/data/uploads`**, for the same reason.
- [ ] **Backups are running.** `railway logs | grep dough.backup` shows
      `Backup ok` with a size and row count. The first appears about a minute
      after a deploy.
- [ ] **A backup has been restored at least once.** Not "backups exist" —
      restored. `docs/runbooks/disaster-recovery.md` is the procedure; the
      mechanism itself is covered on every commit by
      `tests/test_backup.py::test_a_backup_restores_after_the_database_is_destroyed`.
- [ ] **A copy of `/data/backups` is off the host.**
      `railway volume files --volume dough-volume download`. Until this is done
      the volume is a single point of total loss, and it is the one gap the
      backup scheduler deliberately does *not* close.

### Access

- [ ] **`ALLOW_REGISTRATION=0`.** This is what makes the gate invite-only.
      Pinned by `tests/test_identity.py` — a closed instance refuses a posted
      registration, not merely the form.
- [ ] **`APP_HTTPS=1`**, so the session cookie carries `Secure`.
- [ ] **`TRUSTED_PROXIES=1`** on Railway. At `0` every client looks like the
      proxy: one shared login-throttle bucket, and audit rows naming the proxy
      as the actor.
- [ ] **`PUBLIC_BASE_URL` is set.** Otherwise mail links are built from the
      client-controlled `Host` header.
- [ ] **You can invite somebody and they can join.** End to end, from a real
      inbox.

### Mail — the item most likely to be quietly broken

Mail failures here are silent by construction: `MAIL_BACKEND=console` *succeeds*,
and a pending Postmark account accepts an SMTP session and bounces the message
afterwards. Neither shows up as an error.

- [ ] **`MAIL_BACKEND` is not `console` in production.** `ProductionConfig.warnings`
      says so at boot; check the startup logs.
- [ ] **Postmark account approved.** Until it is, every recipient must share the
      `MAIL_FROM` domain, and anything else is refused per message.
      Check: `curl -s -H "X-Postmark-Server-Token: $TOKEN" https://api.postmarkapp.com/deliverystats`
- [ ] **Sender signature or domain verified** in the Postmark dashboard, and
      `MAIL_FROM` matches it. A wrong value is a send-time failure, not a
      startup one — the service boots clean and rejects every message.
- [ ] **A verification mail arrived at an address on a domain you do not own.**
      A Gmail account. This is the check that proves approval actually landed;
      sending to your own domain proves nothing.
- [ ] **A password reset arrived and worked**, end to end.
- [ ] `railway logs | grep dough.email` shows `Sent ... via postmark` — not
      `via console`.

### Monitoring

- [ ] **`SENTRY_DSN` is set** and the startup log says
      `Error reporting enabled`. If the SDK is missing you get a warning instead
      and the application still boots — check which one you got.
- [ ] **A deliberate error appeared in Sentry.** Cause one and look.
- [ ] **That event contains no financial data.** Open it and read it. The
      scrubber is covered by `tests/test_monitoring.py`, but this is the one
      place to confirm the shape of a real event from a real deployment.

### Legal — required even for a beta

Real bank connections and real money advice do not become exempt because the
user list is short.

- [ ] `LEGAL_ENTITY`, `LEGAL_CONTACT_EMAIL` and `LEGAL_JURISDICTION` are set.
      Unset renders a visible `[...]` marker on the live page.
- [ ] `/privacy` and `/terms` load signed out, and the markers are gone.
- [ ] **The privacy policy matches what the deployment actually does.** If AI is
      configured, the Anthropic section must be present — it renders
      conditionally on `ANTHROPIC_API_KEY`.

### Verified working

- [ ] `python -m pytest -q` is green on the deployed commit.
- [ ] `/health/ready` returns 200 against production.
- [ ] Export downloads a file with your data in it.
- [ ] Deletion works on a throwaway account — and the confirmation page
      correctly describes what will be removed.

---

## Gate 2 — Open registration

Everything above, still true, plus the things that only matter when the audience
is strangers rather than friends.

### The change itself

- [ ] **Legal counsel has reviewed `/privacy` and `/terms`.** They were written
      from the code, and every factual claim in them is checkable against a
      module — but they have not been reviewed by a lawyer, the liability and
      warranty sections are the ordinary shape of such clauses rather than
      advice, and this is the item that most needs somebody who is not an
      engineer.
- [ ] **Plaid production access granted.** `PLAID_ENV=production` with the
      matching secret. Their review requires a published privacy policy, which
      is why the item above comes first.
- [ ] **`PLAID_WEBHOOK_URL` set** to `https://<host>/api/plaid/webhook`. Not
      cosmetic: Plaid backfills an Item's history for minutes to hours after
      linking, and this is the only signal that it finished. Unset, a slow bank
      outlasts the retry schedule and the user keeps a fraction of their
      history — the UAT round 1 report. See docs/deploy-railway.md §8.
- [ ] **`ALLOW_REGISTRATION=1`** — the actual switch.

### Abuse, which is now somebody else's decision rather than yours

- [ ] **AI spend has a ceiling you have looked at.** 60 model calls per
      household per hour and 300 per day, enforced since Phase 10.6. Confirm the
      numbers still look right against your Anthropic bill from the beta, and
      remember what they are not: `MemoryBackend` resets on restart and does not
      span workers (SEC-0010).
- [ ] **You have a billing alert on the Anthropic account.** The rate limit
      bounds the per-household rate; it does not bound the number of households.
      This is the control that catches a signup flood.
- [ ] **`REQUIRE_EMAIL_VERIFICATION`** — decide deliberately. It is declared and
      **not wired**; setting it to `1` does nothing today. Wiring it is a
      one-line check in `dough/auth.py` and must not happen before mail is
      confirmed to deliver, because it is the one switch that can lock out every
      existing account at once.
- [ ] **A signup flood would be survivable.** Registration is limited to 5 per
      source address per hour — per address, so a distributed flood is not
      covered by it.

### Operational readiness

- [ ] **Somebody is watching Sentry**, and knows what to do about what they see.
- [ ] **The support address in the legal pages reaches somebody.** Send it a
      message.
- [ ] **You have written down what to do when a user asks for their data or
      asks to be deleted**, beyond "the buttons exist" — including a request
      arriving from somebody who cannot sign in.
- [ ] **The single-worker constraint is still honoured.** `--workers 1` in the
      Procfile. Adding workers for throughput silently duplicates the sync
      scheduler, the backup thread and every rate-limit counter (OPS-0012).

---

## Known and accepted at both gates

These are real, they are documented, and they are not blockers — but they should
be decisions rather than discoveries. Full reasoning in `docs/security.md`.

| | What | Why it is accepted |
|---|---|---|
| SEC-0007 | Tailwind loads from a CDN at runtime | A third-party script with DOM access on pages rendering financial data. Self-hosting it is the fix. |
| SEC-0010 | Rate-limit state is per-process and in memory | Correct for one worker, which is the documented deployment. Redis is the fix and is named in config. |
| SEC-0011 | An invitation link is a bearer credential | Single use, 72-hour expiry, revocable, stored hashed. A link forwarded to the wrong person is a full disclosure until revoked. |
| SEC-0017 | An API token has no second factor | Hashed at rest, scoped, revocable, audited. An exfiltrated one works until somebody notices. |
| SEC-0023 | A password-reset link is a bearer credential | The accepted floor of any email-based recovery. |
| OPS-0012 | The scheduler is per process | Single worker by decision. Enforced by nothing — see the last item of Gate 2. |
| OPS-0013 | The audit log has no retention policy | Grows without bound. It is also what survives an account deletion, so a retention policy is a product decision rather than a cleanup. |
| — | Backups are not off-site automatically | The largest remaining operational risk. Gate 1 requires doing it by hand. |
| — | Deleted accounts persist in audit rows and in backups | Disclosed in the privacy policy. Purging the audit trail would remove the record that the deletion happened. |
