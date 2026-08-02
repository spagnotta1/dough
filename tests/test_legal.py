"""The privacy policy and the terms of service.  [Phase 10.7]

These pages carry no logic, so there is little to test about their *behaviour*.
What is worth testing is everything around them, and it divides into three
kinds:

1. **They are reachable by the people who need them.** Signed out, with
   authentication on, and with registration closed. A policy behind a login is
   a policy nobody can read before agreeing to it, and Plaid's production review
   fetches the privacy URL anonymously.

2. **They do not ship with invented facts.** The operating entity, the contact
   address and the governing jurisdiction are configuration. An unset one has to
   render as a visible marker, because the failure being prevented is a policy
   that reads as finished and names nobody — which is the state in which one
   gets shipped.

3. **They say the things this product specifically has to say.** Two of these
   are not boilerplate: the assistant is a language model whose output can be
   wrong, and connected balances come from third parties. Those clauses exist
   because of features this application actually has, so they are pinned rather
   than left to survive the next edit on trust.
"""

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app


@pytest.fixture()
def legal_app(tmp_path):
    """Authentication on, registration closed — the invite-only launch shape."""
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'legal.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/privacy', '/terms'])
def test_a_signed_out_visitor_can_read_them(legal_app, path):
    """The whole point. No session, no redirect, no 403."""
    response = legal_app.test_client().get(path)
    assert response.status_code == 200
    assert b'Dough' in response.data


@pytest.mark.parametrize('path', ['/privacy', '/terms'])
def test_they_are_served_even_with_authentication_disabled(tmp_path, path):
    """`legal` is registered unconditionally, unlike `auth` and `household`.

    Those are conditional because with authentication off there is nobody to
    sign in. That reasoning does not reach these: a policy describes what the
    software does with data, which is true whether or not anyone can log in.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': False,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'noauth.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        assert application.test_client().get(path).status_code == 200
    scheduler_module._scheduler = None


def test_each_page_links_to_the_other(legal_app):
    """A reader who arrives at one needs the other; they are one agreement."""
    client = legal_app.test_client()
    assert b'/terms' in client.get('/privacy').data
    assert b'/privacy' in client.get('/terms').data


def test_the_landing_page_offers_both(legal_app):
    """Where the decision to hand over a bank connection is actually made.

    An installation with no owner redirects `/` to `/setup`, so the landing page
    only exists once somebody has been created. The account is made and then
    signed out of, which is the state a visitor arrives in.
    """
    import re

    client = legal_app.test_client()
    page = client.get('/setup')
    token = re.search(r'name="_csrf_token" value="([^"]+)"',
                      page.get_data(as_text=True))
    client.post('/setup', data={'username': 'sal', 'password': 'hunter2boat',
                                'confirm': 'hunter2boat',
                                '_csrf_token': token.group(1) if token else ''})

    visitor = legal_app.test_client()
    body = visitor.get('/').get_data(as_text=True)
    assert '/privacy' in body
    assert '/terms' in body


def test_the_registration_form_names_what_is_being_agreed_to(tmp_path):
    """The consent moment, and the only place it can honestly be recorded.

    A Terms of Service nobody was shown before signing up is materially weaker,
    and the fix costs one sentence. This asserts the sentence is on the form
    itself rather than only in a footer somewhere else on the site.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'ALLOW_REGISTRATION': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'reg.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        body = application.test_client().get('/register').get_data(as_text=True)
        assert 'agree' in body.lower()
        assert '/terms' in body
        assert '/privacy' in body
    scheduler_module._scheduler = None


# ---------------------------------------------------------------------------
# No invented facts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/privacy', '/terms'])
def test_unset_placeholders_render_visibly_rather_than_as_nothing(legal_app, path):
    """An unset entity name must look unset.

    Rendering empty would produce "Dough is operated by ." — a sentence that
    reads as complete and names nobody, which is precisely the version that
    survives review and ships. The marker is ugly on purpose.
    """
    body = legal_app.test_client().get(path).get_data(as_text=True)
    assert 'LEGAL_ENTITY' in body or 'OPERATING ENTITY' in body


@pytest.mark.parametrize('path', ['/privacy', '/terms'])
def test_configured_values_replace_the_markers(tmp_path, path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'LEGAL_ENTITY': 'Dough Financial LLC',
        'LEGAL_CONTACT_EMAIL': 'privacy@dough-financial.com',
        'LEGAL_JURISDICTION': 'the State of New York',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'set.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        body = application.test_client().get(path).get_data(as_text=True)
        assert 'Dough Financial LLC' in body
        assert 'privacy@dough-financial.com' in body
        assert 'OPERATING ENTITY' not in body
    scheduler_module._scheduler = None


def test_the_defaults_are_empty_rather_than_a_plausible_guess():
    """There is no safe default for who accepts liability.

    A default of "Dough" or "Dough Financial" would render a complete-looking
    policy naming an entity that may not exist, which is worse than a blank
    because a blank gets filled.
    """
    from config import BaseConfig

    assert BaseConfig.LEGAL_ENTITY == ''
    assert BaseConfig.LEGAL_CONTACT_EMAIL == ''
    assert BaseConfig.LEGAL_JURISDICTION == ''


# ---------------------------------------------------------------------------
# The clauses this product specifically needs
# ---------------------------------------------------------------------------

def test_the_terms_disclaim_financial_advice(legal_app):
    """The clause the whole category needs, and this one especially.

    The application renders budgets, anomaly flags and an investment brief, and
    an assistant that will discuss them in the second person. Without this it
    reads as advice.
    """
    body = legal_app.test_client().get('/terms').get_data(as_text=True).lower()
    assert 'not financial advice' in body or 'not a financial adviser' in body
    assert 'recommendation' in body


def test_the_terms_say_the_assistant_can_be_wrong(legal_app):
    """Not boilerplate: it is true, and it is the product's main risk to a user.

    A language model stating a confident wrong number about somebody's savings
    is the failure mode this product has that a spreadsheet does not.
    """
    body = legal_app.test_client().get('/terms').get_data(as_text=True).lower()
    assert 'language model' in body
    assert 'wrong' in body


def test_the_terms_say_institution_data_is_not_authoritative(legal_app):
    body = legal_app.test_client().get('/terms').get_data(as_text=True).lower()
    assert 'plaid' in body
    assert 'authoritative' in body or 'reconcile' in body


def test_the_privacy_policy_discloses_plaid_and_that_we_never_see_credentials(
        legal_app):
    """Both halves. The second is the one a reader actually wants to know."""
    body = legal_app.test_client().get('/privacy').get_data(as_text=True)
    assert 'Plaid' in body
    lowered = body.lower()
    assert 'never sees' in lowered or 'never see' in lowered


def test_the_privacy_policy_discloses_that_financial_data_reaches_the_model(
        tmp_path):
    """The disclosure that would be easiest to leave out and worst to omit.

    Sending somebody's transaction history to a third-party model is the single
    most surprising thing this application does with their data. The section is
    conditional on the feature being configured, so the test configures it.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'ANTHROPIC_API_KEY': 'sk-ant-test-not-a-real-key',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'ai.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        body = application.test_client().get('/privacy').get_data(as_text=True)
        assert 'Anthropic' in body
        assert 'sent to Anthropic' in body
    scheduler_module._scheduler = None


def test_the_privacy_policy_states_deletion_and_export_rights(legal_app):
    """The rights only exist if the page says how to use them."""
    body = legal_app.test_client().get('/privacy').get_data(as_text=True).lower()
    assert 'export' in body
    assert 'delete your account' in body
    assert 'retention' in body or 'how long we keep' in body


def test_no_secret_reaches_a_legal_page(tmp_path):
    """These render config, so it is worth proving *which* config.

    `_context()` reads three keys by name. A future edit that passed the whole
    config into the template would put the Anthropic key and the mail password
    one typo away from a public page.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'ANTHROPIC_API_KEY': 'sk-ant-secret-value-here',
        'MAIL_PASSWORD': 'postmark-token-secret',
        'SECRET_KEY': 'session-signing-secret',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'sec.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        for path in ('/privacy', '/terms'):
            body = application.test_client().get(path).get_data(as_text=True)
            assert 'sk-ant-secret-value-here' not in body
            assert 'postmark-token-secret' not in body
            assert 'session-signing-secret' not in body
    scheduler_module._scheduler = None
