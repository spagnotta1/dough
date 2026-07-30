"""Shared fixtures: an isolated app + database per test."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.ai import EchoAdapter
from dough.tenancy import tenant_scope
from models import db


@pytest.fixture(autouse=True)
def _isolate_live_credentials(monkeypatch):
    """Tests must exercise sandbox mode by default regardless of what live
    credentials a developer happens to have in their real .env — app.py loads
    .env at import time, so those vars are already in os.environ otherwise.
    Tests that specifically want live-configured behavior re-set these via
    their own monkeypatch.setenv(...).

    ANTHROPIC_API_KEY is in this list for a stronger reason than the others: a
    developer with a real key in .env would otherwise have `/api/dashboard-insight`
    and the two briefing endpoints make **live, billable** API calls during a
    test run. Nothing asserted on that, so it would have been silent.
    """
    for var in ("PLAID_CLIENT_ID", "PLAID_SECRET", "COINBASE_CLIENT_ID",
                "COINBASE_CLIENT_SECRET", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def app(tmp_path):
    """A fresh app bound to a temporary SQLite database.

    The AI adapter is an `EchoAdapter` that reports itself unconfigured, which
    reproduces the "no API key" state every test before Phase 4 ran in — and
    guarantees no test can reach the network even if the isolation above is
    someday loosened. Tests that want a *working* model install a configured
    EchoAdapter of their own; see tests/test_ai_adapter.py.
    """
    # Each test gets its own scheduler bound to its own app.
    scheduler_module._scheduler = None

    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,   # manual-refresh API runs inline for determinism
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': EchoAdapter(configured=False),
    })
    # Phase 5: the default household is bound for the whole test, so the tests
    # that predate tenancy keep building fixtures with a bare `db.session.add`
    # and reading them back with `Model.query`.
    #
    # This is the *ambient* scope, not the one a request runs under —
    # create_app's before_request pushes its own, nested inside this one. Tests
    # that need to prove isolation therefore cannot use this fixture, and
    # tests/test_tenancy_boundary.py deliberately builds its own app with no
    # ambient household: with one bound, every assertion in that file would
    # pass for the wrong reason.
    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def client(app):
    return app.test_client()
