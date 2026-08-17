"""Recurring bills / subscriptions detection (recurring.py + /recurring page)."""

import re
from datetime import date, timedelta

from recurring import detect_recurring


def _monthly(desc, amount, category, start, count, day_jitter=0, account='Checking'):
    """Build `count` monthly transactions starting at `start`."""
    txns = []
    for i in range(count):
        txns.append({
            'date': start + timedelta(days=30 * i + (day_jitter if i % 2 else 0)),
            'description': desc,
            'amount': amount,
            'category': category,
            'account_name': account,
        })
    return txns


# An anchor transaction fixes "today" (the max date) so recency math is stable.
TODAY = date(2026, 7, 1)
ANCHOR = [{'date': TODAY, 'description': 'COFFEE', 'amount': -4.50,
           'category': 'Food', 'account_name': 'Checking'}]


def test_subscription_category_detected_monthly():
    txns = ANCHOR + _monthly('NETFLIX.COM', -15.49, 'Subscriptions',
                             TODAY - timedelta(days=150), 5)
    out = detect_recurring(txns)
    assert [g['description'] for g in out['subscriptions']] == ['NETFLIX.COM']
    assert out['subscriptions'][0]['monthly_amount'] == -15.49
    assert out['bills'] == []


def test_bill_categories_detected_with_varying_amounts():
    start = TODAY - timedelta(days=150)
    txns = list(ANCHOR)
    for i, amt in enumerate([-263.64, -301.12, -255.00, -280.40, -290.00]):
        txns.append({'date': start + timedelta(days=30 * i),
                     'description': 'CHASE CREDIT CRD EPAY', 'amount': amt,
                     'category': 'Credit Card', 'account_name': 'Checking'})
    out = detect_recurring(txns)
    assert len(out['bills']) == 1
    bill = out['bills'][0]
    assert bill['category'] == 'Credit Card'
    # The typical actual payment (median), exactly as seen in transactions
    assert bill['monthly_amount'] == -280.40
    # Annual cost follows the real ~30-day payment cadence (≈ 12 payments)
    assert 3200 < bill['annual_cost'] < 3600
    assert out['subscriptions'] == []


def test_restaurant_with_similar_but_not_identical_amounts_excluded():
    # The complaint case: a favorite restaurant visited ~monthly for roughly
    # the same amount must NOT show up as a subscription.
    start = TODAY - timedelta(days=150)
    txns = list(ANCHOR)
    for i, amt in enumerate([-18.97, -19.12, -18.55, -19.03, -18.97]):
        txns.append({'date': start + timedelta(days=30 * i + (i % 2)),
                     'description': 'SALSA FRESCA BREWSTER', 'amount': amt,
                     'category': 'Food', 'account_name': 'Checking'})
    out = detect_recurring(txns)
    assert out['subscriptions'] == []
    assert out['bills'] == []


def test_hidden_subscription_in_other_category_needs_exact_amount():
    # An exact repeating charge outside the Subscriptions category (e.g. a
    # service categorized as Shopping) still qualifies with 3+ identical hits.
    txns = ANCHOR + _monthly('OPENAI CHATGPT SUBSCR', -20.00, 'Shopping',
                             TODAY - timedelta(days=120), 4)
    out = detect_recurring(txns)
    assert [g['description'] for g in out['subscriptions']] == ['OPENAI CHATGPT SUBSCR']
    # ...but only two occurrences is not enough evidence outside Subscriptions
    txns = ANCHOR + _monthly('OPENAI CHATGPT SUBSCR', -20.00, 'Shopping',
                             TODAY - timedelta(days=60), 2)
    assert detect_recurring(txns)['subscriptions'] == []


def test_stale_groups_dropped_after_six_months():
    # A subscription whose last charge is >6 months before the newest data
    # was probably cancelled.
    txns = ANCHOR + _monthly('HULU', -17.99, 'Subscriptions',
                             TODAY - timedelta(days=400), 6)
    assert detect_recurring(txns)['subscriptions'] == []


def test_transfers_income_investments_never_recurring():
    txns = list(ANCHOR)
    txns += _monthly('VENMO PAYMENT', -5.00, 'Transfer', TODAY - timedelta(days=150), 5)
    txns += _monthly('VANGUARD BUY', -500.00, 'Investments', TODAY - timedelta(days=150), 5)
    out = detect_recurring(txns)
    assert out['bills'] == [] and out['subscriptions'] == []


def test_source_format_change_merges_into_one_group():
    # CSV import says 'Withdrawal from FIRSTMARK PAYMENTS 1234', live sync
    # says 'FIRSTMARK' — one loan, one row.
    start = TODAY - timedelta(days=270)
    txns = list(ANCHOR)
    txns += _monthly('Withdrawal from FIRSTMARK PAYMENTS 1234', -487.17,
                     'Student Loan', start, 6)
    txns += _monthly('FIRSTMARK', -487.17, 'Student Loan',
                     start + timedelta(days=180), 3)
    out = detect_recurring(txns)
    assert len(out['bills']) == 1
    assert out['bills'][0]['occurrences'] == 9
    # Displays the most recent description form
    assert out['bills'][0]['description'] == 'FIRSTMARK'


def test_household_named_bill_categories_are_bills_not_subscriptions():
    # Categories are free text. A household that files its loan servicers under
    # 'Education' and its auto policy under 'Insurance' means the same thing as
    # 'Student Loan' / 'Insurance Payment' — these belong in Monthly Bills, not
    # in Subscriptions.
    start = TODAY - timedelta(days=300)
    txns = list(ANCHOR)
    txns += _monthly('FIRSTMARK', -487.17, 'Education', start, 10)
    txns += _monthly('PROG ADVANCED', -207.83, 'Insurance', start, 10)
    out = detect_recurring(txns)
    assert sorted(g['description'] for g in out['bills']) == ['FIRSTMARK', 'PROG ADVANCED']
    assert out['subscriptions'] == []


def test_bill_category_matching_does_not_overreach():
    # 'Parenting' contains 'rent' as a substring but is not a bill category —
    # matching is on whole words.
    txns = ANCHOR + _monthly('TARGET STORE', -62.14, 'Parenting',
                             TODAY - timedelta(days=150), 5)
    assert detect_recurring(txns)['bills'] == []


def test_renamed_payee_outside_bill_categories_is_one_group():
    # The strict tier requires identical amounts, but must still merge a payee
    # whose import wording changed — otherwise one charge lists twice, once
    # frozen at the stale name.
    start = TODAY - timedelta(days=270)
    txns = list(ANCHOR)
    txns += _monthly('Withdrawal from CRUNCHYROLL COM 998', -9.99, 'Shopping', start, 6)
    txns += _monthly('CRUNCHYROLL COM', -9.99, 'Shopping',
                     start + timedelta(days=180), 3)
    out = detect_recurring(txns)
    assert len(out['subscriptions']) == 1
    group = out['subscriptions'][0]
    assert group['occurrences'] == 9
    assert group['description'] == 'CRUNCHYROLL COM'


def test_group_is_named_by_its_most_recent_charge():
    # Both wordings land on the same day; the newest date decides the name, and
    # the stale wording must not win on merge order.
    start = TODAY - timedelta(days=300)
    txns = list(ANCHOR)
    txns += _monthly('Withdrawal from First Tech FCU ExtrnlTfr', -749.27,
                     'Student Loan', start, 6)
    txns += _monthly('First Tech FCU', -749.27, 'Student Loan',
                     start + timedelta(days=180), 4)
    out = detect_recurring(txns)
    assert len(out['bills']) == 1
    assert out['bills'][0]['description'] == 'First Tech FCU'
    assert out['bills'][0]['occurrences'] == 10


def test_irregular_bill_category_payments_still_included():
    # Category rules are the primary evidence for bills: two semester bursar
    # payments ~4 months apart are still student loan payments.
    txns = list(ANCHOR)
    txns.append({'date': TODAY - timedelta(days=160), 'description': 'IU ePay',
                 'amount': -4875.00, 'category': 'Student Loan', 'account_name': 'Savings'})
    txns.append({'date': TODAY - timedelta(days=40), 'description': 'IU ePay',
                 'amount': -2437.50, 'category': 'Student Loan', 'account_name': 'Savings'})
    out = detect_recurring(txns)
    assert [g['description'] for g in out['bills']] == ['IU ePay']


def test_dismissed_keys_suppress_groups():
    sub = _monthly('NETFLIX.COM', -15.49, 'Subscriptions', TODAY - timedelta(days=150), 5)
    bill = _monthly('Withdrawal from PROG ADVANCED INS PREM 123', -204.98,
                    'Insurance Payment', TODAY - timedelta(days=150), 5)
    txns = ANCHOR + sub + bill
    out = detect_recurring(txns)
    assert len(out['subscriptions']) == 1 and len(out['bills']) == 1

    # Exact normalized key
    out = detect_recurring(txns, dismissed_keys=['netflix.com'])
    assert out['subscriptions'] == []
    # Substring-related key survives a source-format change
    out = detect_recurring(txns, dismissed_keys=['prog advanced'])
    assert out['bills'] == []


def test_income_is_never_listed():
    txns = ANCHOR + _monthly('PAYROLL DEPOSIT', 2500.00, 'Income',
                             TODAY - timedelta(days=150), 5)
    out = detect_recurring(txns)
    assert out['bills'] == [] and out['subscriptions'] == []


def test_recurring_page_renders(client):
    from models import db, Transaction
    start = TODAY - timedelta(days=150)
    for i in range(5):
        db.session.add(Transaction(account_name='Checking',
                                   date=start + timedelta(days=30 * i),
                                   description='SPOTIFY', amount=-11.99,
                                   category='Subscriptions'))
        db.session.add(Transaction(account_name='Checking',
                                   date=start + timedelta(days=30 * i),
                                   description='FIRSTMARK', amount=-487.17,
                                   category='Student Loan'))
    db.session.commit()
    resp = client.get('/recurring')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'SPOTIFY' in html
    assert 'FIRSTMARK' in html
    assert 'Monthly Bills' in html


def test_recurring_page_empty_db(client):
    resp = client.get('/recurring')
    assert resp.status_code == 200


def test_the_account_filter_is_the_shared_filter_bar(client):
    """One control, and the same one the dashboard and the ledger carry.

    This page's filter used to be a card holding a labelled `<select>` and a
    Filter button — three pieces of chrome for one choice, and a different
    shape from the same choice on the two pages either side of it.
    """
    from models import db, Transaction

    db.session.add(Transaction(account_name='Visa', date=TODAY,
                               description='SPOTIFY', amount=-11.99,
                               category='Subscriptions'))
    db.session.commit()

    html = client.get('/recurring').get_data(as_text=True)
    assert 'data-filter-bar' in html, 'the recurring view lost its filter bar'
    assert 'ds-filter-trigger' in html
    # Unfiltered is the state the page opens in, so it gets no chip.
    assert 'ds-filter-chip' not in html


def test_filtering_to_an_account_says_so_and_offers_a_way_back(client):
    from models import db, Transaction

    db.session.add(Transaction(account_name='Visa', date=TODAY,
                               description='SPOTIFY', amount=-11.99,
                               category='Subscriptions'))
    db.session.commit()

    html = client.get('/recurring?account=Visa').get_data(as_text=True)
    assert '<span class="ds-filter-chip__val">Visa</span>' in html
    assert 'name="account" value="Visa"' in html
    # And the chip's ✕ leads back to the unfiltered page rather than nowhere.
    assert re.search(r'ds-filter-chip__x"[^>]*href="/recurring"', html)


def test_dismiss_and_restore_flow(client):
    from models import db, Transaction, RecurringDismissal
    start = TODAY - timedelta(days=150)
    for i in range(5):
        db.session.add(Transaction(account_name='Checking',
                                   date=start + timedelta(days=30 * i),
                                   description='SPOTIFY', amount=-11.99,
                                   category='Subscriptions'))
    db.session.commit()

    assert 'SPOTIFY' in client.get('/recurring').get_data(as_text=True)

    resp = client.post('/recurring/dismiss',
                       data={'description': 'SPOTIFY', 'kind': 'subscription'},
                       follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Gone from the subscriptions table, present only in the dismissed panel
    assert 'Dismissed items (1)' in html
    assert RecurringDismissal.query.count() == 1

    # Dismissing again is a no-op, not a crash or duplicate
    client.post('/recurring/dismiss', data={'description': 'SPOTIFY 123', 'kind': 'subscription'})
    assert RecurringDismissal.query.count() == 1

    dismissal = RecurringDismissal.query.first()
    resp = client.post('/recurring/restore', data={'id': dismissal.id}, follow_redirects=True)
    assert resp.status_code == 200
    assert RecurringDismissal.query.count() == 0
    assert 'SPOTIFY' in resp.get_data(as_text=True)
