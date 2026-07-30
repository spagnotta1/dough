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
