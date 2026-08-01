"""The four cost and traffic policies, at their call sites.
[Phase 10.6 — SEC-0018]

`tests/test_ratelimit.py` owns the abstraction: the policy table, the backend
contract, and the AST pin that says which policies have call sites at all. It
deliberately does not assert that any particular surface is limited, on the
grounds that "does a limiter work" and "is this route limited" are different
questions.

This file is the second question, for the four policies Phase 10.6 wired. It is
separate from `test_ratelimit.py` for the reason that file gives, and separate
from `test_api_auth.py` because these are questions about *cost and volume*
rather than about credentials, and they need an app with the limiter switched on
— which `RATELIMIT_ENABLED=False` under `TestingConfig` otherwise prevents.

What is being defended, stated once: with `ALLOW_REGISTRATION` on, a stranger
can reach an AI surface that spends real money per request. The limits below are
per household and per token, so one account cannot spend another's allowance and
cannot spend without bound.
"""

import pytest

from dough.ai.errors import AIBudgetExceeded
from dough.ai import EchoAdapter
from dough.ai.service import AIService
from dough.services.ratelimit import policy_for
from finance_sync import scheduler as scheduler_module

from app import create_app

PASSWORD = 'correct horse battery staple'


@pytest.fixture()
def limited_app(tmp_path):
    """An app with the limiter live and a working model.

    `RATELIMIT_ENABLED` is the switch that matters. It is False under
    `TestingConfig` so the rest of the suite can make as many requests as it
    likes, which means every limit in this application is only ever exercised by
    a fixture that turns it back on.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': EchoAdapter(configured=True),
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


# ---------------------------------------------------------------------------
# The AI budget
# ---------------------------------------------------------------------------

def _service(app):
    return app.extensions['dough_ai']


def _ask(surface='test'):
    """`build_request` keywords for one trivial completion.

    A helper because these tests make up to `policy_for('ai').limit` calls each
    and the content of the prompt is irrelevant to every assertion here — what
    is being counted is that a call happened.
    """
    return {'messages': [{'role': 'user', 'content': 'hello'}],
            'metadata': {'surface': surface}}


def test_the_hourly_budget_refuses_once_it_is_spent(limited_app):
    """The ceiling exists and is reached.

    Asserted against `policy_for('ai').limit` rather than a hardcoded 60, so a
    deliberate retune of the number does not fail this test — the property being
    defended is that there *is* a ceiling and that crossing it refuses.
    """
    from dough.tenancy import tenant_scope

    service = _service(limited_app)
    limit = policy_for('ai').limit

    with tenant_scope(limited_app.config['DEFAULT_HOUSEHOLD_ID']):
        for _ in range(limit):
            service.generate(**_ask())

        with pytest.raises(AIBudgetExceeded) as excinfo:
            service.generate(**_ask())

    assert excinfo.value.policy == 'ai'
    assert excinfo.value.retry_after > 0, (
        'a refusal that does not say when to come back produces clients that poll')


def test_one_household_cannot_spend_anothers_allowance(limited_app):
    """The budget is per household, which is what `per='household'` promises.

    A shared counter would let one family's chat exhaust every other family's,
    which is a denial of service dressed as a cost control.
    """
    from models import Household, db
    from dough.tenancy import tenant_scope, unscoped

    service = _service(limited_app)
    limit = policy_for('ai').limit

    with unscoped():
        other = Household(name='Second household')
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    with tenant_scope(limited_app.config['DEFAULT_HOUSEHOLD_ID']):
        for _ in range(limit):
            service.generate(**_ask())
        with pytest.raises(AIBudgetExceeded):
            service.generate(**_ask())

    with tenant_scope(other_id):
        response = service.generate(**_ask())
    assert response.text, 'the second household must have its own allowance'


def test_a_cached_answer_does_not_spend_the_budget(limited_app):
    """The accounting the ceiling is actually about.

    The limit is on spending money, and a cache hit did not. Charging for one
    would make the budget a limit on *questions asked* rather than on provider
    calls, and would make a well-cached surface look expensive.
    """
    from dough.tenancy import tenant_scope

    service = _service(limited_app)
    calls = {'n': 0}

    def producer():
        calls['n'] += 1
        return service.generate(**_ask('cached')).text

    with tenant_scope(limited_app.config['DEFAULT_HOUSEHOLD_ID']):
        first = service.cached('cached', producer)
        for _ in range(policy_for('ai').limit + 5):
            again = service.cached('cached', producer)

    assert calls['n'] == 1, 'the producer must run once'
    assert again == first, 'and every later read is the cached value'


def test_a_refusal_names_the_policy_rather_than_looking_like_a_provider_fault(
        limited_app):
    """`AIBudgetExceeded` is ours; `AIRateLimited` is the provider's.

    Collapsing them would make the cost control invisible in the logs somebody
    scans when the bill is wrong — the two mean opposite things about what to do
    next.
    """
    from dough.ai.errors import AIError, AIRateLimited
    from dough.tenancy import tenant_scope

    service = _service(limited_app)
    with tenant_scope(limited_app.config['DEFAULT_HOUSEHOLD_ID']):
        for _ in range(policy_for('ai').limit):
            service.generate(**_ask())
        with pytest.raises(AIBudgetExceeded) as excinfo:
            service.generate(**_ask())

    assert isinstance(excinfo.value, AIError), (
        'must stay an AIError so the routes that already catch one degrade')
    assert not isinstance(excinfo.value, AIRateLimited)


def test_the_budget_is_not_charged_when_there_is_no_application():
    """A service built without Flask still works.

    The adapter tests construct one directly. A limiter lives on an app, so with
    no app context there is nothing to spend against — and failing the call
    would make this module untestable without a request.
    """
    service = AIService(EchoAdapter(configured=True))
    assert service.generate(**_ask()).text


# ---------------------------------------------------------------------------
# The API budget
# ---------------------------------------------------------------------------

def _csrf(response):
    import re
    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _token(app, scopes=None):
    client = app.test_client()
    page = client.get('/setup')
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD, '_csrf_token': _csrf(page)})
    payload = {'username': 'sal', 'password': PASSWORD, 'device_name': 'test'}
    if scopes is not None:
        payload['scopes'] = scopes
    response = app.test_client().post('/api/v1/auth/login', json=payload)
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()['data']['token']


def test_a_token_over_its_write_limit_is_refused_with_429(limited_app):
    """`api_write` is the tighter of the two, so it is the one that fires first.

    Writes are held below general traffic because each costs a database write on
    a single-writer SQLite file — the limit is protecting the process, not a
    bill.
    """
    token = _token(limited_app)
    client = limited_app.test_client()
    headers = {'Authorization': f'Bearer {token}'}

    limit = policy_for('api_write').limit
    last = None
    for _ in range(limit + 1):
        last = client.post('/api/v1/transactions',
                           json={'description': 'x', 'amount': -1,
                                 'date': '2026-08-01', 'category': 'Other'},
                           headers=headers)

    assert last.status_code == 429, last.get_data(as_text=True)
    assert last.get_json()['error']['code'] == 'rate_limited'


def test_a_429_says_when_to_come_back(limited_app):
    """`Retry-After`, which the envelope handler had no way to send before.

    Without it a refused client polls, which is more load than the limit was
    imposed to prevent.
    """
    token = _token(limited_app)
    client = limited_app.test_client()
    headers = {'Authorization': f'Bearer {token}'}

    last = None
    for _ in range(policy_for('api_write').limit + 1):
        last = client.post('/api/v1/transactions',
                           json={'description': 'x', 'amount': -1,
                                 'date': '2026-08-01', 'category': 'Other'},
                           headers=headers)

    assert last.status_code == 429
    assert int(last.headers['Retry-After']) > 0


def test_reads_are_not_charged_against_the_write_limit(limited_app):
    """A client that has exhausted its writes can still read.

    The two policies are separate counters, and collapsing them would make a
    burst of writes take the whole API down for that token rather than just its
    writes.
    """
    token = _token(limited_app)
    client = limited_app.test_client()
    headers = {'Authorization': f'Bearer {token}'}

    for _ in range(policy_for('api_write').limit + 1):
        client.post('/api/v1/transactions',
                    json={'description': 'x', 'amount': -1,
                          'date': '2026-08-01', 'category': 'Other'},
                    headers=headers)

    assert client.get('/api/v1/transactions', headers=headers).status_code == 200


def test_a_session_authenticated_call_is_not_limited_by_the_token_policies(
        limited_app):
    """The residual SEC-0018 records, pinned so it is a decision and not a drift.

    Both policies declare `per='token'`. A session request has no token, so
    there is no key that would honour the declaration — inventing one would make
    the table say `token` while the code counted users. Session traffic to
    `/api/v1` is therefore unlimited by this hook, which is recorded rather than
    hidden; the expensive surface behind it is metered per household regardless
    of how the caller authenticated.
    """
    client = limited_app.test_client()
    page = client.get('/setup')
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD, '_csrf_token': _csrf(page)})

    for _ in range(policy_for('api_write').limit + 5):
        response = client.get('/api/v1/transactions')

    assert response.status_code == 200
