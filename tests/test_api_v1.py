"""The `/api/v1` contract: the envelope, the conventions, and the resources.

Runs against the shared `app` fixture, which has authentication off — so these
tests exercise the route bodies rather than the credential path.
`tests/test_api_auth.py` owns the credential path and turns auth on to do it.

Three groups, and the middle one is the reason this file exists:

1. **Shape.** Every response is the envelope, every collection pages the same
   way, every failure carries a code from the closed vocabulary. A client can be
   written against these and nothing else.
2. **Shared services.** The API and the web UI produce the same answers because
   they call the same code, asserted by making a change through one and reading
   it back through the other. A test that only exercised the API would pass
   just as well if the logic had been copied, which is the failure this phase
   is written against.
3. **Resources.** The individual endpoints do what they say.
"""

from datetime import date

import pytest

from models import Budget, Holding, Transaction, db


@pytest.fixture()
def ledger(app):
    """Five transactions across two categories and two accounts."""
    rows = [
        ('Checking', date(2026, 7, 1), 'Coffee shop', -4.50, 'Dining'),
        ('Checking', date(2026, 7, 2), 'Grocery run', -82.10, 'Groceries'),
        ('Checking', date(2026, 7, 3), 'Payday', 2400.00, 'Income'),
        ('Savings', date(2026, 7, 4), 'Bookshop', -31.00, 'Dining'),
        ('Savings', date(2026, 7, 5), 'Interest', 1.25, 'Income'),
    ]
    for account, day, description, amount, category in rows:
        db.session.add(Transaction(account_name=account, date=day,
                                   description=description, amount=amount,
                                   category=category))
    db.session.commit()
    return rows


def data_of(response):
    """The `data` block, asserting the envelope on the way through.

    Every test reads its payload through this rather than
    `response.get_json()['data']`, so the envelope is checked on every single
    response in the file instead of in one test that could be the only place it
    holds.
    """
    body = response.get_json()
    assert body is not None, 'response was not JSON'
    assert body['success'] is True, body
    assert body['meta']['api_version'] == 'v1'
    assert 'timestamp' in body['meta']
    return body['data']


def error_of(response):
    body = response.get_json()
    assert body['success'] is False, body
    assert body['meta']['api_version'] == 'v1'
    return body['error']


# ---------------------------------------------------------------------------
# 1. Shape
# ---------------------------------------------------------------------------

def test_every_successful_response_carries_the_envelope(client, ledger):
    """One shape, checked across resources rather than on one endpoint.

    The endpoints predating this phase each answer differently -- a bare object,
    a bare array, `{'success': ...}` with 200 on failure. That is what a second
    client cannot absorb, and this is the assertion that says v1 does not repeat
    it.
    """
    for path in ('/api/v1/transactions', '/api/v1/budgets', '/api/v1/accounts',
                 '/api/v1/settings', '/api/v1/investments/holdings',
                 '/api/v1/accounts/net-worth',
                 '/api/v1/transactions/categories'):
        response = client.get(path)
        assert response.status_code == 200, f'{path} -> {response.status_code}'
        body = response.get_json()
        assert set(body) == {'success', 'data', 'meta'}, path


def test_the_request_id_in_the_body_matches_the_header(client):
    """The whole point of emitting it twice.

    A person reads the id off a screen and it resolves to log lines; the header
    is invisible to anybody without a debugger. They have to be the same value
    or the one people can actually quote is the wrong one.
    """
    response = client.get('/api/v1/settings')
    assert (response.get_json()['meta']['request_id']
            == response.headers['X-Request-ID'])


def test_a_failure_carries_a_machine_readable_code(client):
    error = error_of(client.get('/api/v1/transactions?page=abc'))
    assert error['code'] == 'validation_error'
    assert 'page' in error['details']


def test_an_unrouted_api_path_answers_in_the_envelope(client):
    """`/api/v1/transctions` is the typo a client will actually make.

    A 404 for an unrouted path belongs to no blueprint, so it reaches the
    application's own handler. If that answered HTML, a client could not parse
    the thing telling it about its typo.
    """
    response = client.get('/api/v1/transctions')
    assert response.status_code == 404
    assert error_of(response)['code'] == 'not_found'


def test_a_wrong_method_answers_in_the_envelope(client):
    response = client.delete('/api/v1/settings')
    assert response.status_code == 405
    assert error_of(response)['code'] == 'method_not_allowed'


def test_a_missing_row_and_another_households_row_are_the_same_answer(client):
    """`find_owned` collapses them; the API must not undo that.

    Distinguishing them turns a sequential id into an oracle for how many
    transactions another household has.
    """
    response = client.get('/api/v1/transactions/99999')
    assert response.status_code == 404
    assert error_of(response)['code'] == 'not_found'


def test_an_unparseable_body_is_a_bad_request_not_a_crash(client):
    """`get_json(force=True)` raises a werkzeug 400 with an HTML body.

    That is the one response shape a JSON client cannot read, which is why
    `validation.body()` uses `silent=True` and refuses in the envelope.
    """
    response = client.post('/api/v1/budgets', data='{not json',
                           content_type='application/json')
    assert response.status_code == 400
    assert error_of(response)['code'] == 'bad_request'


def test_a_json_array_body_is_refused_rather_than_crashing(client):
    response = client.post('/api/v1/budgets', json=[1, 2, 3])
    assert response.status_code == 400
    assert error_of(response)['code'] == 'bad_request'


# ---------------------------------------------------------------------------
# Pagination and filtering conventions
# ---------------------------------------------------------------------------

def test_a_collection_pages_and_reports_where_it_is(client, ledger):
    first = client.get('/api/v1/transactions?page_size=2')
    meta = first.get_json()['meta']['pagination']

    assert len(data_of(first)) == 2
    assert meta == {'page': 1, 'page_size': 2, 'total': 5, 'total_pages': 3,
                    'has_next': True, 'has_prev': False}

    last = client.get('/api/v1/transactions?page_size=2&page=3')
    assert last.get_json()['meta']['pagination'] == {
        'page': 3, 'page_size': 2, 'total': 5, 'total_pages': 3,
        'has_next': False, 'has_prev': True}


def test_paging_never_repeats_or_drops_a_row(client, app):
    """Paging an unstable sort silently returns the wrong ledger.

    Ten transactions on the *same date* have no defined order, so without the
    primary-key tiebreak in `apply_ordering` the database may order them
    differently for page 1 and page 2 -- duplicating a row on one and losing it
    from the other. Nothing fails; the client just shows something wrong.

    Asserted by walking every page and comparing the multiset of ids against the
    unpaged truth, which is the only assertion that catches it.
    """
    for index in range(10):
        db.session.add(Transaction(account_name='Checking', date=date(2026, 7, 1),
                                   description=f'Same day {index}',
                                   amount=-index - 1, category='Dining'))
    db.session.commit()

    seen = []
    for page in (1, 2, 3, 4):
        seen.extend(t['id'] for t in data_of(
            client.get(f'/api/v1/transactions?page_size=3&page={page}')))

    assert len(seen) == len(set(seen)) == 10


def test_page_size_is_clamped_but_a_bad_page_is_refused(client, ledger):
    """The two are treated differently, deliberately.

    Asking for 5,000 rows is a caller making a judgement about its own memory,
    so it is clamped. `page=abc` is a client bug, and silently serving page 1
    would surface as "the API keeps returning the first page" -- reported as a
    server problem and investigated in the wrong place.
    """
    from dough.api.pagination import MAX_PAGE_SIZE

    clamped = client.get('/api/v1/transactions?page_size=5000')
    assert clamped.status_code == 200
    assert clamped.get_json()['meta']['pagination']['page_size'] == MAX_PAGE_SIZE

    assert client.get('/api/v1/transactions?page=abc').status_code == 422
    assert client.get('/api/v1/transactions?page=0').status_code == 422


def test_sort_is_an_allow_list_and_not_interpolated(client, ledger):
    """`sort` reaches an ORDER BY, so an unchecked value is an injection surface.

    The allow-list means the request never carries anything but a key into a
    dictionary the route wrote.
    """
    ascending = [t['amount'] for t in data_of(
        client.get('/api/v1/transactions?sort=amount&order=asc'))]
    assert ascending == sorted(ascending)

    refused = client.get('/api/v1/transactions?sort=amount;DROP TABLE')
    assert refused.status_code == 422
    assert error_of(refused)['code'] == 'validation_error'


def test_filters_are_spelled_the_same_way_across_the_api(client, ledger):
    dining = data_of(client.get('/api/v1/transactions?category=Dining'))
    assert {t['category'] for t in dining} == {'Dining'}

    savings = data_of(client.get('/api/v1/transactions?account=Savings'))
    assert {t['account_name'] for t in savings} == {'Savings'}

    outgo = data_of(client.get('/api/v1/transactions?direction=outgo'))
    assert all(t['amount'] < 0 for t in outgo)

    # Both boundaries inclusive. This is the assertion that caught the
    # pre-existing `Date`-vs-`datetime` comparison bug in
    # `build_transaction_query`: every filtered window silently dropped its
    # first day, on the web list and the CSV export as well as here. See the
    # comment at that filter.
    windowed = data_of(client.get(
        '/api/v1/transactions?date_from=2026-07-02&date_to=2026-07-03'))
    assert {t['date'] for t in windowed} == {'2026-07-02', '2026-07-03'}

    searched = data_of(client.get('/api/v1/transactions?q=Coffee'))
    assert [t['description'] for t in searched] == ['Coffee shop']


def test_an_inverted_date_range_is_refused_rather_than_returning_nothing(client, ledger):
    """An empty result would be indistinguishable from "you have no data".

    Which is the reading a client would report to its user, sending them looking
    for missing transactions rather than at their own query.
    """
    response = client.get(
        '/api/v1/transactions?date_from=2026-07-09&date_to=2026-07-01')
    assert response.status_code == 422
    assert error_of(response)['code'] == 'validation_error'


def test_the_api_does_not_inherit_the_pages_sticky_filters(client, ledger):
    """A stateless client sending no filter means "everything".

    The web list uses `sticky_filter`, which falls back to session state so a
    filter survives navigation. That is a browser affordance; answering an API
    request with whatever the last caller filtered on would be indefensible.
    """
    client.get('/transactions?category=Dining')          # sets session state
    assert len(data_of(client.get('/api/v1/transactions'))) == 5


# ---------------------------------------------------------------------------
# 2. Shared services — the architectural claim
# ---------------------------------------------------------------------------

def test_a_write_through_the_api_is_visible_to_the_web_route(client, ledger):
    """One implementation, two clients — asserted by crossing between them.

    A test that only used the API would pass equally well if the logic had been
    copied into `dough/api/`, which is precisely the outcome this phase exists
    to prevent. Writing through one surface and reading through the other is
    what makes it a claim about shared code.
    """
    data_of(client.post('/api/v1/transactions', json={
        'account_name': 'Checking', 'date': '2026-07-08',
        'description': 'API-created row', 'amount': -12.34,
        'category': 'Dining'}))

    page = client.get('/transactions').get_data(as_text=True)
    assert 'API-created row' in page


def test_a_write_through_the_web_route_is_visible_to_the_api(client, ledger):
    client.post('/budgets', data={'action': 'add', 'category': 'Dining',
                                  'account_name': 'both', 'monthly_limit': '250'})

    budgets = data_of(client.get('/api/v1/budgets'))['budgets']
    assert [b['category'] for b in budgets] == ['Dining']
    assert budgets[0]['monthly_limit'] == 250.0


def test_both_surfaces_compute_the_same_budget_progress(client, ledger):
    """The number, not just the row. Two implementations of "am I over budget"
    would disagree the first time either changed, and the disagreement would be
    about a household's money.
    """
    from dough.services import budgets as budget_service

    db.session.add(Budget(category='Dining', account_name='both',
                          monthly_limit=100))
    db.session.commit()

    api_row = data_of(client.get('/api/v1/budgets'))['budgets'][0]
    service_row = budget_service.status()['budgets'][0]

    assert api_row['spent'] == service_row['spent']
    assert api_row['pct'] == service_row['pct']
    assert api_row['state'] == service_row['state']


def test_the_synced_holding_rule_is_the_same_on_both_surfaces(client):
    """A rule living in one of two callers is a rule the other does not have.

    A synchronized holding must refuse a manual edit identically whether the
    request came from the page or from a client, because the consequence of
    accepting it is the same: the change appears to work and is reverted at the
    next sync.
    """
    holding = Holding(ticker='VTI', name='Total Market', shares=10,
                      current_value=2500, asset_class='ETF',
                      account_name='Brokerage', source='sync')
    db.session.add(holding)
    db.session.commit()

    legacy = client.put(f'/api/holdings/{holding.id}', json={'shares': 11})
    versioned = client.patch(f'/api/v1/investments/holdings/{holding.id}',
                             json={'shares': 11})

    assert legacy.status_code == 409
    assert versioned.status_code == 409
    assert error_of(versioned)['code'] == 'conflict'
    # The same sentence, from the same service, naming the same account.
    assert legacy.get_json()['error'] == error_of(versioned)['message']


def test_budget_spend_includes_the_first_of_the_month(client, app):
    """Regression: the window's first day was silently excluded.

    `spend_by_category` filtered a `Date` column against a `datetime`, so SQLite
    string-compared '2026-07-01' against '2026-07-01 00:00:00.000000' and
    dropped the 1st. Every budget under-reported its spend by whatever was
    charged on the first of the month — and under-reporting is the direction
    that reassures, so nobody would have investigated it.

    Asserted on the total rather than on the SQL, and with a transaction placed
    deliberately on day 1: a fixture using arbitrary dates passes either way.

    The service call pins `today` rather than letting it read the clock. It
    used to seed days 1-3 of the current month and compare against
    `status()` — which meant that for the first two days of every month the
    later transactions were still in the *future*, the window excluded them,
    and this test reported "the 1st of the month was dropped" for a reason that
    had nothing to do with the bug it guards. It failed on 1 August 2026
    exactly that way.
    """
    from datetime import datetime

    from dough.services import budgets as budget_service

    today = date.today()
    db.session.add(Budget(category='Dining', account_name='both',
                          monthly_limit=100))
    for day in (1, 2, 3):
        db.session.add(Transaction(
            account_name='Checking', date=date(today.year, today.month, day),
            description=f'Meal {day}', amount=-45.0, category='Dining'))
    db.session.commit()

    # A window that ends on the 3rd, whatever the real date is.
    third = datetime(today.year, today.month, 3, 23, 59, 59)
    row = budget_service.status(today=third)['budgets'][0]
    assert row['spent'] == 135.0, 'the 1st of the month was dropped'
    assert row['state'] == 'danger'

    # And through the API, which is the surface a client sees. The route reads
    # the real clock, so it can only have seen the days that have happened —
    # asserting that, rather than 135, is what keeps this honest on the 1st.
    so_far = 45.0 * sum(1 for day in (1, 2, 3) if day <= today.day)
    assert data_of(client.get('/api/v1/budgets'))['budgets'][0]['spent'] == so_far


def test_the_csv_sign_inference_is_reachable_and_pure(app):
    """Extracted from the upload route, where it needed a file and a request.

    The branch order is load-bearing: 'credit card' is tested before 'credit',
    so a card payment is an outgo rather than being caught by the generic
    'credit' branch and booked as income. That is the case worth pinning.
    """
    from dough.services.ledger import infer_signed_amount

    assert infer_signed_amount('CREDIT CARD PAYMENT', 50.0, None, None) == -50.0
    assert infer_signed_amount('DIRECT DEPOSIT', 50.0, None, None) == 50.0
    assert infer_signed_amount('PURCHASE AT SHOP', 50.0, None, None) == -50.0
    # The fallback: no vocabulary matched, so the balance delta decides.
    assert infer_signed_amount('OPAQUE', 50.0, balance=100.0,
                               prev_balance=150.0) == -50.0
    assert infer_signed_amount('OPAQUE', 50.0, balance=150.0,
                               prev_balance=100.0) == 50.0


# ---------------------------------------------------------------------------
# 3. Resources
# ---------------------------------------------------------------------------

def test_creating_a_transaction_returns_201_and_a_location(client):
    response = client.post('/api/v1/transactions', json={
        'account_name': 'Checking', 'date': '2026-07-08',
        'description': 'New row', 'amount': -12.34})

    assert response.status_code == 201
    body = data_of(response)
    assert response.headers['Location'] == f"/api/v1/transactions/{body['id']}"
    # Defaulted by the service, not by the client.
    assert body['category'] == 'Uncategorized'


def test_patching_leaves_unmentioned_fields_alone(client, ledger):
    """The bug this shape prevents: a PATCH blanking what it did not mention."""
    original = data_of(client.get('/api/v1/transactions?q=Coffee'))[0]

    updated = data_of(client.patch(f"/api/v1/transactions/{original['id']}",
                                   json={'category': 'Coffee'}))

    assert updated['category'] == 'Coffee'
    assert updated['description'] == original['description']
    assert updated['amount'] == original['amount']
    assert updated['date'] == original['date']


def test_patching_with_no_recognised_field_is_refused(client, ledger):
    """Silently succeeding would report a change that never happened."""
    row = data_of(client.get('/api/v1/transactions?q=Coffee'))[0]
    response = client.patch(f"/api/v1/transactions/{row['id']}",
                            json={'household_id': 99})
    assert response.status_code == 422


def test_a_patch_cannot_reparent_a_row_to_another_household(client, ledger):
    """`EDITABLE_FIELDS` is an allow-list, not "whatever keys arrived".

    Without it the tenancy write guard would still refuse -- but as a 500 from
    inside a flush, and only because that guard happens to exist.
    """
    row = data_of(client.get('/api/v1/transactions?q=Coffee'))[0]
    client.patch(f"/api/v1/transactions/{row['id']}",
                 json={'household_id': 99, 'category': 'Coffee'})

    from dough.tenancy import require_household
    assert (db.session.get(Transaction, row['id']).household_id
            == require_household())


def test_deleting_a_transaction_answers_204_with_no_body(client, ledger):
    row = data_of(client.get('/api/v1/transactions?q=Coffee'))[0]
    response = client.delete(f"/api/v1/transactions/{row['id']}")

    assert response.status_code == 204
    assert response.get_data() == b''
    assert client.get(f"/api/v1/transactions/{row['id']}").status_code == 404


def test_bulk_reports_what_actually_changed_not_what_was_asked(client, ledger):
    """The count comes from the UPDATE, never from `len(ids)`.

    An id belonging to another household is filtered out by the backstop and
    changes nothing, while the old route's response claimed it had. A client
    passing ids it guessed would be told the guess worked.
    """
    ids = [t['id'] for t in data_of(client.get('/api/v1/transactions?category=Dining'))]

    response = data_of(client.post('/api/v1/transactions/bulk', json={
        'action': 'recategorize', 'ids': ids + [999999], 'category': 'Eating out'}))

    assert response['affected'] == len(ids)


def test_bulk_refuses_a_malformed_id_list(client, ledger):
    for payload in ({'action': 'delete', 'ids': []},
                    {'action': 'delete', 'ids': 'all'},
                    {'action': 'delete', 'ids': ['1', '2']},
                    {'action': 'nonsense', 'ids': [1]}):
        assert client.post('/api/v1/transactions/bulk',
                           json=payload).status_code == 422, payload


def test_undoing_an_unknown_import_is_a_conflict_not_a_cheerful_zero(client):
    response = client.delete('/api/v1/transactions/imports/no-such-batch')
    assert response.status_code == 409
    assert error_of(response)['code'] == 'conflict'


def test_budgets_upsert_rather_than_conflict(client):
    first = client.post('/api/v1/budgets',
                        json={'category': 'Dining', 'monthly_limit': 200})
    second = client.post('/api/v1/budgets',
                         json={'category': 'Dining', 'monthly_limit': 300})

    assert first.status_code == 201
    assert data_of(first)['created'] is True
    assert second.status_code == 200
    assert data_of(second)['created'] is False
    assert data_of(second)['monthly_limit'] == 300.0


def test_accounts_reports_which_kind_each_entry_is(client, ledger):
    """A synced balance and a hand-typed one differ in trustworthiness.

    A client showing them identically would be lying by omission, so `kind` is
    on every entry rather than left to be inferred from which fields are set.
    """
    from models import AccountBalance

    db.session.add(AccountBalance(account_type='checking', starting_balance=1200))
    db.session.commit()

    entries = data_of(client.get('/api/v1/accounts'))
    kinds = {e['kind'] for e in entries}
    assert 'manual' in kinds
    assert 'ledger' in kinds        # Checking/Savings exist only on transactions

    # A CSV-only account has no known balance. Null, never 0.00 -- a client
    # would happily add a zero into a total.
    ledger_only = [e for e in entries if e['kind'] == 'ledger']
    assert ledger_only and all(e['balance'] is None for e in ledger_only)


def test_setting_a_manual_balance_is_audited_with_both_values(client):
    """"Balance set to 1200" does not say what an incident review needs."""
    from models import AuditEvent, EVENT_ACCOUNT_BALANCE_SET

    client.put('/api/v1/accounts/balances/checking',
               json={'starting_balance': 1200})
    client.put('/api/v1/accounts/balances/checking',
               json={'starting_balance': 900})

    events = AuditEvent.query.filter_by(
        event_type=EVENT_ACCOUNT_BALANCE_SET).all()
    latest = events[-1].to_dict()['metadata']
    assert latest['from'] == 1200
    assert latest['to'] == 900


def test_settings_reports_capabilities_and_never_values(client):
    """This endpoint is read by every client on startup, so it is a
    disclosure surface. It carries whether a key is configured, never the key.
    """
    settings = data_of(client.get('/api/v1/settings'))

    assert settings['api']['version'] == 'v1'
    assert isinstance(settings['ai']['available'], bool)

    flattened = repr(settings)
    for forbidden in ('SECRET_KEY', 'ANTHROPIC_API_KEY', 'sk-', 'password'):
        assert forbidden not in flattened


def test_holdings_round_trip_through_the_api(client):
    created = data_of(client.post('/api/v1/investments/holdings', json={
        'ticker': 'vti', 'name': 'Total Market', 'shares': 10,
        'current_value': 2500, 'asset_class': 'ETF'}))

    # Upper-cased by the service, in one place: `vti` and `VTI` are the same
    # position and storing both shows it twice on every allocation chart.
    assert created['ticker'] == 'VTI'

    updated = data_of(client.patch(
        f"/api/v1/investments/holdings/{created['id']}",
        json={'current_value': 2600}))
    assert updated['current_value'] == 2600.0
    assert updated['name'] == 'Total Market'

    assert client.delete(
        f"/api/v1/investments/holdings/{created['id']}").status_code == 204


def test_the_investments_overview_matches_the_shared_snapshot(client):
    """One derivation, four consumers. A second would let this API narrate a
    figure the page does not display.
    """
    from dough.services.networth import wealth_snapshot

    overview = data_of(client.get('/api/v1/investments'))
    assert overview['nw'] == wealth_snapshot()['nw']


def test_chat_conversations_round_trip(client):
    created = data_of(client.post('/api/v1/chat/conversations',
                                  json={'title': 'Budget questions'}))
    conversation_id = created['id']

    listed = data_of(client.get('/api/v1/chat/conversations'))
    assert conversation_id in [c['id'] for c in listed]

    renamed = data_of(client.patch(f'/api/v1/chat/conversations/{conversation_id}',
                                   json={'title': 'Renamed'}))
    assert renamed['title'] == 'Renamed'

    assert client.delete(
        f'/api/v1/chat/conversations/{conversation_id}').status_code == 204
    assert client.get(
        f'/api/v1/chat/conversations/{conversation_id}').status_code == 404


def test_asking_without_a_configured_model_is_503_not_a_silent_success(client):
    """The `app` fixture installs an EchoAdapter reporting itself unconfigured.

    Somebody who typed a question and pressed send has made a request that
    cannot be satisfied, so it is an error. The *briefings* deliberately answer
    `available: false` with a 200 instead -- see the next test.
    """
    response = client.post('/api/v1/copilot/ask', json={'question': 'How am I doing?'})
    assert response.status_code == 503
    assert error_of(response)['code'] == 'service_unavailable'


def test_an_unavailable_briefing_is_a_field_and_not_an_error(client):
    """A briefing is optional furniture on a dashboard.

    A 503 would make a healthy page look degraded because an optional feature is
    switched off, and every client would have to special-case that status to
    tell "no API key" from "the server is broken".
    """
    response = client.get('/api/v1/copilot/brief')
    assert response.status_code == 200
    assert data_of(response) == {'available': False}


def test_the_chat_limits_match_the_web_blueprints(client):
    """Two constants, deliberately duplicated, pinned so they cannot drift.

    `dough/api/v1/chat.py` cannot import them from `dough/blueprints/chat.py` --
    that is the cross-blueprint import rule 1 forbids -- so this is what keeps
    the two honest.
    """
    from dough.api.v1 import chat as api_chat
    from dough.blueprints import chat as web_chat

    assert api_chat.CHAT_HISTORY_LIMIT == web_chat.CHAT_HISTORY_LIMIT
    assert api_chat.CHAT_MAX_TOKENS == web_chat.CHAT_MAX_TOKENS


def test_household_never_serializes_an_invite_token(client, app):
    """The plaintext does not exist by the time anything lists an invitation.

    Only the hash is stored, so this is not a filter that could be forgotten --
    but it is worth pinning, because a future `to_dict` reflecting the model
    would start emitting `token_hash`, which is not the credential but is a
    great deal closer to it than nothing.
    """
    from models import AppUser, ROLE_MEMBER
    from dough.services.membership import issue_invite
    from dough.tenancy import require_household

    user = AppUser(username='sal', password_hash='x',
                   household_id=require_household())
    db.session.add(user)
    db.session.commit()
    _invite, token = issue_invite(require_household(), user, role=ROLE_MEMBER)

    listed = repr(data_of(client.get('/api/v1/household/invites')))
    assert token not in listed
    assert 'token_hash' not in listed
