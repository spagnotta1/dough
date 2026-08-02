"""The authorization boundary, specified before it is rebuilt.

Written in Phase 0 deliberately: the boundary is described here first, so the
later auth/tenancy phases have to satisfy a contract they did not get to write.
Tests that describe behaviour the app does not have yet are marked
`xfail(strict=True)` -- strict, so that when a phase implements them the XPASS
fails the build and forces the marker to come off. A boundary test that can be
quietly satisfied by weakening it is not a boundary test.

What is asserted here:

  PASSING NOW  -- no route is reachable anonymously. Phase 6 replaced the
                  endpoint-name allowlist that used to guarantee this with the
                  @public marker, and this file needed no change for that: it
                  enumerates the URL map either way.
               -- the 17 non-/api mutation routes answer `fetch` with 401 JSON
                  rather than a 302 to an HTML login page  [Phase 6].
               -- public routes carry an explicit @public decorator, so the
                  public set is derived from the code  [Phase 6].
               -- no route accepts an unsafe method without a CSRF token. The
                  exempt set was empty from Phase 6 to Phase 9 and holds exactly
                  one view from Phase 10 (`api_v1_auth.login`, which has no
                  session to bind a token to); tests/test_csrf.py pins it and
                  states the argument.

               -- authenticated responses carry Cache-Control: no-store, and
                  anonymous and static ones do not  [Phase 8, SEC-0008].

  XFAIL        -- nothing. The Phase 0 specification is fully implemented; the
                  last marker came off in Phase 8.
"""

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

PASSWORD = 'hunter2boat'

# Routes that are legitimately reachable without a session. Since Phase 6 this
# is derived from the @public marker on the view itself
# (test_public_routes_carry_an_explicit_marker below asserts the two agree);
# this constant is the reviewed expectation the code is checked against.
# `auth.join` is here because redeeming an invitation is done by somebody who
# has no account yet -- the token is the credential.
#
# Phase 7 prefixed these with their blueprint. That the names changed and this
# constant had to be updated is the point of deriving the *set* from the markers
# rather than from the names: a route that quietly became public would still
# show up here as an extra item, whatever it happened to be called.
#
# Phase 8 added `health.live` and `health.ready`. They are public because a
# probe cannot hold a session, and because a readiness check that returns 302
# to /login tells a load balancer the application is fine when it is not. What
# makes that acceptable is the response body: check names and booleans, no
# versions, no configuration, no error text. See dough/blueprints/health.py.
# Phase 10 added exactly one: `api_v1_auth.login`. It is where API credentials
# come from, so a caller with no credential is the entire point of it -- the
# same argument that makes `auth.login` public. It is also the application's
# first `@csrf_exempt` view; see tests/test_csrf.py, where that set is pinned
# and the reasoning for the exemption is written down.
# Phase 10.5 added five, and one of them is unlike everything above it.
#
# Four are ordinary: `auth.register`, `auth.forgot_password`,
# `auth.reset_password` and `auth.verify_email` are all reached by somebody who
# by definition cannot sign in -- they have no account yet, or they have lost the
# password, or they are following a link out of a mail client that carries no
# cookie. The token is the credential, exactly as it is for `auth.join`.
#
# `core.dashboard` is the unusual one, and it is worth reading the marker
# carefully before assuming it is a mistake. `/` renders the marketing page for a
# stranger and the dashboard for a signed-in user, so it must be *reachable*
# without a session. It does not *show* anything without one: `_is_anonymous()`
# returns before any query runs, and an anonymous request never binds a
# household, so a tenant query on that path would raise rather than leak. The
# session-lifetime check still runs for a request that has a session --
# `_require_login` was changed in the same phase so that `@public` cannot be a
# way to skip it. See dough/blueprints/core.py.
#: `legal.privacy` and `legal.terms` are public deliberately and not
#: incidentally. They are read most often by somebody who has *not* signed in
#: and is deciding whether to hand this application a bank connection, and
#: Plaid's production review fetches the privacy URL anonymously. A policy
#: behind a login is a policy nobody can check before agreeing to it.
#:
#: They are also the safest possible public routes: two templates, no query, no
#: database access, and no request input reaches either one.
PUBLIC_ENDPOINTS = {'api_v1_auth.login', 'auth.forgot_password', 'auth.join',
                    'auth.login', 'auth.register', 'auth.reset_password',
                    'auth.setup', 'auth.verify_email', 'core.dashboard',
                    'health.live', 'health.ready', 'legal.privacy',
                    'legal.terms', 'static'}

# Sample values for URL converters, so every rule can actually be requested.
# Keyed on the werkzeug converter class name lowercased with 'converter'
# stripped -- IntegerConverter -> 'integer', UnicodeConverter -> 'unicode'.
CONVERTER_SAMPLES = {
    'integer': 1,
    'float': 1.0,
    'path': 'sample',
    'unicode': 'sample',
    'any': 'sample',
    'uuid': '00000000-0000-0000-0000-000000000000',
}
_DEFAULT_SAMPLE = 'sample'


def _concrete_path(rule):
    """Turn '/transactions/<int:transaction_id>' into '/transactions/1'."""
    values = {}
    for name in rule.arguments:
        kind = rule._converters[name].__class__.__name__.lower().replace('converter', '')
        values[name] = CONVERTER_SAMPLES.get(kind, _DEFAULT_SAMPLE)
    return rule.build(values, append_unknown=False)[1]


def _guardable_rules(app):
    """Every (path, method, endpoint) that should require a session."""
    out = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
            out.append((_concrete_path(rule), method, rule.endpoint))
    return sorted(out)


@pytest.fixture()
def guard_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def anon(guard_app):
    """A client that has created the owner account and then signed out.

    Signing out rather than never signing in matters: it exercises the state a
    real expired session lands in, and it means /login no longer redirects to
    /setup, so the assertions below see the real login page.
    """
    client = guard_app.test_client()
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD, 'confirm': PASSWORD})
    client.post('/logout')
    return client


def _request(client, path, method):
    return client.open(path, method=method)


# ---------------------------------------------------------------------------
# Passing now -- freeze the guarantee that exists today.
# ---------------------------------------------------------------------------

def test_no_route_is_reachable_anonymously(guard_app, anon):
    """Not one non-public route may execute its view without a session.

    This is the single most important test in the suite. It enumerates the URL
    map rather than listing routes by hand, so a route added in a later phase is
    covered the moment it exists -- which is the whole point. A new endpoint that
    forgets its guard fails here, not in production.
    """
    rules = _guardable_rules(guard_app)
    # Guard against this test silently becoming vacuous -- if _concrete_path or
    # the URL map ever yields nothing, an empty loop would "pass" while checking
    # precisely zero routes.
    # ~68 before Phase 10, ~117 after it added the /api/v1 surface. The floor is
    # raised with the route count rather than left at 60: a bound that every
    # plausible regression clears has stopped guarding anything.
    assert len(rules) >= 110, f'only {len(rules)} routes enumerated; expected ~117'
    assert not [p for p, _, _ in rules if '<' in p], 'unsubstituted URL converter'

    leaked = []
    for path, method, endpoint in rules:
        resp = _request(anon, path, method)
        # 302 to login, or 401. Anything else means the view ran.
        denied = resp.status_code == 401 or (
            resp.status_code in (301, 302, 303, 307, 308)
            and '/login' in resp.headers.get('Location', ''))
        if not denied:
            leaked.append((method, path, endpoint, resp.status_code))
    assert not leaked, "Reachable without a session:\n" + "\n".join(
        f"  {m} {p} -> {code} ({ep})" for m, p, ep, code in leaked)


def test_api_routes_answer_401_not_a_redirect(guard_app, anon):
    """/api/* must never hand a fetch() an HTML login page."""
    offenders = []
    for path, method, endpoint in _guardable_rules(guard_app):
        if not path.startswith('/api/'):
            continue
        resp = _request(anon, path, method)
        if resp.status_code != 401:
            offenders.append((method, path, resp.status_code))
    assert not offenders, f"/api routes not returning 401: {offenders}"


def test_page_routes_redirect_to_login_with_next(anon):
    resp = anon.get('/transactions')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']
    assert 'next=' in resp.headers['Location']


def test_public_endpoint_set_is_minimal(guard_app):
    """Guards against the allowlist quietly growing.

    Later phases may legitimately expand PUBLIC_ENDPOINTS further (legal pages,
    a status page). Doing so should be a visible, reviewed edit to this constant
    -- never an accident. Phase 6 added exactly one: `auth.join`. Phase 8 added
    two: the health probes. Phase 10 added one: `api_v1_auth.login`. Phase 10.5
    added five, which is the largest single expansion this set has had and the
    reason it is worth restating what the marker does and does not mean.
    Phase 10.7 added the two this docstring predicted: `legal.privacy` and
    `legal.terms`. They are the least dangerous entries here -- each renders one
    template, reads no request input and touches no database -- and they have to
    be public to do their job, since both are read by somebody deciding whether
    to sign up and Plaid's review fetches the privacy URL anonymously.

    `@public` suppresses the *login redirect*. It suppresses nothing else. It
    does not disable tenancy (an anonymous request binds no household, so a
    scoped query on that path raises rather than leaking), it does not disable
    CSRF (`/register`, `/forgot-password` and `/reset-password` all POST and all
    carry a token -- see tests/test_csrf.py), and since Phase 10.5 it does not
    skip the session-lifetime check either.

    Written out literally rather than as `PUBLIC_ENDPOINTS` so that the two are
    checked against each other. A test that compared the constant with itself
    would pass no matter what the constant said.
    """
    reachable_without_session = {'api_v1_auth.login', 'auth.forgot_password',
                                 'auth.join', 'auth.login', 'auth.register',
                                 'auth.reset_password', 'auth.setup',
                                 'auth.verify_email', 'core.dashboard',
                                 'health.live', 'health.ready', 'legal.privacy',
                                 'legal.terms', 'static'}
    actual = {r.endpoint for r in guard_app.url_map.iter_rules()} & PUBLIC_ENDPOINTS
    assert actual == reachable_without_session


# ---------------------------------------------------------------------------
# Implemented in Phase 6. These were xfail(strict=True) from Phase 0 until
# dough/auth.py satisfied them; the markers came off when the XPASS failed the
# build, which is what strict was for.
# ---------------------------------------------------------------------------

# Every mutating route that does NOT live under /api/. These are all called
# from fetch() in the templates, and before Phase 6 the guard only returned 401
# for paths starting with /api/, so these 302'd to an HTML login page -- which
# fetch follows, yielding a 200 full of HTML that the caller then tries to
# .json(). See dough.auth.wants_json.
NON_API_MUTATION_ROUTES = [
    ('/anomalies/1/dismiss', 'POST'),
    ('/anomalies/dismiss_all', 'POST'),
    ('/budgets', 'POST'),
    ('/import/sample/undo', 'POST'),
    ('/recurring/dismiss', 'POST'),
    ('/recurring/restore', 'POST'),
    ('/rules', 'POST'),
    ('/rules/ai-apply', 'POST'),
    ('/rules/ai-suggest', 'POST'),
    ('/rules/reorder', 'POST'),
    ('/rules/test', 'POST'),
    ('/transactions/1', 'PUT'),
    ('/transactions/1', 'DELETE'),
    ('/transactions/bulk_delete', 'POST'),
    ('/update_categories_bulk', 'POST'),
    ('/update_category', 'POST'),
    ('/upload', 'POST'),
]


@pytest.mark.parametrize('path,method', NON_API_MUTATION_ROUTES)
def test_non_api_mutations_return_401_to_fetch(anon, path, method):
    resp = anon.open(path, method=method, headers={
        'Sec-Fetch-Mode': 'cors',
        'Accept': 'application/json',
    })
    assert resp.status_code == 401
    assert resp.is_json


def test_browser_navigation_still_redirects(anon):
    """A real form post from a browser must keep redirecting, not start 401ing.

    Passes today and must still pass after Phase 6. The 401 negotiation is only
    allowed to change the answer for fetch(); turning a user's budget-form
    submission into a raw JSON 401 would be a regression, so this pins the other
    side of that change.
    """
    resp = anon.post('/budgets', data={}, headers={'Sec-Fetch-Mode': 'navigate'})
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_public_routes_carry_an_explicit_marker(guard_app):
    """Default-deny: a route is protected unless it opts out in its own source.

    The current allowlist is fail-open -- a new route is public until somebody
    remembers to think about it. After Phase 6 every reachable-anonymously view
    must carry `@public`, and this test derives the public set from the code
    rather than from a constant that can drift.
    """
    marked = {
        endpoint for endpoint, view in guard_app.view_functions.items()
        if getattr(view, '_dough_public', False)
    }
    assert marked == PUBLIC_ENDPOINTS - {'static'}


def test_session_cookie_is_httponly(guard_app):
    """Passes today (Flask's default). Pinned so it cannot regress."""
    assert guard_app.config['SESSION_COOKIE_HTTPONLY'] is True


def test_session_cookie_samesite_is_configured(guard_app):
    """Regression test for the setdefault no-op fixed in the Phase 0 addendum.

    Flask ships SESSION_COOKIE_SAMESITE in its own default config already set to
    None. Because the key is therefore always present, the original
    `config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')` never assigned
    anything and the cookie went out with no SameSite attribute -- while the
    source read as though a mitigation were in place. This app has no CSRF
    tokens, so that attribute was the only thing standing between a signed-in
    browser and a cross-site POST to /transactions/bulk_delete.

    Asserting the config value alone would have passed even with the bug had
    Flask's default been 'Lax', so the real Set-Cookie header is checked below.
    """
    assert guard_app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_session_cookie_header_carries_samesite(guard_app):
    """The attribute must survive into the wire format, not just the config.

    Werkzeug omits SameSite from the header entirely when the value is None, so
    this is the assertion that actually distinguishes fixed from broken.
    """
    client = guard_app.test_client()
    resp = client.post('/setup', data={
        'username': 'sal', 'password': PASSWORD, 'confirm': PASSWORD})

    cookies = [h for k, h in resp.headers if k == 'Set-Cookie' and 'session=' in h]
    assert cookies, 'signing in did not set a session cookie'
    header = cookies[0]
    assert 'SameSite=Lax' in header, header
    assert 'HttpOnly' in header, header


def test_authenticated_responses_are_not_cacheable(guard_app):
    """SEC-0008, closed in Phase 8. Was xfail(strict=True) from Phase 0.

    The failure is a shared machine: after logging out, the back button
    restores the dashboard from bfcache with real balances on it. `no-store` is
    the only directive that covers bfcache.
    """
    client = guard_app.test_client()
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD, 'confirm': PASSWORD})
    resp = client.get('/transactions')
    assert 'no-store' in resp.headers.get('Cache-Control', '')


def test_anonymous_and_static_responses_stay_cacheable(guard_app):
    """The other half: no-store on everything would be a performance bug.

    Static assets are the same bytes for everyone and are the one thing here
    worth caching. The login page carries no data belonging to anybody.
    """
    client = guard_app.test_client()
    assert 'no-store' not in client.get('/login').headers.get('Cache-Control', '')
    # Closed explicitly: static responses wrap an open file handle, and the
    # leak surfaced as the suite's only ResourceWarning.
    with client.get('/static/css/dough.css') as static:
        assert 'no-store' not in static.headers.get('Cache-Control', '')
