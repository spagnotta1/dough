from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.query import Query
from sqlalchemy import Index

from dough.tenancy import TenantScopedMixin, apply_tenant_predicate


class TenantScopedQuery(Query):
    """Closes the one hole in the `do_orm_execute` tenant backstop.

    That event adds `household_id = <current>` to every statement whose
    `all_mappers` includes a tenant-scoped entity, which covers SELECT, bulk
    UPDATE, bulk DELETE and column expressions like `func.sum(Transaction.amount)`.

    It does not cover `Query.count()`, and the way it fails is quiet. `count()`
    goes through `_legacy_from_self`: it freezes the current query into a
    subquery and executes `SELECT count(*) FROM (<frozen>) AS anon_1`. By the
    time the event fires, the only entity left in the statement is the count
    column — `all_mappers` comes back **empty**, the handler concludes no tenant
    data is involved, and the frozen subquery inside is beyond the reach of
    `with_loader_criteria` anyway.

    The result was that `Transaction.query.filter_by(...).all()` returned one
    household's rows while `.count()` on the same query counted everybody's.
    Found by `test_both_households_can_import_the_identical_csv_row`, which is
    the only test that had both households holding a row matching the same
    filter — every other test would have counted the right number by accident.

    So `count()` applies the predicate itself, *before* the subquery is frozen.
    Set as `query_class`, which Flask-SQLAlchemy uses for both `Model.query` and
    `db.session.query(...)`.
    """

    def count(self):
        scoped = apply_tenant_predicate(
            self, [d['entity'] for d in self.column_descriptions])
        # Bound to `scoped`, not to self, and dispatched to the parent class so
        # this override does not call itself. `scoped` is another
        # TenantScopedQuery, so a plain `scoped.count()` would recurse forever.
        return super(TenantScopedQuery, scoped).count()


db = SQLAlchemy(query_class=TenantScopedQuery)


# ═══════════════════════════════════════════════════════════════════════════
# Tenancy  [Phase 5]
#
# `Household` is the tenant. It is not itself tenant-scoped -- it *is* the
# scope -- which is why it does not carry the mixin and why reading it is one
# of the three sanctioned uses of `dough.tenancy.unscoped()`.
#
# Every other model divides into three kinds, and the kind is a decision worth
# being able to see at a glance:
#
#   identity      AppUser -- carries a plain household_id, because login has to
#                 query it *before* a household is known. Filtering it through
#                 the ORM backstop would make signing in impossible.
#   tenant-scoped the fourteen models below inheriting TenantScopedMixin.
#   global        MarketPrice -- the closing price of VTI is not anybody's
#                 private data, one household's sync warms the cache for all,
#                 and scoping it would multiply identical rows by tenant count.
#
# Phase 8 adds a fourth kind, and it is the only one:
#
#   audited       AuditEvent -- a *nullable* household_id, scoped by its service
#                 rather than by the mixin. The reason is at the class.
# ═══════════════════════════════════════════════════════════════════════════

class Household(db.Model):
    """One tenant: a family's money, and everyone allowed to see it.

    Named for what it is rather than "account" or "organization" because this
    application's unit of isolation really is a household — two people sharing
    a checking account are one tenant, not two that federate.
    """
    __tablename__ = 'households'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    # Plaid issues a per-end-user identifier for its identity products. It is
    # a property of the household, not of a single connection: two institutions
    # linked by the same family share one. Nullable — households created before
    # a Plaid link exists have none.
    plaid_user_id = db.Column(db.String(80), nullable=True, unique=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    members = db.relationship('AppUser', backref='household', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        }


#: The two roles a member can hold. `owner` is the one that can never be
#: absent — `tools/verify_tenancy.py` asserts every household has at least one,
#: because a household with only members is one nobody can administer.
ROLE_OWNER = 'owner'
ROLE_MEMBER = 'member'


class AppUser(db.Model):
    """Login credentials, and the one household this person belongs to.

    Deliberately a foreign key rather than a membership join table. A join
    table models a user in several households, which is a product decision
    nobody has made; adding it later is a migration, whereas removing an
    unused one that authorization code has already learned to consult is not.
    """
    __tablename__ = 'app_users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    #: The address password recovery and verification are sent to.  [Phase 10.5]
    #:
    #: Nullable, and it has to be. Every account that existed before this column
    #: did was created through `/setup` or `/join`, neither of which asked for an
    #: address, and there is nothing truthful to backfill — an invented value
    #: would be a delivery address for a password reset, which is the one field
    #: where a wrong guess hands the account to whoever owns the address that was
    #: guessed. `/register` requires one; the older paths still do not.
    #:
    #: Unique, because it is a lookup key for `/forgot-password` and two accounts
    #: sharing one would make "which account did you mean" unanswerable. SQL
    #: treats NULLs as distinct from each other, so the constraint costs the
    #: address-less accounts nothing.
    #:
    #: Stored lowercased — see `dough.services.identity.normalize_email`. Storing
    #: what was typed would make the uniqueness constraint case-sensitive, so
    #: `Sam@x.com` and `sam@x.com` would be two accounts and the reset lookup
    #: would find whichever one was typed the same way twice.
    email = db.Column(db.String(255), nullable=True, unique=True)

    #: When the address above was proved reachable, or NULL if it never was.
    #:
    #: A timestamp rather than a boolean because "verified" is an event with a
    #: date an operator may need — a support question about an account
    #: compromised in March is a different question depending on whether the
    #: address was confirmed before or after. Nothing is gated on it today;
    #: `REQUIRE_EMAIL_VERIFICATION` is the switch that will be, and it is off.
    email_verified_at = db.Column(db.DateTime, nullable=True)

    # Not TenantScopedMixin: see the block comment above.
    household_id = db.Column(db.Integer, db.ForeignKey('households.id'),
                             nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default=ROLE_OWNER,
                     server_default=ROLE_OWNER)

    #: The generation counter every credential for this account is stamped with.
    #: Raising it invalidates all of them at once — the browser session, which
    #: carries the value it was signed in under, and every API token, which
    #: stores the value it was issued under. One number, both surfaces, no sweep
    #: over any table and nothing to keep in step.
    #:
    #: Nobody has to remember to raise it. A `before_flush` listener in
    #: `dough/auth.py` does it whenever `password_hash` changes, because the
    #: alternative — an explicit call at each site that changes a password — is
    #: a convention, and a forgotten convention here means old credentials keep
    #: working after the password they were obtained with has been replaced.
    #:
    #: The one exemption is the sign-in rehash, which changes the stored hash
    #: without changing the password; it is marked at the single place that
    #: performs it. See `dough.auth.upgrade_password_hash`.
    session_version = db.Column(db.Integer, nullable=False, default=1,
                                server_default='1')

    @property
    def is_owner(self):
        return self.role == ROLE_OWNER


class HouseholdInvite(TenantScopedMixin, db.Model):
    """A one-time link that lets one more person into this household.  [Phase 6]

    Tenant-scoped, so an owner listing invitations sees their own and the ORM
    backstop covers that listing like any other query. The *redemption* path is
    the exception and is the one place in the application that resolves a row
    before any household is known: whoever follows the link is anonymous, and
    the token is what says which household they are joining. `app.py` does that
    lookup inside `unscoped()` with the reasoning written at the call site.

    ## Why a hash and not the token

    An invitation link is a bearer credential — holding it is the whole
    authorization. Storing it in plaintext would mean a database file, a backup,
    or a stray `SELECT *` in a log yields working invitations into a household's
    finances. The hash is enough to answer "is this the link I issued?" and
    useless for making one.

    SHA-256 without a salt or a work factor, deliberately, which is the opposite
    of the advice for passwords and right for the same reason it is wrong there:
    the input is 256 bits of `secrets.token_urlsafe`, not something a person
    chose, so there is no dictionary to precompute and nothing for a slow KDF to
    protect against. A work factor here would only make redemption slow.
    """
    __tablename__ = 'household_invites'

    id = db.Column(db.Integer, primary_key=True)
    # Unique across the installation, not per household: two households must
    # never be able to issue colliding links, and the lookup happens before any
    # household is known, so a per-household constraint could not be enforced
    # at the point it matters.
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    role = db.Column(db.String(20), nullable=False, default=ROLE_MEMBER,
                     server_default=ROLE_MEMBER)
    # The owner's own note about who a link was meant for. Free text, never
    # matched against anything — an invitation is bearer-authorized, so binding
    # it to a name here would imply a check that does not exist.
    label = db.Column(db.String(120), nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey('app_users.id'),
                              nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    accepted_at = db.Column(db.DateTime, nullable=True)
    accepted_by_id = db.Column(db.Integer, db.ForeignKey('app_users.id'),
                               nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    created_by = db.relationship('AppUser', foreign_keys=[created_by_id])
    accepted_by = db.relationship('AppUser', foreign_keys=[accepted_by_id])

    def state(self, now=None):
        """`'accepted'`, `'revoked'`, `'expired'` or `'pending'`.

        Order matters and it is not alphabetical: an accepted invitation that
        has since passed its expiry is *accepted*, and reporting it as expired
        would hide the fact that somebody used it.
        """
        now = now or datetime.utcnow()
        if self.accepted_at:
            return 'accepted'
        if self.revoked_at:
            return 'revoked'
        if self.expires_at <= now:
            return 'expired'
        return 'pending'

    @property
    def is_redeemable(self):
        return self.state() == 'pending'


class ApiToken(db.Model):
    """A bearer credential a non-browser client authenticates with.  [Phase 10]

    ## Why this exists rather than reusing the session cookie

    The session is a signed cookie plus a CSRF token bound to it, and both of
    those are browser mechanisms. A native client would have to hold a cookie
    jar, scrape a token out of an HTML page, and replay both — which is a
    browser emulator, not an API client. More importantly the session has no
    revocation story short of rotating `SECRET_KEY` and signing everybody out,
    and "the phone was stolen" must not be an event that logs the family's
    desktop out too.

    ## Not `TenantScopedMixin`, for the same reason as `AppUser`

    The lookup runs *before* any household is bound — the token is what says
    which household the request belongs to. Filtering this table through the ORM
    backstop would make authenticating impossible, exactly as it would for
    login. So `household_id` is a plain indexed foreign key and the isolation
    guarantee comes from `dough/services/api_tokens.py`, which is the only
    module that reads this table and always states the predicate itself.

    `tools/verify_tenancy.py` lists the table with that reason attached, so the
    exception is reviewed rather than assumed.

    ## Why the hash and not the token

    Identical reasoning to `HouseholdInvite`, and it is the same shape of
    credential: holding the string *is* the authorization. Storing plaintext
    would mean a database file, a backup, or a stray `SELECT *` in a log yields
    working access to a household's finances.

    SHA-256, unsalted, no work factor — deliberately, and for the reason that
    makes it wrong for passwords: the input is 256 bits from
    `secrets.token_urlsafe`, so there is no dictionary to precompute and nothing
    a slow KDF would protect against. A work factor here would only tax every
    authenticated request, which is the one place in this application where a
    per-request cost is actually paid on every request.

    ## `prefix`

    The first characters of the token, stored in clear. It is not a credential —
    it identifies which row a token belongs to for the revocation UI, so
    somebody looking at three tokens can tell which one is on the old phone.
    Short enough to be useless for guessing the remaining entropy.
    """
    __tablename__ = 'api_tokens'

    id = db.Column(db.Integer, primary_key=True)

    # Not TenantScopedMixin: see the class docstring.
    household_id = db.Column(db.Integer, db.ForeignKey('households.id'),
                             nullable=False, index=True)
    # Which person the token acts as. Role checks (`@owner_required`) resolve
    # through this user, so a token can never do more than the human who issued
    # it — and a demoted owner's tokens are demoted with them, because the role
    # is read from `app_users` at request time rather than copied here.
    user_id = db.Column(db.Integer, db.ForeignKey('app_users.id'),
                        nullable=False, index=True)

    #: The owner's own label: "iPhone", "shortcuts". Never matched against
    #: anything.
    name = db.Column(db.String(80), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    prefix = db.Column(db.String(20), nullable=False)

    #: Comma-separated, from `api_tokens.VALID_SCOPES`. A closed vocabulary for
    #: the same reason the audit event types are one: `'read'`, `'Read'` and
    #: `'read '` must not be three different permissions.
    #:
    #: `server_default` as well as `default`, matching every other column in
    #: this file that has one. `tests/test_migrations.py` compares the schema
    #: `create_all()` builds against the one the migration chain builds, and a
    #: Python-side default alone would leave the two disagreeing -- which is the
    #: drift that test exists to catch.
    scopes = db.Column(db.String(120), nullable=False, default='read',
                       server_default='read')

    #: `AppUser.session_version` as it stood when this token was issued. A
    #: mismatch means the account's credentials have been invalidated since —
    #: today, that a password was changed — and `authenticate()` refuses the
    #: token without anything having had to sweep this table.
    #:
    #: Copied rather than joined-and-compared at issue time so the check costs
    #: nothing: `authenticate()` already loads the user row on every request, for
    #: the separate reason that a demoted owner's tokens must demote with them.
    #: Lazy invalidation rather than eager revocation is also what makes the
    #: guarantee hold under a partial failure — there is no second write that
    #: could be lost, so a token cannot survive by having been missed.
    session_version = db.Column(db.Integer, nullable=False, default=1,
                                server_default='1')

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    #: Nullable means "does not expire". A token with no expiry is a deliberate
    #: choice an operator can make, not the only thing the schema can express.
    expires_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    #: Written coarsely — see `api_tokens.touch`. This is the field that answers
    #: "which of these can I safely delete?", which is the question that decides
    #: whether anybody ever prunes them.
    last_used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('AppUser', foreign_keys=[user_id])

    #: Every value `state()` can return, in the order it decides them. A closed
    #: vocabulary for the same reason `VALID_SCOPES` is one, and pinned against
    #: the OpenAPI enum by `tests/test_openapi.py` — a fifth state added here
    #: without the spec catching up is a client that meets a value its own
    #: types say cannot exist.
    STATES = ('revoked', 'stale', 'expired', 'active')

    def scope_list(self):
        return [s for s in (self.scopes or '').split(',') if s]

    def has_scope(self, scope):
        return scope in self.scope_list()

    def state(self, now=None):
        """`'revoked'`, `'stale'`, `'expired'` or `'active'`.

        Deliberate acts are reported before incidental ones, which is why
        revoked and stale both come before expired: a token somebody withdrew,
        or one that a password change invalidated, should say so rather than
        report the fact that it would have lapsed anyway. Between the two,
        revocation names *this token* and staleness names the account.

        `'stale'` also covers a token whose user has been deleted — a removed
        member's rows are not cleaned up, and `authenticate` refuses those
        (as `'orphaned'`), so listing one as active would be a lie about a
        credential that no longer works.
        """
        now = now or datetime.utcnow()
        if self.revoked_at:
            return 'revoked'
        # A lazy load per token. The list is per household and single-digit in
        # practice, and the alternative -- threading the user's version in from
        # every caller -- is a parameter that a serializer could omit, which
        # would make this read 'active' at exactly the moment it must not.
        if self.user is None or self.user.session_version != self.session_version:
            return 'stale'
        if self.expires_at and self.expires_at <= now:
            return 'expired'
        return 'active'

    @property
    def is_usable(self):
        return self.state() == 'active'

    def to_dict(self):
        """Everything about the token except the token.

        There is no branch here that could ever include the plaintext, which is
        the point: `issue()` returns it separately and exactly once, so no
        serializer can leak it into a list response by accident.
        """
        return {
            'id': self.id,
            'name': self.name,
            'prefix': self.prefix,
            'scopes': self.scope_list(),
            'state': self.state(),
            'created_at': self.created_at.isoformat() + 'Z',
            'expires_at': self.expires_at.isoformat() + 'Z' if self.expires_at else None,
            'last_used_at': self.last_used_at.isoformat() + 'Z' if self.last_used_at else None,
            'revoked_at': self.revoked_at.isoformat() + 'Z' if self.revoked_at else None,
        }


#: What an `EmailVerification` row entitles its holder to do. A closed
#: vocabulary for the same reason `AUDIT_EVENT_TYPES` and `VALID_SCOPES` are
#: closed, and here the reason is not tidiness: `purpose` is what stops a link
#: that proves an address is reachable from also being a link that sets a
#: password. A typo in a string comparison would silently merge the two.
PURPOSE_VERIFY_EMAIL = 'verify_email'
PURPOSE_PASSWORD_RESET = 'password_reset'
VERIFICATION_PURPOSES = (PURPOSE_VERIFY_EMAIL, PURPOSE_PASSWORD_RESET)


class EmailVerification(db.Model):
    """A single-use, expiring token mailed to an address.  [Phase 10.5]

    Two things use this table and they are told apart by `purpose`: proving an
    address is reachable, and authorizing a password reset.

    ## Why one table and not two

    The listed fields for both are identical — a hash, an expiry, a single-use
    stamp — and so is every rule about them. Two tables would mean two
    implementations of "is this token still redeemable?", and the failure mode of
    the duplicate is specific rather than aesthetic: the second copy is the one
    that gets the expiry check right and the single-use check subtly wrong,
    because it was written by someone reading the first copy rather than the
    requirement. `redeem()` in `dough/services/identity.py` is that logic, once,
    and `purpose` is an argument to it — so asking for a reset token and being
    handed a verification token is a lookup that returns nothing rather than a
    comparison somebody forgot to write.

    ## Not `TenantScopedMixin`, for the third time in this file

    `AppUser` and `ApiToken` give the same reason and it applies most strongly
    here: whoever follows a reset link is, by definition, someone who cannot sign
    in. There is no session, no household bound, and no possibility of one — the
    token is the only thing identifying anybody. A scoped query would find
    nothing and password reset would be impossible to implement.

    The isolation guarantee is therefore the same as `api_tokens`': one module
    reads this table (`dough/services/identity.py`), and it always states its own
    predicate. `tools/verify_tenancy.py` lists the table with that reason
    attached, so the exception is reviewed rather than assumed.

    ## Why the hash and not the token

    Third time for this reasoning too, and it has not changed: holding the string
    *is* the authorization. A password-reset token in a database file, a backup,
    or a stray `SELECT *` in a log is a working takeover of the account it names
    — strictly worse than a leaked session, because using it also locks the real
    owner out.

    SHA-256, unsalted, no work factor, for the reason that makes that wrong for
    passwords: the input is 256 bits from `secrets.token_urlsafe`, so there is no
    dictionary to precompute and nothing a slow KDF would protect against.
    """
    __tablename__ = 'email_verifications'

    id = db.Column(db.Integer, primary_key=True)

    # Not TenantScopedMixin: see the class docstring. `ondelete` is deliberately
    # absent for the same reason it is on `audit_events` -- but the opposite
    # conclusion applies, so it is stated: a token whose user is gone must not
    # resolve, and `redeem()` refuses one whose user no longer loads rather than
    # relying on the database to have swept it.
    user_id = db.Column(db.Integer, db.ForeignKey('app_users.id'),
                        nullable=False, index=True)

    # Unique across the installation, exactly like `household_invites.token_hash`
    # and for the same reason: the lookup happens before any household is known,
    # so a per-household constraint could not be enforced at the point it matters.
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)

    #: One of `VERIFICATION_PURPOSES`.
    purpose = db.Column(db.String(20), nullable=False,
                        default=PURPOSE_VERIFY_EMAIL,
                        server_default=PURPOSE_VERIFY_EMAIL)

    #: The address the token was actually sent to, as it stood at issue.
    #:
    #: Recorded rather than read back off the user because the two can disagree:
    #: someone requests a reset, changes their address, then follows the old
    #: link. Redeeming it would then act on an address its holder never proved
    #: they control. `redeem()` compares the two and refuses a mismatch.
    sent_to = db.Column(db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    #: Set the moment the token is spent. NOT NULL means used; this is what
    #: makes it single-use, and `redeem()` writes it in the same transaction as
    #: the effect, so a token cannot be spent twice by a request that raced.
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('AppUser', foreign_keys=[user_id])

    def state(self, now=None):
        """`'used'`, `'expired'` or `'pending'`.

        Used is decided before expired, matching `HouseholdInvite.state` and for
        the same reason: a spent token that has since lapsed was *spent*, and
        reporting it as expired would hide the fact that somebody used it.
        """
        now = now or datetime.utcnow()
        if self.used_at:
            return 'used'
        if self.expires_at <= now:
            return 'expired'
        return 'pending'

    @property
    def is_redeemable(self):
        return self.state() == 'pending'


class Transaction(TenantScopedMixin, db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    account_name = db.Column(db.String(50), nullable=False)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='Uncategorized')
    imported_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    anomaly_score = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    import_batch_id = db.Column(db.String(36), nullable=True, index=True)
    anomaly_reviewed = db.Column(db.Boolean, nullable=False, default=False, server_default='0')

    # --- synchronization fields (populated by finance_sync) ---
    source = db.Column(db.String(10), nullable=False, default='csv', server_default='csv')  # 'csv' | 'sync' | 'manual'
    account_id = db.Column(db.Integer, db.ForeignKey('financial_accounts.id'), nullable=True, index=True)
    external_id = db.Column(db.String(120), nullable=True)

    # Unique indexes prevent duplicates for both CSV imports (by content)
    # and synced imports (by provider transaction ID).
    #
    # The content index leads with household_id, and must. repository.py dedupes
    # CSV imports by catching IntegrityError on this index inside begin_nested();
    # without the household column, the second family to import a $15.99 Netflix
    # charge on the same date silently loses the row, and the data loss looks
    # exactly like the dedupe feature working correctly.
    #
    # idx_transaction_external_unique is left alone on purpose: account_id
    # already resolves to exactly one household through financial_accounts, so
    # adding the column would constrain nothing that is not already constrained.
    __table_args__ = (
        Index('idx_transaction_unique',
              'household_id', 'account_name', 'date', 'description', 'amount',
              unique=True),
        Index('idx_transaction_external_unique', 'account_id', 'external_id', unique=True),
    )

    def __repr__(self):
        return f'<Transaction {self.id}: {self.date} {self.description} {self.amount}>'


class Budget(TenantScopedMixin, db.Model):
    __tablename__ = 'budgets'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    account_name = db.Column(db.String(50), nullable=False, default='both')
    monthly_limit = db.Column(db.Numeric(10, 2), nullable=False)

    __table_args__ = (
        Index('idx_budget_unique', 'household_id', 'category', 'account_name',
              unique=True),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'category': self.category,
            'account_name': self.account_name,
            'monthly_limit': float(self.monthly_limit),
        } 

class CategoryRule(TenantScopedMixin, db.Model):
    """One keyword-or-pattern that assigns a category. [Phase 11A.1]

    ## Why this table exists

    Category rules lived in `category_rules.json` — **one file at the repo
    root**, read and written by every household in the installation. There was
    no `household_id` anywhere in `rules.py`, `categorization.py` or the Rules
    page, so this was not a leak through a missing filter: the rules were never
    tenanted at all, and the second household to sign in saw the first
    household's rule set because there was only ever one.

    That is a disclosure of personal financial data, not just untidy state. A
    rule set names the merchants somebody actually pays — their gym, their
    student-loan servicer, their subscriptions. `/planet fitness|gym
    membership/` in one family's rules tells another family where they exercise.

    The same file was also writable by the test suite, which is how
    `BrowserTestCategory` came to be sitting in a developer's real rules.

    ## Shape

    One row per (category, keyword) pair rather than a JSON blob per household.
    That is what makes "delete this one keyword" a `DELETE` of one row instead
    of a read-modify-write of a document, and it lets `position` order the rules
    without rewriting every one of them when a row moves.

    `position` is per household and ascending — **lower wins**, matching the
    Rules page's "rules higher in the list win". Ties break on `id`, so two
    rules written in one request stay in insertion order.

    The unique index leads with `household_id`, for the reason
    `idx_transaction_unique` does: without it the second household to add
    `STARBUCKS` to `Coffee` collides with the first, and the failure looks like
    a duplicate-rule error rather than the tenancy bug it is.
    """

    __tablename__ = 'category_rules'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    #: A plain substring, or a regex when wrapped in slashes — `/amazon|amzn/`.
    #: Stored exactly as typed; `rules.py` owns what the slashes mean.
    keyword = db.Column(db.String(200), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0,
                         server_default='0')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_category_rule_unique', 'household_id', 'category', 'keyword',
              unique=True),
        Index('idx_category_rule_order', 'household_id', 'position'),
    )

    def to_dict(self):
        return {'id': self.id, 'category': self.category,
                'keyword': self.keyword, 'position': self.position}

    def __repr__(self):
        return f'<CategoryRule {self.category}: {self.keyword!r}>'


class LogEntry(TenantScopedMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_type = db.Column(db.String(50), nullable=False)  # 'checking' or 'savings'
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    cleared = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Balance fields
    starting_balance = db.Column(db.Float, nullable=False)
    pending_total = db.Column(db.Float, nullable=False)
    cleared_balance = db.Column(db.Float, nullable=False)
    available_balance = db.Column(db.Float, nullable=False)

    def to_dict(self):
        return {
            'id': str(self.id),
            'account_type': self.account_type,
            'date': self.date.strftime('%Y-%m-%d'),
            'description': self.description,
            'amount': self.amount,
            'cleared': self.cleared,
            'starting_balance': self.starting_balance,
            'pending_total': self.pending_total,
            'cleared_balance': self.cleared_balance,
            'available_balance': self.available_balance
        }

class AccountBalance(TenantScopedMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Was `unique=True`. A column-level UNIQUE becomes an autoindex inside the
    # CREATE TABLE, so removing it needs a table rebuild -- see the migration.
    # Left global, it would let exactly one household in the installation
    # record a manual checking balance.
    account_type = db.Column(db.String(50), nullable=False)  # 'checking' or 'savings'
    starting_balance = db.Column(db.Float, nullable=False, default=0)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_account_balance_unique', 'household_id', 'account_type',
              unique=True),
    )

    def to_dict(self):
        return {
            'account_type': self.account_type,
            'starting_balance': self.starting_balance,
            'last_updated': self.last_updated.strftime('%Y-%m-%d %H:%M:%S')
        }


class Conversation(TenantScopedMixin, db.Model):
    __tablename__ = 'conversations'

    id         = db.Column(db.String(36), primary_key=True)
    title      = db.Column(db.String(80), nullable=False, default='New Chat')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ChatMessage(TenantScopedMixin, db.Model):
    __tablename__ = 'chat_messages'

    id         = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), nullable=False, index=True)
    role       = db.Column(db.String(20), nullable=False)   # 'user' | 'assistant'
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class RecurringDismissal(TenantScopedMixin, db.Model):
    """A detected recurring bill/subscription the user has marked as not
    actually recurring. Matched against detected groups by normalized
    description (see recurring.py), so it survives re-detection."""
    __tablename__ = 'recurring_dismissals'

    id          = db.Column(db.Integer, primary_key=True)
    desc_key    = db.Column(db.String(255), nullable=False)  # normalized description
    description = db.Column(db.String(255), nullable=False)  # as displayed when dismissed
    kind        = db.Column(db.String(20), nullable=False, default='subscription')  # 'bill' | 'subscription'
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Was a global UNIQUE on desc_key, which would have meant one household
    # dismissing "NETFLIX" silently un-dismissable by any other.
    __table_args__ = (
        Index('idx_recurring_dismissal_unique', 'household_id', 'desc_key',
              unique=True),
    )


class Holding(TenantScopedMixin, db.Model):
    __tablename__ = 'holdings'

    id            = db.Column(db.Integer, primary_key=True)
    ticker        = db.Column(db.String(20), nullable=False)
    name          = db.Column(db.String(100), nullable=False)
    shares        = db.Column(db.Numeric(14, 6), nullable=False, default=0)
    current_value = db.Column(db.Numeric(12, 2), nullable=False)
    asset_class   = db.Column(db.String(20), nullable=False, default='Stock')
    account_name  = db.Column(db.String(50), nullable=False, default='Brokerage')
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- synchronization fields (populated by finance_sync) ---
    source         = db.Column(db.String(10), nullable=False, default='manual', server_default='manual')  # 'manual' | 'sync'
    account_id     = db.Column(db.Integer, db.ForeignKey('financial_accounts.id'), nullable=True, index=True)
    external_id    = db.Column(db.String(120), nullable=True)
    avg_cost       = db.Column(db.Numeric(14, 4), nullable=True)   # per-share cost basis
    current_price  = db.Column(db.Numeric(14, 4), nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        Index('idx_holding_sync_unique', 'account_id', 'ticker', unique=True),
    )

    @property
    def cost_basis(self):
        if self.avg_cost is None or self.shares is None:
            return None
        return round(float(self.avg_cost) * float(self.shares), 2)

    @property
    def gain_loss(self):
        basis = self.cost_basis
        if basis is None:
            return None
        return round(float(self.current_value) - basis, 2)

    @property
    def gain_pct(self):
        basis = self.cost_basis
        if not basis:
            return None
        return round((float(self.current_value) - basis) / basis * 100, 2)

    def to_dict(self):
        return {
            'id': self.id,
            'ticker': self.ticker,
            'name': self.name,
            'shares': float(self.shares),
            'current_value': float(self.current_value),
            'asset_class': self.asset_class,
            'account_name': self.account_name,
            'updated_at': self.updated_at.strftime('%Y-%m-%d %H:%M') if self.updated_at else None,
            'source': self.source,
            'account_id': self.account_id,
            'avg_cost': float(self.avg_cost) if self.avg_cost is not None else None,
            'current_price': float(self.current_price) if self.current_price is not None else None,
            'cost_basis': self.cost_basis,
            'gain_loss': self.gain_loss,
            'gain_pct': self.gain_pct,
            'last_synced_at': self.last_synced_at.strftime('%Y-%m-%d %H:%M') if self.last_synced_at else None,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Financial institution synchronization (finance_sync)
# ═══════════════════════════════════════════════════════════════════════════

class InstitutionConnection(TenantScopedMixin, db.Model):
    """A user's connection to a financial institution.

    Most adapters have exactly one connection per institution slug. Aggregator
    adapters (e.g. Plaid) can have many — one per linked "Item" — distinguished
    by ``item_id``. ``UNIQUE(institution, item_id)`` still lets single-item
    adapters behave as before: SQL treats NULL as distinct from every other
    NULL, so multiple rows sharing an institution with ``item_id IS NULL``
    would technically be permitted at the DB level, but application logic
    (``ConnectionService``) never creates more than one for those adapters.
    """
    __tablename__ = 'connected_accounts'

    id               = db.Column(db.Integer, primary_key=True)
    institution      = db.Column(db.String(40), nullable=False)  # adapter slug
    item_id          = db.Column(db.String(80), nullable=True)  # aggregator item id (Plaid); NULL for single-item adapters
    display_name     = db.Column(db.String(80), nullable=False)
    status           = db.Column(db.String(20), nullable=False, default='connected')  # connected | error | expired | disconnected
    auth_blob        = db.Column(db.Text, nullable=True)  # encrypted OAuth/API tokens (never usernames/passwords)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    last_sync_at     = db.Column(db.DateTime, nullable=True)
    last_sync_status = db.Column(db.String(20), nullable=True)  # success | partial | error
    last_error       = db.Column(db.Text, nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # household_id leads: without it, the second family to link Chase collides
    # with the first, and the connect flow fails with an IntegrityError that
    # reads like a Plaid problem.
    __table_args__ = (
        Index('uq_institution_item', 'household_id', 'institution', 'item_id',
              unique=True),
    )

    accounts = db.relationship('FinancialAccount', backref='connection',
                               cascade='all, delete-orphan', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'institution': self.institution,
            'item_id': self.item_id,
            'display_name': self.display_name,
            'status': self.status,
            'last_sync_at': self.last_sync_at.strftime('%Y-%m-%d %H:%M:%S') if self.last_sync_at else None,
            'last_sync_status': self.last_sync_status,
            'last_error': self.last_error,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'account_count': len(self.accounts),
        }


class FinancialAccount(TenantScopedMixin, db.Model):
    """A synchronized account (checking, savings, brokerage, crypto) at an institution."""
    __tablename__ = 'financial_accounts'

    id                = db.Column(db.Integer, primary_key=True)
    connection_id     = db.Column(db.Integer, db.ForeignKey('connected_accounts.id'), nullable=False, index=True)
    external_id       = db.Column(db.String(120), nullable=False)
    name              = db.Column(db.String(120), nullable=False)
    account_type      = db.Column(db.String(20), nullable=False)  # checking | savings | brokerage | crypto | credit | other
    currency          = db.Column(db.String(10), nullable=False, default='USD')
    mask              = db.Column(db.String(10), nullable=True)
    balance           = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    available_balance = db.Column(db.Numeric(14, 2), nullable=True)
    is_active         = db.Column(db.Boolean, nullable=False, default=True)
    last_synced_at    = db.Column(db.DateTime, nullable=True)
    created_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_finacct_unique', 'connection_id', 'external_id', unique=True),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'connection_id': self.connection_id,
            'institution': self.connection.institution if self.connection else None,
            'name': self.name,
            'account_type': self.account_type,
            'currency': self.currency,
            'mask': self.mask,
            'balance': float(self.balance),
            'available_balance': float(self.available_balance) if self.available_balance is not None else None,
            'is_active': self.is_active,
            'last_synced_at': self.last_synced_at.strftime('%Y-%m-%d %H:%M:%S') if self.last_synced_at else None,
        }


class SyncRun(TenantScopedMixin, db.Model):
    """One synchronization attempt (per connection, or engine-wide)."""
    __tablename__ = 'sync_history'

    id                   = db.Column(db.Integer, primary_key=True)
    connection_id        = db.Column(db.Integer, db.ForeignKey('connected_accounts.id'), nullable=True, index=True)
    institution          = db.Column(db.String(40), nullable=True)  # denormalized for display after disconnect
    trigger              = db.Column(db.String(20), nullable=False, default='manual')  # manual | scheduled | connect
    status               = db.Column(db.String(20), nullable=False, default='running')  # running | success | partial | error
    started_at           = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at          = db.Column(db.DateTime, nullable=True)
    accounts_synced      = db.Column(db.Integer, nullable=False, default=0)
    balances_updated     = db.Column(db.Integer, nullable=False, default=0)
    holdings_synced      = db.Column(db.Integer, nullable=False, default=0)
    transactions_added   = db.Column(db.Integer, nullable=False, default=0)
    transactions_skipped = db.Column(db.Integer, nullable=False, default=0)
    error_message        = db.Column(db.Text, nullable=True)

    errors = db.relationship('SyncErrorLog', backref='run', cascade='all, delete-orphan', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'connection_id': self.connection_id,
            'institution': self.institution,
            'trigger': self.trigger,
            'status': self.status,
            'started_at': self.started_at.strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': self.finished_at.strftime('%Y-%m-%d %H:%M:%S') if self.finished_at else None,
            'accounts_synced': self.accounts_synced,
            'balances_updated': self.balances_updated,
            'holdings_synced': self.holdings_synced,
            'transactions_added': self.transactions_added,
            'transactions_skipped': self.transactions_skipped,
            'error_message': self.error_message,
            'errors': [e.to_dict() for e in self.errors],
        }


class SyncErrorLog(TenantScopedMixin, db.Model):
    """Individual errors captured during a sync run."""
    __tablename__ = 'sync_errors'

    id            = db.Column(db.Integer, primary_key=True)
    run_id        = db.Column(db.Integer, db.ForeignKey('sync_history.id'), nullable=True, index=True)
    connection_id = db.Column(db.Integer, nullable=True)
    institution   = db.Column(db.String(40), nullable=True)
    error_type    = db.Column(db.String(40), nullable=False, default='sync_error')
    message       = db.Column(db.Text, nullable=False)
    is_transient  = db.Column(db.Boolean, nullable=False, default=False)
    attempt       = db.Column(db.Integer, nullable=False, default=1)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'institution': self.institution,
            'error_type': self.error_type,
            'message': self.message,
            'is_transient': self.is_transient,
            'attempt': self.attempt,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        }


class PortfolioSnapshotRow(TenantScopedMixin, db.Model):
    """Daily net-worth snapshot written after each sync (one row per day)."""
    __tablename__ = 'portfolio_snapshots'

    id                = db.Column(db.Integer, primary_key=True)
    # Was globally unique on the date, which under tenancy means the first
    # household to sync each morning is the only one that gets a snapshot --
    # and every other household's net-worth chart quietly stops advancing.
    snapshot_date     = db.Column(db.Date, nullable=False)
    checking          = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    savings           = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total_cash        = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    brokerage         = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    crypto            = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total_investments = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    net_worth         = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    created_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_portfolio_snapshot_unique', 'household_id', 'snapshot_date',
              unique=True),
    )

    def to_dict(self):
        return {
            'date': self.snapshot_date.strftime('%Y-%m-%d'),
            'checking': float(self.checking),
            'savings': float(self.savings),
            'total_cash': float(self.total_cash),
            'brokerage': float(self.brokerage),
            'crypto': float(self.crypto),
            'total_investments': float(self.total_investments),
            'net_worth': float(self.net_worth),
        }


class MarketPrice(db.Model):
    """Latest known market price per symbol (updated on every sync).

    Deliberately *not* tenant-scoped. A share of VTI is worth the same to
    everyone, so this is a shared cache rather than anybody's private data:
    scoping it would store one identical row per household and make each
    household wait for its own sync to learn a public number. Nothing here
    reveals a position — only that some household somewhere holds the symbol,
    which is why `source` records an institution slug and not a connection id.
    """
    __tablename__ = 'market_prices'

    id          = db.Column(db.Integer, primary_key=True)
    symbol      = db.Column(db.String(20), nullable=False, unique=True)
    name        = db.Column(db.String(100), nullable=True)
    price       = db.Column(db.Numeric(14, 4), nullable=False)
    currency    = db.Column(db.String(10), nullable=False, default='USD')
    asset_class = db.Column(db.String(20), nullable=True)
    source      = db.Column(db.String(40), nullable=True)  # institution slug that reported it
    as_of       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            'symbol': self.symbol,
            'name': self.name,
            'price': float(self.price),
            'currency': self.currency,
            'asset_class': self.asset_class,
            'source': self.source,
            'as_of': self.as_of.strftime('%Y-%m-%d %H:%M:%S'),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Auditing  [Phase 8]
# ═══════════════════════════════════════════════════════════════════════════

#: Event types. A closed vocabulary rather than free text, because the whole
#: value of an audit log is being able to ask "every role change in March" and
#: get an answer -- which a column holding `role_changed`, `role change` and
#: `ROLE_CHANGED` cannot give. `dough/services/audit.py` rejects anything not
#: listed here, and `tests/test_audit.py` asserts every constant is reachable
#: from some call site rather than merely declared.
EVENT_LOGIN_SUCCEEDED      = 'auth.login.succeeded'
EVENT_LOGIN_FAILED         = 'auth.login.failed'
EVENT_LOGIN_THROTTLED      = 'auth.login.throttled'
EVENT_LOGOUT               = 'auth.logout'
EVENT_PASSWORD_REHASHED    = 'auth.password.rehashed'
EVENT_SETUP_COMPLETED      = 'auth.setup.completed'

#: Phase 10.5. The identity lifecycle: an account coming into existence, an
#: address being proved, a password being replaced, and every credential the
#: account holds being invalidated.
#:
#: `auth.password.changed` and `auth.password.reset.completed` are two events and
#: not one with a flag, because the questions asked of them differ. "Was this
#: password change made by somebody who was already signed in?" is the first
#: thing anybody asks about a suspected takeover, and a flag inside
#: `metadata_json` is not something you can filter a table on.
#:
#: `auth.password.reset.requested` is recorded for an address that matches no
#: account as well as one that does — with `user_exists` in the metadata,
#: exactly as `auth.login.failed` already records it. That fact must not reach
#: the *response* (see `dough/blueprints/auth.py`), but it is precisely what an
#: operator needs to see somebody walking a list of addresses.
EVENT_REGISTERED           = 'auth.register.completed'
EVENT_PASSWORD_CHANGED     = 'auth.password.changed'
EVENT_PASSWORD_RESET_REQUESTED = 'auth.password.reset.requested'
EVENT_PASSWORD_RESET_COMPLETED = 'auth.password.reset.completed'
EVENT_EMAIL_VERIFICATION_SENT  = 'auth.email.verification.sent'
EVENT_EMAIL_VERIFIED       = 'auth.email.verified'
#: Every credential invalidated at once, by an explicit act rather than as the
#: side effect of a password change. This is the one the "sign out everywhere"
#: button records.
EVENT_SESSIONS_REVOKED     = 'auth.sessions.revoked'
#: A request refused by `dough/services/ratelimit.py`. Distinct from
#: `auth.login.throttled`, which names the *login* buckets specifically; this one
#: carries the policy name, so an AI-budget refusal and a password-reset flood
#: are the same event type with different metadata rather than two more
#: constants that would need adding for every policy.
EVENT_RATE_LIMITED         = 'security.rate_limited'

#: Phase 10. An API token is a credential that outlives the session that
#: created it, so its whole lifecycle is auditable — issuing one is the moment a
#: second way into the household's data comes into existence, and `used` is the
#: only signal that a credential nobody remembers is still live.
EVENT_API_TOKEN_ISSUED     = 'api.token.issued'
EVENT_API_TOKEN_REVOKED    = 'api.token.revoked'
EVENT_API_TOKEN_REJECTED   = 'api.token.rejected'

EVENT_INVITE_CREATED       = 'membership.invite.created'
EVENT_INVITE_REVOKED       = 'membership.invite.revoked'
EVENT_INVITE_ACCEPTED      = 'membership.invite.accepted'
EVENT_MEMBER_REMOVED       = 'membership.member.removed'
EVENT_ROLE_CHANGED         = 'membership.role.changed'

EVENT_CONNECTION_CREATED   = 'connection.created'
EVENT_CONNECTION_REMOVED   = 'connection.removed'

EVENT_SYNC_COMPLETED       = 'sync.completed'
EVENT_SYNC_FAILED          = 'sync.failed'

EVENT_AI_REQUESTED         = 'ai.requested'

EVENT_ACCOUNT_BALANCE_SET  = 'account.balance.set'

#: Phase 10.7. A person exercising the erasure right in the privacy policy.
#:
#: The one audit event that deliberately outlives everything it describes. The
#: user row is gone, the household may be gone, and every scoped row with it —
#: this is the only remaining record that any of it ever existed, which is what
#: makes it the answer to "what happened to this account". It is also why the
#: append-only guarantee matters here more than anywhere else: an event that
#: could be deleted would leave a deletion that nothing recorded.
#:
#: Its metadata names the username and the row counts removed. That is personal
#: data surviving a deletion request, disclosed in `templates/privacy.html`
#: rather than quietly retained, and justified by the same security interest
#: that justifies the audit trail existing at all.
EVENT_ACCOUNT_DELETED      = 'account.deleted'
#: A full data export was produced. Recorded because an export is a complete
#: copy of a household's finances leaving the system in one file — the single
#: highest-value action a compromised session could take that is not a password
#: change, and it should not be the one action that leaves no trace.
EVENT_ACCOUNT_EXPORTED     = 'account.exported'

#: Every event type the service will accept.
AUDIT_EVENT_TYPES = frozenset({
    EVENT_LOGIN_SUCCEEDED, EVENT_LOGIN_FAILED, EVENT_LOGIN_THROTTLED,
    EVENT_LOGOUT, EVENT_PASSWORD_REHASHED, EVENT_SETUP_COMPLETED,
    EVENT_INVITE_CREATED, EVENT_INVITE_REVOKED, EVENT_INVITE_ACCEPTED,
    EVENT_MEMBER_REMOVED, EVENT_ROLE_CHANGED,
    EVENT_CONNECTION_CREATED, EVENT_CONNECTION_REMOVED,
    EVENT_SYNC_COMPLETED, EVENT_SYNC_FAILED,
    EVENT_AI_REQUESTED,
    EVENT_ACCOUNT_BALANCE_SET,
    EVENT_API_TOKEN_ISSUED, EVENT_API_TOKEN_REVOKED, EVENT_API_TOKEN_REJECTED,
    EVENT_REGISTERED, EVENT_PASSWORD_CHANGED,
    EVENT_PASSWORD_RESET_REQUESTED, EVENT_PASSWORD_RESET_COMPLETED,
    EVENT_EMAIL_VERIFICATION_SENT, EVENT_EMAIL_VERIFIED,
    EVENT_SESSIONS_REVOKED, EVENT_RATE_LIMITED,
    EVENT_ACCOUNT_DELETED, EVENT_ACCOUNT_EXPORTED,
})


class AuditEvent(db.Model):
    """One thing that happened, who did it, and to what.  [Phase 8]

    ## Why `household_id` is nullable, and why that is not a hole

    Every other tenant table declares `household_id NOT NULL` and inherits
    `TenantScopedMixin`, so the ORM backstop filters it automatically. This one
    cannot, and the reason is in the requirement itself: an audit log has to
    record *failed* logins, and a failed login for a username that does not
    exist has no user, therefore no household. There is nothing truthful to put
    in the column. A sentinel household was considered and rejected for the same
    reason `_household_for_request` returns None rather than a wildcard -- a fake
    tenant that a scoped query could match is worse than no tenant at all.

    So the column is nullable and NULL means exactly one thing: *no tenant
    existed when this happened*. The safety that the mixin would have provided is
    replaced by a narrower guarantee, enforced in `dough/services/audit.py`:
    there is a single read function, and it always filters on the caller's
    household. A NULL row is therefore invisible to every tenant, and visible
    only to an operator reading the database directly -- which is the correct
    audience for "somebody tried to sign in as a user who does not exist".

    `tools/verify_tenancy.py` lists this table as deliberately unscoped with
    that reason attached, so the exception is reviewed rather than assumed.

    ## Append-only

    Enforced, not documented. `dough/services/audit.py` installs a `before_flush`
    hook that raises on any UPDATE or DELETE of an `AuditEvent`, in the same
    place and the same style as the tenancy write guard. An audit log that the
    application can quietly edit is a log that proves nothing, and "we only ever
    insert" is a property worth having a test for rather than a convention.

    Nothing here is deleted on a household's behalf either: removing a member
    does not remove the record that they were removed.
    """
    __tablename__ = 'audit_events'

    id = db.Column(db.Integer, primary_key=True)

    # NULL only when no tenant existed -- see the class docstring. No ForeignKey
    # cascade: an audit row must outlive the thing it describes.
    household_id = db.Column(db.Integer, db.ForeignKey('households.id'),
                             nullable=True, index=True)
    # NULL for anonymous actors (a failed login, a redemption in progress) and
    # for the scheduler, which has no user. `metadata_json` carries whatever
    # identifier was actually offered.
    actor_user_id = db.Column(db.Integer, db.ForeignKey('app_users.id'),
                              nullable=True, index=True)

    event_type = db.Column(db.String(60), nullable=False, index=True)
    # What the event was about: 'invite', 'app_user', 'connection', 'sync_run'.
    # A string rather than a polymorphic relationship on purpose -- the row it
    # names may already be deleted, and an audit log that breaks when its
    # subject is removed is useless at the moment it matters most.
    entity_type = db.Column(db.String(40), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.utcnow, index=True)

    # Request provenance. Both nullable: the scheduler has neither.
    ip_address = db.Column(db.String(45), nullable=True)     # 45 = max INET6 text
    user_agent = db.Column(db.String(255), nullable=True)

    #: JSON object, already redacted by the service. Never a password, a token,
    #: an API key, an account number, or the text of an AI prompt or reply.
    metadata_json = db.Column(db.Text, nullable=True)

    __table_args__ = (
        Index('ix_audit_events_household_created', 'household_id', 'created_at'),
    )

    def to_dict(self):
        import json as _json
        return {
            'id': self.id,
            'household_id': self.household_id,
            'actor_user_id': self.actor_user_id,
            'event_type': self.event_type,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'metadata': _json.loads(self.metadata_json) if self.metadata_json else {},
        }

