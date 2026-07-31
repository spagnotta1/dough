"""Shared fixtures: an isolated app + database per test."""

import os
import sys

import dotenv
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _isolate_from_developer_dotenv():
    """Keep the developer's real `.env` out of the test run entirely.

    `app.py` calls `load_dotenv()` at import, and `config.py` reads the
    environment in its *class bodies* -- `ALLOW_REGISTRATION = _bool(...)` is
    evaluated once, when the module is first imported. Those two facts together
    mean a fixture cannot undo the leak: by the time an autouse fixture runs its
    `monkeypatch.delenv`, `BaseConfig.ALLOW_REGISTRATION` has already been
    computed from whatever the file said.

    That is not hypothetical. A single `ALLOW_REGISTRATION=1` in a personal
    `.env` failed `test_registration_is_closed_by_default` -- a test whose
    entire subject is what the default is. The suite has to describe the code,
    not the machine it happens to run on.

    Two steps, and both are needed. Neutralising `load_dotenv` alone leaves
    anything a previous import already loaded; clearing `os.environ` alone gets
    undone by `app.py`'s call a moment later, because `load_dotenv` declines to
    overwrite variables that are set but happily fills in ones that are absent.

    The names come from the file rather than a list kept here, because a list
    kept here is a list that goes stale: this leak has already been patched
    twice, one variable at a time, in `_isolate_live_credentials` below and in
    `tests/test_encryption.py`. A variable added to `.env` tomorrow cannot
    quietly start steering the suite.

    Nothing happens when there is no `.env` -- which is the case in CI, and is
    why CI never saw any of this.
    """
    names = dotenv.dotenv_values(os.path.join(_ROOT, '.env'))
    dotenv.load_dotenv = lambda *args, **kwargs: False
    for name in names:
        os.environ.pop(name, None)


# Before `app` is imported below, and therefore before `config` is imported and
# its class bodies read the environment. Import order is load-bearing here.
_isolate_from_developer_dotenv()

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.ai import EchoAdapter
from dough.tenancy import tenant_scope
from models import db


@pytest.fixture(autouse=True)
def _isolate_live_credentials(monkeypatch):
    """Tests must exercise sandbox mode by default regardless of what live
    credentials a developer happens to have. Tests that specifically want
    live-configured behavior re-set these via their own monkeypatch.setenv(...).

    `_isolate_from_developer_dotenv` above already removes anything named in
    `.env`, which is where these normally come from. This stays as the second
    layer, for credentials exported into the shell rather than written to the
    file — that path reaches `os.environ` without going through dotenv at all,
    and these five are the ones that cost real money or touch a real account.

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
