"""HTTP API and page rendering for the sync layer."""

from models import FinancialAccount, Holding, InstitutionConnection, Transaction


def _connect(client, institution):
    resp = client.post("/api/connections", json={"institution": institution})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def test_institutions_catalog(client):
    resp = client.get("/api/institutions")
    assert resp.status_code == 200
    slugs = {i["institution"] for i in resp.get_json()}
    assert "plaid" in slugs
    # Coinbase is registered but not offered, so it must not reach the picker.
    assert "coinbase" not in slugs


def test_connect_flow_syncs_immediately(client):
    data = _connect(client, "plaid")
    assert data["institution"] == "plaid"
    # SYNC_SYNCHRONOUS=True → the initial sync already ran
    assert FinancialAccount.query.count() == 2
    assert Transaction.query.filter_by(source="sync").count() > 0
    connection = InstitutionConnection.query.first()
    assert connection.status == "connected"
    assert connection.last_sync_status == "success"


def test_connect_unknown_institution_404(client):
    resp = client.post("/api/connections", json={"institution": "nope"})
    assert resp.status_code == 404


def test_manual_refresh_single_and_all(client):
    data = _connect(client, "coinbase")
    resp = client.post(f"/api/connections/{data['id']}/sync")
    assert resp.status_code == 202
    resp = client.post("/api/sync/all")
    assert resp.status_code == 202
    history = client.get("/api/sync/history").get_json()
    assert len(history) >= 3  # connect + single + all


def test_sync_status_endpoint(client):
    _connect(client, "plaid")
    status = client.get("/api/sync/status").get_json()
    assert status["running"] is False
    assert status["connections"][0]["institution"] == "plaid"


def test_net_worth_endpoint(client):
    _connect(client, "plaid")
    _connect(client, "coinbase")
    data = client.get("/api/net-worth").get_json()
    assert data["current"]["net_worth"] > 0
    assert data["current"]["crypto"] > 0
    assert len(data["history"]) == 1  # today's snapshot


def test_disconnect_via_api(client):
    data = _connect(client, "coinbase")
    resp = client.delete(f"/api/connections/{data['id']}")
    assert resp.status_code == 200
    assert FinancialAccount.query.count() == 0


def test_synced_holding_cannot_be_edited_manually(client):
    _connect(client, "coinbase")
    holding = Holding.query.filter_by(source="sync").first()
    assert holding is not None
    resp = client.put(f"/api/holdings/{holding.id}", json={"shares": 1})
    assert resp.status_code == 409
    resp = client.delete(f"/api/holdings/{holding.id}")
    assert resp.status_code == 409


def test_transaction_filters_can_be_cleared(client):
    # Prime the sticky session filters via a drill-down style link.
    resp = client.get("/transactions?type=inbound&category=Income")
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("direction") == "inbound"
        assert sess.get("category") == "Income"

    # Submitting the filter form with everything reset to "All" sends the
    # params as empty strings — that must clear the filters, not fall back
    # to the previously stored session values.
    resp = client.get(
        "/transactions?account=&category=&direction=&start_date=&end_date=&search="
    )
    assert resp.status_code == 200
    assert b'value="inbound" selected' not in resp.data
    with client.session_transaction() as sess:
        assert sess.get("direction") is None
        assert sess.get("category") is None

    # Params absent entirely (e.g. pagination links) still keep session filters.
    client.get("/transactions?direction=outgo")
    resp = client.get("/transactions?page=1")
    assert b'value="outgo"   selected' in resp.data


def test_pages_render(client):
    _connect(client, "coinbase")
    _connect(client, "plaid")
    for path in ("/", "/investments", "/connections", "/sync-history",
                 "/transactions", "/budgets"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    body = client.get("/investments").data.decode()
    assert "Refresh" in body
    assert "Synced" in body
    body = client.get("/connections").data.decode()
    assert "Coinbase" in body and "Connected" in body


def test_dashboard_category_cascading_filter(client):
    from datetime import date
    from models import db, Transaction

    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 5),
                               description="AJI SUSHI", amount=-77.31, category="Food"))
    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 6),
                               description="SHELL GAS", amount=-53.19, category="Gas"))
    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 7),
                               description="MTA TICKET", amount=-20.00, category="Travel"))
    db.session.commit()
    window = "start_date=2026-03-01&end_date=2026-03-31"

    # Unfiltered: all categories feed the charts (running balance ends
    # at -77.31 - 53.19 - 20.00 = -150.50).
    html = client.get(f"/?{window}").get_data(as_text=True)
    assert "-150.5" in html

    # Single category: only Food data reaches the visualizations
    # (balance history JSON), but the breakdown grid still lists the other
    # categories so the user can switch or extend the filter.
    html = client.get(f"/?{window}&category=Food").get_data(as_text=True)
    assert "-77.31" in html
    assert "-130.5" not in html and "-150.5" not in html
    # The active filter appears as a removable chip in the filter panel. It
    # carries a hidden input so the selection composes with the rest of the
    # form instead of navigating the moment it changes.
    assert 'data-cat="Food"' in html
    assert '<input type="hidden" name="category" value="Food">' in html
    assert 'data-remove-cat="Food"' in html
    assert ">Gas</a>" in html                           # grid row still clickable
    assert "category=Food&amp;category=Gas" in html \
        or "category=Food&category=Gas" in html         # row link adds Gas to the selection

    # Multiselect: Food + Gas cascade together, Travel stays excluded.
    html = client.get(f"/?{window}&category=Food&category=Gas").get_data(as_text=True)
    assert "-130.5" in html
    assert "-150.5" not in html
    assert 'data-cat="Food"' in html and 'data-cat="Gas"' in html

    # No category params → no chips, and the category row stays collapsed.
    html = client.get(f"/?{window}").get_data(as_text=True)
    assert 'data-cat="Food"' not in html
    assert 'id="catRow" hidden' in html


def test_a_first_run_dashboard_asks_for_a_connection_not_a_file(client):
    """An account with no transactions at all is somebody who has just signed
    up, and the moment they are most willing to set this up is the moment they
    are looking at this page. It used to point them at a statement upload —
    the slower of the two paths, and the one they have to keep repeating.
    """
    html = client.get("/").get_data(as_text=True)

    assert "Link an account and I'll take it from there" in html
    assert 'href="/connections" class="ds-btn ds-btn--primary">Connect an institution' in html
    # Upload survives as the fallback for a bank Plaid cannot reach; it is no
    # longer the thing being asked for.
    assert "Or upload a statement" in html
    # The empty-*window* advice would be wrong here: there is no window to widen.
    assert "Try a wider date range" not in html


def test_an_empty_window_over_a_stocked_ledger_still_gets_filter_advice(client):
    """The other empty state, which must not be swallowed by the first one.
    Nothing in March is a filter question, not an onboarding question."""
    from datetime import date

    from models import Transaction, db

    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 5),
                               description="AJI SUSHI", amount=-77.31, category="Food"))
    db.session.commit()

    html = client.get("/?start_date=2026-04-01&end_date=2026-04-30").get_data(as_text=True)

    assert "No transactions in" in html
    assert "Try a wider date range" in html
    assert "Link an account and I'll take it from there" not in html


def test_filtering_the_transaction_list_does_not_blank_the_dashboard(client):
    """Reported as "I filter by This Month on transactions, then the dashboard
    says there are no transactions".  [UAT round 1]

    It was never about the dates. The two pages share `session['account']` and
    disagree about how to spell "everything": the dashboard says 'both', and
    the transactions filter form's "All Accounts" option has value="", which
    `sticky_filter` stores as None. Every submission of that form — the date
    preset chips submit the whole form — planted that None, and the dashboard
    then asked for `account_name IS NULL`, which no row can satisfy.
    """
    from datetime import date

    from models import Transaction, db

    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 5),
                               description="AJI SUSHI", amount=-77.31, category="Food"))
    db.session.commit()

    # Exactly what the filter form sends: every field, account left on "All".
    client.get("/transactions?account=&category=&start_date=2026-03-01"
               "&end_date=2026-03-31&type=&search=")

    html = client.get("/").get_data(as_text=True)

    assert "No transactions in" not in html
    assert "-77.31" in html
    # And the panel's account select agrees with the query it just ran, rather
    # than showing "Both accounts" only because nothing else was selected.
    assert '<option value="both" selected>Both accounts</option>' in html


def test_a_connection_awaiting_its_first_sync_is_not_asked_to_connect_again(client):
    """Linked, but the sync has not landed: the ledger is still empty and the
    page still has nothing to show. Repeating the invitation there reads as the
    connection having failed."""
    from models import db

    db.session.add(InstitutionConnection(institution="plaid", display_name="Plaid",
                                         status="connected"))
    db.session.commit()

    html = client.get("/").get_data(as_text=True)

    assert "I'm fetching the history now" in html
    assert "Check sync status" in html
    assert "Link an account and I'll take it from there" not in html


def test_chat_context_splits_spending_and_income_by_account(app):
    """The assistant can only answer "checking only" if the snapshot carries the
    account dimension over the full history — recent_transactions is too short."""
    from datetime import date
    from models import db, Transaction

    rows = [
        ("Checking", date(2026, 3, 5), "AJI SUSHI", -77.31, "Food"),
        ("Checking", date(2026, 3, 6), "SHELL GAS", -53.19, "Gas"),
        ("Savings",  date(2026, 3, 7), "VANGUARD BUY", -500.00, "Investments"),
        ("Checking", date(2026, 3, 8), "PAYROLL", 2400.00, "Income"),
        ("Savings",  date(2026, 3, 9), "INTEREST", 12.50, "Income"),
    ]
    for account, when, desc, amount, category in rows:
        db.session.add(Transaction(account_name=account, date=when,
                                   description=desc, amount=amount, category=category))
    db.session.commit()

    ctx = app.build_finance_context(detail=True)

    assert ctx["transaction_coverage"]["accounts"] == ["Checking", "Savings"]

    march = ctx["monthly_spending_by_account_category"]["2026-03"]
    assert march["Checking"] == {"Food": 77.31, "Gas": 53.19}
    assert march["Savings"] == {"Investments": 500.00}

    # The combined series stays intact — a "total" answer reads it directly
    # rather than re-adding the split.
    assert ctx["monthly_spending_by_category"]["2026-03"] == {
        "Food": 77.31, "Gas": 53.19, "Investments": 500.00}

    assert ctx["monthly_income_by_account"]["2026-03"] == {
        "Checking": 2400.00, "Savings": 12.50}
    assert ctx["monthly_income"]["2026-03"] == 2412.50


def test_chat_context_gives_totals_to_reconcile_an_other_bucket(app):
    """A chart that folds small categories into "Other" must get that number by
    subtraction. Summing the leftovers by hand is what the assistant gets wrong,
    so the totals it subtracts from ship with the snapshot."""
    from datetime import date
    from models import db, Transaction

    rows = [
        ("Checking", "Transfer", -1000.00),
        ("Checking", "Food", -100.00),
        ("Checking", "Gas", -30.00),
        ("Checking", "Shopping", -20.00),
        ("Savings", "Investments", -500.00),
    ]
    for account, category, amount in rows:
        db.session.add(Transaction(account_name=account, date=date(2026, 3, 5),
                                   description=category.upper(), amount=amount,
                                   category=category))
    db.session.commit()

    totals = app.build_finance_context(detail=True)["monthly_spending_totals"]["2026-03"]

    assert totals["Checking"] == {"total": 1150.00, "excluding_transfers": 150.00}
    assert totals["Savings"] == {"total": 500.00, "excluding_transfers": 500.00}
    assert totals["all_accounts"] == {"total": 1650.00, "excluding_transfers": 650.00}

    # An "Other" series after naming Transfer and Food is one subtraction.
    assert round(totals["Checking"]["total"] - 1000.00 - 100.00, 2) == 50.00


def test_dashboard_embeds_parseable_json_for_the_client(client):
    """The dashboard's state block must be valid JSON, in a non-executable tag.

    The client reads every chart series out of `#dashData`. Two ways that has
    broken: the block losing its id/type during SPA navigation (the browser
    then tries to run JSON as code), and a non-finite float reaching it —
    Python writes bare `NaN`/`Infinity`, which `JSON.parse` rejects. Either
    leaves the dashboard silently inert, so both are pinned here.
    """
    import json
    import re
    from datetime import date
    from models import db, Transaction

    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 5),
                               description="AJI SUSHI", amount=-77.31, category="Food"))
    db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 8),
                               description="PAYCHECK", amount=2400.00, category="Income"))
    db.session.commit()

    html = client.get("/?start_date=2026-03-01&end_date=2026-03-31").get_data(as_text=True)

    match = re.search(
        r'<script id="dashData" type="application/json">(.*?)</script>', html, re.S)
    assert match, 'dashboard must embed its state in <script id="dashData" type="application/json">'

    payload = match.group(1)
    assert not re.search(r'\b(NaN|Infinity|-Infinity)\b', payload), \
        "non-finite float reached the client; JSON.parse would reject the whole block"

    data = json.loads(payload)
    for key in ("monthlyIncome", "monthlyOutgo", "balanceHistory", "categoryTrend",
                "categoryStats", "budgetMap", "periodMonths", "allCategories", "forecast"):
        assert key in data, f"dashData is missing {key}"

    # The forecast drives the one chart drawn before any panel is opened.
    assert data["forecast"]["points"], "forecast must carry at least one point"
    assert all(isinstance(p["balance"], (int, float)) for p in data["forecast"]["points"])


def test_the_dashboard_window_includes_its_own_start_date(client):
    """The dashboard counted a window one day shorter than it advertised.

    Reported from the running app: the transactions page listed four August
    rows over `2026-08-01 .. 2026-08-03` while the dashboard beside it counted
    one. Everything dated on the start day was missing, so `Spending` read
    $5.00 against an actual $41.21.

    The cause was `Transaction.date.between(start, end)` given `datetime`
    objects. SQLAlchemy types that bind by the value rather than by the `Date`
    column and sends '2026-08-01 00:00:00.000000'; SQLite compares it as a
    string against the stored '2026-08-01', which is shorter and sorts first,
    so `>= start` is False on the start date itself.

    `build_transaction_query` had already been fixed for exactly this — which
    is what made the two pages disagree rather than both being wrong — so this
    asserts the agreement rather than the total alone. A boundary this quiet is
    only visible when two views of the same window are compared.
    """
    import json
    import re
    from datetime import date
    from models import db, Transaction

    rows = [
        (date(2026, 8, 1), 'Whole Foods', -35.92, 'Groceries'),    # the start day
        (date(2026, 8, 2), 'RAILWAY', -5.00, 'Uncategorized'),
        (date(2026, 8, 3), 'Hardware store', -12.00, 'Shopping'),  # the end day
        (date(2026, 7, 31), 'Before the window', -99.00, 'Shopping'),
    ]
    for day, description, amount, category in rows:
        db.session.add(Transaction(account_name='Checking', date=day,
                                   description=description, amount=amount,
                                   category=category))
    db.session.commit()

    window = 'start_date=2026-08-01&end_date=2026-08-03'
    html = client.get(f'/?{window}').get_data(as_text=True)
    data = json.loads(re.search(
        r'<script id="dashData" type="application/json">(.*?)</script>',
        html, re.S).group(1))

    # Asserted on the embedded state rather than the rendered figure: `money`
    # rounds to whole dollars by default, and $53 vs $5 would have caught this
    # one while a subtler boundary slipped through.
    spent = sum(s['outbound'] for s in data['categoryStats'].values())
    assert spent == 35.92 + 5.00 + 12.00, \
        'dashboard spending must cover the whole window, both boundaries included'

    # The balance history is the series drawn on the page, and it is built by
    # its own query -- so it can disagree with the totals above, and did.
    assert [p['date'] for p in data['balanceHistory']] == [
        '2026-08-01', '2026-08-02', '2026-08-03']

    # And the two pages agree about which rows are in the window.
    listed = client.get(f'/transactions?{window}').get_data(as_text=True)
    for description in ('Whole Foods', 'RAILWAY', 'Hardware store'):
        assert description in listed
    assert 'Before the window' not in listed
    assert data['categoryStats'].keys() == {'Groceries', 'Uncategorized', 'Shopping'}


def test_the_palette_ranks_spending_categories_ahead_of_income_and_transfers(client):
    """The charts that use this palette are spending charts. [UAT round 1]

    Reported from the running app: "Spending by category over time" drew three
    of its six series in the identical overflow gray. The palette is eight
    hues wide and `allCategories` decides who gets one — but it ranked on
    gross `abs(amount)` over every row, so `Income` and `Transfer`, the two
    largest movers in almost any ledger and the two a spending chart can never
    draw, took the first two slots. A quarter of the palette went to
    categories guaranteed not to appear, pushing real series past the end of
    it and into the shared neutral.

    The head of this list must therefore be the biggest *spenders*. Income and
    transfers still appear — the breakdown grid needs a stable identity for
    them — but behind every category that spends.
    """
    import json
    import re
    from datetime import date
    from models import db, Transaction

    # Income and Transfer dwarf every real spending category, which is exactly
    # the shape that used to hand them the first two hues.
    rows = [
        ("PAYCHECK", 9000.00, "Income"),
        ("MOVE TO SAVINGS", -8000.00, "Transfer"),
        ("AJI SUSHI", -300.00, "Food"),
        ("SHELL", -200.00, "Gas"),
        ("NETFLIX", -100.00, "Subscriptions"),
    ]
    for description, amount, category in rows:
        db.session.add(Transaction(account_name="Checking", date=date(2026, 3, 5),
                                   description=description, amount=amount,
                                   category=category))
    db.session.commit()

    html = client.get("/?start_date=2026-03-01&end_date=2026-03-31").get_data(as_text=True)
    match = re.search(
        r'<script id="dashData" type="application/json">(.*?)</script>', html, re.S)
    categories = json.loads(match.group(1))["allCategories"]

    spenders = ["Food", "Gas", "Subscriptions"]
    assert categories[:3] == spenders, (
        "the biggest spenders must hold the first palette slots, got "
        f"{categories}")
    # Present, but behind every spender — they still need a stable color
    # elsewhere on the page, just not one a spending chart was going to use.
    assert set(categories[3:]) == {"Income", "Transfer"}


def test_a_default_window_with_no_data_snaps_to_the_newest_month(client):
    """Reported as "none of the visualizations are appearing". [UAT round 1]

    The dashboard defaults to month-to-date. A household whose newest
    transaction predates the 1st therefore got an empty window, and every
    panel drew an empty box — the ordinary state of this application in the
    first days of a month, and for anyone whose last sync is a few days old.
    """
    import json
    import re
    from datetime import date, timedelta
    from models import db, Transaction

    # Comfortably before the 1st of whatever month the suite runs in.
    stale = date.today().replace(day=1) - timedelta(days=20)
    db.session.add(Transaction(account_name="Checking", date=stale,
                               description="AJI SUSHI", amount=-77.31,
                               category="Food"))
    db.session.commit()

    html = client.get("/").get_data(as_text=True)
    data = json.loads(re.search(
        r'<script id="dashData" type="application/json">(.*?)</script>',
        html, re.S).group(1))

    assert data["categoryTrend"]["months"] == [stale.strftime("%Y-%m")], (
        "the default window must fall back to the newest month holding data")
    assert data["categoryStats"], "the fallback window must carry the charts"


def test_an_explicitly_requested_empty_window_is_left_alone(client):
    """"No transactions between these dates" is a true and useful answer.

    Relocating someone who typed a date range, or followed a link to one,
    would make the date inputs disagree with what is on screen. Only the
    unasked-for default gets rescued.
    """
    import json
    import re
    from datetime import date, timedelta
    from models import db, Transaction

    stale = date.today().replace(day=1) - timedelta(days=20)
    db.session.add(Transaction(account_name="Checking", date=stale,
                               description="AJI SUSHI", amount=-77.31,
                               category="Food"))
    db.session.commit()

    empty_start = date.today().replace(day=1)
    html = client.get(f"/?start_date={empty_start}&end_date={date.today()}"
                      ).get_data(as_text=True)
    data = json.loads(re.search(
        r'<script id="dashData" type="application/json">(.*?)</script>',
        html, re.S).group(1))

    assert data["categoryTrend"]["months"] == []
    assert "No transactions in" in html, (
        "an explicitly empty window must say so rather than silently moving")


def test_the_fallback_does_nothing_when_the_current_month_has_data(client):
    """Month-to-date stays the default whenever it holds anything."""
    import json
    import re
    from datetime import date
    from models import db, Transaction

    today = date.today()
    db.session.add(Transaction(account_name="Checking", date=today,
                               description="AJI SUSHI", amount=-77.31,
                               category="Food"))
    db.session.commit()

    html = client.get("/").get_data(as_text=True)
    data = json.loads(re.search(
        r'<script id="dashData" type="application/json">(.*?)</script>',
        html, re.S).group(1))

    assert data["categoryTrend"]["months"] == [today.strftime("%Y-%m")]
