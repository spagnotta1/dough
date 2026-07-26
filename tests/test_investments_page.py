"""The Investments page and the wealth-snapshot it renders from.

The arithmetic itself lives in ``tests/test_investments_intel.py``; these
tests cover the seam — that the route assembles the snapshot correctly from
real rows, that the page renders with and without data, and that the copilot
endpoints behave sanely when no API key is configured.
"""

from datetime import date, timedelta

import pytest

from models import Holding, PortfolioSnapshotRow, db


def _connect(client, institution):
    resp = client.post("/api/connections", json={"institution": institution})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _add_holding(**kw):
    row = Holding(**{
        "ticker": "AAPL", "name": "Apple Inc.", "shares": 10,
        "current_value": 2000, "asset_class": "Stock",
        "account_name": "Brokerage", "source": "manual", **kw})
    db.session.add(row)
    db.session.commit()
    return row


def _add_snapshots(values, end=None):
    end = end or date.today()
    start = end - timedelta(days=len(values) - 1)
    for i, v in enumerate(values):
        db.session.add(PortfolioSnapshotRow(
            snapshot_date=start + timedelta(days=i),
            checking=100, savings=200, total_cash=300,
            brokerage=v, crypto=0, total_investments=v, net_worth=v + 300))
    db.session.commit()


# ══════════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════════

def test_page_renders_with_no_data_at_all(client):
    """A brand-new account must not 500 on a page full of ratios."""
    resp = client.get("/investments")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Wealth Copilot" in body
    assert "No holdings yet" in body


def test_page_renders_with_synced_holdings(client):
    _connect(client, "plaid")
    _connect(client, "coinbase")
    resp = client.get("/investments")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Refresh" in body          # sync control survived the redesign
    assert "Synced" in body           # per-holding sync marker
    assert "Portfolio health" in body
    assert "Wealth Copilot" in body


def test_alpine_binding_is_deferred_for_the_spa_router(client):
    """A literal x-data on the root breaks every form on SPA entry.

    The router assigns new markup with innerHTML; Alpine's mutation observer
    walks it before the router executes the inline script that defines
    investmentsApp, so the binding has to be held back and promoted. This is
    invisible on a direct page load, which is what makes it worth pinning.
    """
    body = client.get("/investments").data.decode()
    assert 'x-ignore' in body
    assert 'data-alpine="investmentsApp(' in body
    assert 'x-data="investmentsApp(' not in body


def test_manual_holdings_still_editable_from_the_page(client):
    holding = _add_holding()
    body = client.get("/investments").data.decode()
    assert f"editHolding({holding.id})" in body


def test_modelled_figures_state_their_basis(client):
    """The projection and benchmark must never appear as measurements."""
    _add_holding()
    # Enough span that the benchmark comparison is willing to render at all.
    _add_snapshots([9000.0 + i * 8 for i in range(130)])
    body = client.get("/investments").data.decode()
    assert "Modelled from the assumptions shown" in body
    assert "not live index data" in body
    assert "built-in yield reference" in body


def test_benchmark_and_horizon_come_from_the_query_string(client):
    _add_holding()
    body = client.get("/investments?benchmark=nasdaq&horizon=20&contribution=500").data.decode()
    assert "NASDAQ 100" in body
    assert "Projected in 20 years" in body


def test_a_short_history_says_so_instead_of_headlining_setup_churn(client):
    """The first weeks of snapshots are accounts being linked, not returns."""
    _add_holding()
    _add_snapshots([50_000.0, 39_000.0, 40_000.0])
    body = client.get("/investments").data.decode()
    assert "days of snapshots so far" in body
    assert "treat the swings above as provisional" in " ".join(body.split())


def test_bad_query_parameters_fall_back_rather_than_erroring(client):
    _add_holding()
    for qs in ("benchmark=nope", "horizon=abc", "horizon=999", "contribution=-5",
               "contribution=xyz"):
        assert client.get(f"/investments?{qs}").status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# The snapshot the page and the copilot share
# ══════════════════════════════════════════════════════════════════════════

def test_snapshot_matches_the_underlying_rows(app):
    _add_holding(ticker="VTI", name="Vanguard Total Stock Market ETF",
                 asset_class="ETF", current_value=6000, avg_cost=50, shares=100)
    _add_holding(ticker="BND", name="Vanguard Total Bond ETF",
                 asset_class="Bond", current_value=4000)

    snap = app.wealth_snapshot()
    assert len(snap["positions"]) == 2
    assert snap["nw"]["investments"] == pytest.approx(10_000)
    assert snap["concentration"]["top1_pct"] == pytest.approx(60.0, abs=0.1)
    # Sector labels come from the reference table, and coverage says so.
    assert snap["allocation"]["sector"]["coverage"] > 0
    assert 0 <= snap["health"]["score"] <= 100
    assert 0 <= snap["risk"]["score"] <= 100


def test_sweep_cash_is_not_double_counted_as_a_position(app):
    """Cash-class holdings are the brokerage sweep. They already count as
    cash in net worth, so counting them as portfolio positions too would
    inflate both the value and every weight derived from it."""
    _add_holding(ticker="VMFXX", name="Federal Money Market", asset_class="Cash",
                 current_value=5000)
    _add_holding(ticker="AAPL", current_value=5000)

    snap = app.wealth_snapshot()
    assert [p["ticker"] for p in snap["positions"]] == ["AAPL"]
    assert snap["nw"]["brokerage_cash"] == pytest.approx(5000)


def test_performance_uses_the_snapshot_history(app):
    _add_holding(current_value=11_000)
    _add_snapshots([10_000.0, 11_000.0])

    perf = app.wealth_snapshot()["performance"]
    assert perf["windows"]["day"]["available"] is True
    assert perf["windows"]["day"]["change"] == pytest.approx(1000.0)
    # A two-day history cannot support a year window, and says so rather
    # than reporting zero.
    assert perf["windows"]["year"]["available"] is False


def test_empty_portfolio_snapshot_is_all_zeroes_not_an_exception(app):
    snap = app.wealth_snapshot()
    assert snap["positions"] == []
    assert snap["insights"] == []
    assert snap["concentration"]["largest"] is None
    assert snap["dividends"]["annual"] == 0


def test_insights_flag_a_dominant_position(app):
    _add_holding(ticker="AAPL", current_value=9000)
    _add_holding(ticker="MSFT", name="Microsoft", current_value=1000)

    insights = app.wealth_snapshot()["insights"]
    assert insights[0]["severity"] == "critical"
    assert "AAPL" in insights[0]["title"]
    assert insights[0]["why"]      # every item explains itself


# ══════════════════════════════════════════════════════════════════════════
# Copilot endpoints
# ══════════════════════════════════════════════════════════════════════════

def test_brief_reports_unavailable_without_an_api_key(client, app):
    app.config["ANTHROPIC_API_KEY"] = ""
    assert client.get("/api/investments/brief").get_json() == {"available": False}


def test_ask_rejects_an_empty_question(client):
    assert client.post("/api/investments/ask", json={"question": "  "}).status_code == 400


def test_ask_reports_a_missing_api_key_rather_than_failing_opaquely(client, app):
    app.config["ANTHROPIC_API_KEY"] = ""
    resp = client.post("/api/investments/ask", json={"question": "How am I doing?"})
    assert resp.status_code == 503
    assert "API key" in resp.get_json()["error"]
