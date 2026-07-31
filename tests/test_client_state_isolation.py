"""Two accounts in one browser must not share client-side state.

The server side of tenancy is covered elsewhere (`tools/verify_tenancy.py` and
the scoped-query tests). This file covers the half that lives in the browser,
which no amount of `household_id` filtering reaches: `localStorage` is keyed by
origin, so the theme, the dashboard and investments layouts, the chat's
conversation list and its message cache, and the recurring page's marks were all
shared by every account signed in from the same browser.

The cosmetic symptom is a theme following you between accounts. The one worth a
test is chat.js, which renders its cached conversation titles and message bodies
before the server answers and keeps them on screen if that request fails — so
one account could read another's financial conversations out of the cache, even
though the database never handed them over.

`base.html` fixes it by stamping the browser with the signed-in user's id and
clearing storage when the stamp changes. These tests assert the two properties
that make that work: the stamp actually differs per account, and it runs before
anything can read what it is meant to be throwing away.
"""

import re

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

PASSWORD = 'hunter2boat'
BASE_TEMPLATE = 'templates/base.html'


@pytest.fixture()
def app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'ALLOW_REGISTRATION': True,
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
def client(app):
    return app.test_client()


def _csrf(response):
    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _post(client, path, data, page_path=None):
    token = _csrf(client.get(page_path or path))
    return client.post(path, data={**data, '_csrf_token': token})


def _register(client, username, email):
    return _post(client, '/register',
                 {'username': username, 'email': email,
                  'password': PASSWORD, 'confirm': PASSWORD})


def _logout(client):
    return _post(client, '/logout', {}, page_path='/settings')


def _stamp(client, path='/settings'):
    """The identity `base.html` hands the browser, as the page renders it.

    Read out of the response rather than from the session, because the bug this
    guards was a template that rendered a value of the wrong *type*: an
    unparenthesised Jinja conditional emitted a bare number, which compares
    unequal to the string `localStorage.getItem` returns, and the purge then ran
    on every page load instead of on a change of account. Asserting on the
    rendered characters is what catches that; asserting on `user.id` would not.
    """
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'var who = (.+?);', html)
    return match.group(1) if match else None


def test_stamp_is_a_quoted_string_not_a_bare_number(client):
    """The value must be a JSON string, because getItem only returns strings."""
    _register(client, 'sal', 'sal@example.com')
    assert _stamp(client) == '"1"'


def test_each_account_stamps_the_browser_differently(client):
    """Two accounts, one client. The stamp is what tells them apart."""
    _register(client, 'spagnotta11', 'first@example.com')
    first = _stamp(client)
    _logout(client)

    _register(client, 'rankparsely', 'second@example.com')
    second = _stamp(client)

    assert first and second, 'both pages must carry a stamp'
    assert first != second, (
        'both accounts stamped the browser identically, so a change of account '
        'is undetectable on the client and their localStorage stays shared')


def test_signed_out_pages_do_not_clear_storage(client):
    """`null` means leave it alone.

    Clearing on the way past a signed-out page would reset the theme of
    everyone who ever signs out, and there is nothing to protect at that point:
    the purge belongs at the moment a *different* person appears.
    """
    _register(client, 'sal', 'sal@example.com')
    _logout(client)
    html = client.get('/login').get_data(as_text=True)
    assert 'var who = null' in html or 'var who = ' not in html


def test_purge_runs_before_anything_reads_storage():
    """Ordering is the whole mechanism, so it is asserted structurally.

    A purge that ran after the theme script would still be clearing the right
    keys, just too late to stop the previous account's values being read and
    applied first.
    """
    with open(BASE_TEMPLATE, encoding='utf-8') as handle:
        source = handle.read()

    stamp = source.find("'check-identity'")
    theme = source.find("'check-theme'")
    assert stamp != -1, 'the identity stamp is gone from base.html'
    assert theme != -1, 'this test is anchored to the theme key, which moved'
    assert stamp < theme, (
        'the theme script reads localStorage before the purge runs, so the '
        'previous account\'s theme is applied before it is discarded')


def test_purge_clears_wholesale_rather_than_by_key():
    """`clear()`, not a list of keys.

    An enumeration is the part that rots: the next key somebody adds is covered
    by being on the origin at all, and not by anyone remembering this file.
    """
    with open(BASE_TEMPLATE, encoding='utf-8') as handle:
        source = handle.read()
    assert 'localStorage.clear()' in source
    assert 'sessionStorage.clear()' in source
