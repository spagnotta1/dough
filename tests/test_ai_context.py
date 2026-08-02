"""`proactive.py` and `ai_context.py` — what Dough is told, and what he is not.

These two are tested together because they are the same guarantee at two sizes:
the model receives conclusions that were computed, with the figures attached,
and receives nothing that was estimated. Everything Phase 11 claims about not
fabricating financial facts reduces to that, and it is a property of the context
rather than of the prompt.

The assertions worth reading twice:

- `test_the_summarised_context_is_much_smaller_than_the_detailed_one` — the
  reason this module exists, held to a ratio so it cannot quietly regress.
- `test_an_unmeasurable_figure_is_null_and_not_absent` — an absent key invites
  the model to fill the gap; a null with a reason invites it to say it cannot see.
- `test_the_context_never_carries_another_households_figures` — the one that
  matters more than the rest put together.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.services import ai_context, proactive

TODAY = date(2026, 8, 15)


@pytest.fixture()
def post():
    from models import Transaction, db

    def _post(when, description, amount, category='Groceries',
              account='checking'):
        db.session.add(Transaction(
            account_name=account, date=when, description=description,
            amount=Decimal(str(amount)), category=category))
        db.session.commit()
    return _post


@pytest.fixture()
def household(post):
    """Six months of ordinary history, with one deliberate story in it.

    Dining climbs steadily and a subscription reprices in August — enough for
    the trend engine and the anomaly engine each to have something true to say,
    without so much noise that a ranking assertion becomes luck.
    """
    for month in range(3, 9):
        post(date(2026, month, 1), 'Payroll ACME', 5000.00, 'Income')
        post(date(2026, month, 2), 'Landlord', -1800.00, 'Rent')
        post(date(2026, month, 4), 'Whole Foods', -400.00, 'Groceries')
        post(date(2026, month, 6), 'Netflix', -15.99, 'Streaming')
        post(date(2026, month, 8), 'Olive Garden', -(100 + (month - 3) * 60),
             'Dining')
    # Before TODAY, not after: a charge dated later in August falls outside
    # every window anchored on the 15th, and the assertions below would pass
    # vacuously against an empty result.
    post(date(2026, 8, 10), 'Netflix', -24.99, 'Streaming')
    return post


# ── Proactive insights ──────────────────────────────────────────────────────

def test_a_quiet_month_produces_no_insights(app, post):
    """An empty list is a valid result and must stay one.

    A card that says something obvious teaches the reader to skip the card.
    """
    for month in range(3, 9):
        post(date(2026, month, 1), 'Payroll ACME', 5000.00, 'Income')
        post(date(2026, month, 2), 'Landlord', -1800.00, 'Rent')

    assert proactive.insights(anchor=TODAY) == []


def test_insights_are_capped_and_ranked(app, household):
    found = proactive.insights(limit=3, anchor=TODAY)
    assert len(found) <= 3
    assert found == sorted(found, key=lambda i: -i['priority'])
    assert all(i['priority'] >= proactive.MIN_PRIORITY for i in found)


def test_every_insight_carries_the_figures_behind_it(app, household):
    """A formatter must be able to check the sentence it is about to write."""
    for insight in proactive.insights(anchor=TODAY):
        assert insight['summary']
        assert insight['evidence']
        assert insight['severity'] in ('critical', 'warning', 'info', 'positive')
        assert insight['amount'] is not None
        assert insight['source'] in ('anomalies', 'periods', 'trends', 'health')


def test_a_subscription_reprice_surfaces_as_an_insight(app, household):
    found = proactive.insights(anchor=TODAY)
    hike = next((i for i in found if i['kind'] == 'subscription_hike'), None)
    assert hike is not None
    assert hike['actionable'] is True
    assert hike['evidence']['was'] == 15.99
    assert hike['evidence']['now'] == 24.99


def test_larger_money_outranks_smaller_at_equal_severity(app):
    """The log scale, asserted directly rather than through a fixture."""
    small = proactive._priority({'severity': 'warning', 'amount': 40.0})
    large = proactive._priority({'severity': 'warning', 'amount': 4000.0})
    assert large > small
    # ...but not by a factor of a hundred, which is what a linear term gives.
    assert large < small * 2


def test_actionable_findings_outrank_equivalent_ones(app):
    passive = proactive._priority({'severity': 'warning', 'amount': 500.0})
    actionable = proactive._priority({'severity': 'warning', 'amount': 500.0,
                                      'actionable': True})
    assert actionable > passive


def test_one_insight_per_category(app, household):
    """Dining both spiked and is trending; saying both reads as repetition."""
    found = proactive.insights(anchor=TODAY)
    categories = [i['evidence'].get('category') for i in found
                  if i['evidence'].get('category')]
    assert len(categories) == len(set(categories))


def test_the_digest_carries_the_headline_figures_too(app, household):
    digest = proactive.digest(anchor=TODAY)
    assert digest['window']['label'] == 'August 2026'
    assert digest['income'] == 5000.00
    assert 'insights' in digest


# ── Context shape ───────────────────────────────────────────────────────────

def test_the_context_is_json_serialisable(app, household):
    """It is about to be `json.dumps`ed into a prompt. Decimals would raise."""
    import json

    built = ai_context.build(anchor=TODAY)
    assert json.loads(json.dumps(built, default=str))


def test_only_the_requested_sections_are_built(app, household):
    built = ai_context.build(['period', 'budgets'], anchor=TODAY)
    assert 'period' in built and 'budgets' in built
    assert 'trends' not in built
    assert 'portfolio' not in built
    assert 'unusual_activity' not in built


def test_an_unknown_section_raises_rather_than_being_ignored(app):
    """A typo'd section name that silently returns less is a context that is
    quietly missing the thing the caller asked for."""
    with pytest.raises(ValueError, match='unknown context sections'):
        ai_context.build(['period', 'holdinggs'])


def test_the_period_figures_match_the_analytics_layer(app, household):
    """The context must not re-derive anything. One number, one source."""
    from dough.services import analytics

    window = analytics.resolve_window('month', TODAY)
    truth = analytics.period_summary(window)
    built = ai_context.build(['period'], anchor=TODAY)['period']

    assert built['income'] == truth['income']
    assert built['spending'] == truth['spending']
    assert built['net_cash_flow'] == truth['net']
    assert built['savings_rate_pct'] == truth['savings_rate']


def test_the_provenance_note_tells_the_model_what_null_means(app, household):
    note = ai_context.build(['period'], anchor=TODAY)['note']
    assert 'null' in note and 'not zero' in note
    assert 'cannot see it' in note


def test_an_unmeasurable_figure_is_null_and_not_absent(app, post):
    """An absent key invites the model to fill the gap."""
    post(date(2026, 8, 4), 'Whole Foods', -400.00, 'Groceries')
    built = ai_context.build(['period'], anchor=TODAY)

    assert 'savings_rate_pct' in built['period']
    assert built['period']['savings_rate_pct'] is None


def test_missing_sections_say_why_rather_than_going_quiet(app, post):
    post(date(2026, 8, 4), 'Whole Foods', -400.00, 'Groceries')
    built = ai_context.build(['budgets', 'holdings'], anchor=TODAY)

    assert built['budgets'] == {'available': False, 'reason': 'no budgets set'}
    assert built['portfolio']['available'] is False
    assert built['portfolio']['reason']


def test_trends_carry_their_confidence(app, household):
    """The model cannot hedge on a number it was not given."""
    for trend in ai_context.build(['trends'], anchor=TODAY)['trends']:
        assert trend['confidence'] in ('low', 'moderate', 'high')
        assert trend['months_observed'] >= 3


def test_budgets_carry_the_months_progress(app, post):
    """60% spent means opposite things on the 5th and the 25th."""
    from models import Budget, db

    db.session.add(Budget(category='Groceries', monthly_limit=Decimal('500'),
                          account_name='both'))
    db.session.commit()
    post(date(2026, 8, 4), 'Whole Foods', -300.00, 'Groceries')

    built = ai_context.build(['budgets'], anchor=TODAY)['budgets']
    assert built['month_progress_pct'] == 48        # the 15th of a 31-day month
    assert built['budgets'][0]['used_pct'] == 60.0


def test_the_health_section_names_what_it_did_not_measure(app, household):
    health = ai_context.build(['health'], anchor=TODAY)['financial_health']
    assert 0 <= health['score'] <= 100
    assert health['methodology']
    assert 'Investment consistency' in health['not_measured']


def test_coverage_states_the_real_range(app, household):
    coverage = ai_context.build(['coverage'], anchor=TODAY)['coverage']
    assert coverage['first_transaction'] == '2026-03-01'
    assert coverage['total_transactions'] > 0


# ── Size ────────────────────────────────────────────────────────────────────

def test_the_summarised_context_is_much_smaller_than_the_detailed_one(app, post):
    """The reason this module exists, held to a ratio.

    Measured against a realistic ledger rather than the small fixture above,
    because the claim is about households with history: the detailed context
    grows with the ledger and this one does not, so on six transactions they
    are nearly the same size and the assertion would mean nothing.

    Two years of daily-ish spending measures at about 17% — the threshold here
    is 50%, loose enough to survive fixture churn and tight enough that losing
    the summarisation fails it.
    """
    from models import Transaction, db
    from dough.services.finance_context import build_finance_context

    categories = ['Groceries', 'Dining', 'Gas', 'Shopping', 'Utilities',
                  'Travel', 'Streaming', 'Healthcare']
    day = date(2024, 9, 1)
    rows, n = [], 0
    while day < TODAY:
        for offset in range(2):
            category = categories[n % len(categories)]
            rows.append(Transaction(
                account_name='checking', date=day,
                description=f'{category} store {n}',
                amount=Decimal(str(-(20 + (n % 90)))), category=category))
            n += 1
        if day.day == 1:
            rows.append(Transaction(
                account_name='checking', date=day, description='Payroll ACME',
                amount=Decimal('5200.00'), category='Income'))
        day += _one_day()
    db.session.add_all(rows)
    db.session.commit()

    detailed = ai_context.estimated_tokens(build_finance_context(detail=True))
    summarised = ai_context.estimated_tokens(ai_context.build(anchor=TODAY))

    assert summarised < detailed * 0.5, (
        f'summarised {summarised} vs detailed {detailed} tokens')


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def test_the_context_does_not_grow_with_the_ledger(app, post):
    """The property the ratio above is really standing in for.

    Ten times the transactions must not mean ten times the context, or the
    copilot gets slower every month the household uses the product.
    """
    for month in range(3, 9):
        post(date(2026, month, 1), 'Payroll', 5000.00, 'Income')
    small = ai_context.estimated_tokens(ai_context.build(anchor=TODAY))

    for month in range(3, 9):
        for day in range(1, 26):
            post(date(2026, month, day), f'Shop {day}', -20.00 - day, 'Shopping')
    large = ai_context.estimated_tokens(ai_context.build(anchor=TODAY))

    assert large < small * 2.5


# ── Tenancy ─────────────────────────────────────────────────────────────────

def test_the_context_never_carries_another_households_figures(app):
    """The assertion that matters more than the rest put together."""
    import json

    from dough.tenancy import tenant_scope, unscoped
    from models import Household, Transaction, db

    with unscoped():
        a = Household(name='A', plaid_user_id='ctx-a')
        b = Household(name='B', plaid_user_id='ctx-b')
        db.session.add_all([a, b])
        db.session.commit()
        a_id, b_id = a.id, b.id

    for hid, amount, payee in ((a_id, '-111.11', 'Alpha Grocers'),
                               (b_id, '-999.99', 'Beta Boutique')):
        with tenant_scope(hid):
            for month in range(3, 9):
                db.session.add(Transaction(
                    account_name='checking', date=date(2026, month, 4),
                    description=payee, amount=Decimal(amount),
                    category='Groceries'))
            db.session.commit()

    with tenant_scope(a_id):
        serialised = json.dumps(ai_context.build(anchor=TODAY), default=str)

    assert 'Alpha Grocers' in serialised
    assert 'Beta Boutique' not in serialised
    assert '999.99' not in serialised
