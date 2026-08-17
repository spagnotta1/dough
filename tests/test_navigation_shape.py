"""Where each destination lives in the chrome, and that none of them is lost.

The navigation was re-cut so that the rail carries *places* and the avatar
carries *you*: Upload, Rules, Connections and Sync History moved out of the
profile menu into a "See more" disclosure at the foot of the rail, and what
remained behind the avatar — the account, the household, the theme — went
under a single Settings entry.

Both halves of that move can break silently. A link dropped from one menu and
not added to the other leaves a page reachable only by typing its address; a
phone has no rail at all, so anything that lives only there is gone on the
device most of this product is read on. These tests read the rendered chrome
rather than base.html's source, because what matters is the markup a browser
is handed.
"""

import re

import pytest


#: The four that moved. Kept as one list so a fifth added later has to be
#: added here too — which is the moment somebody has to decide where on a
#: phone it goes.
SECONDARY = ['/upload', '/rules', '/connections', '/sync-history']


@pytest.fixture()
def page(tmp_path):
    """An app with auth off, so every route renders the signed-in chrome."""
    import finance_sync.scheduler as scheduler_module
    from app import create_app
    from dough.tenancy import tenant_scope
    from models import db

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'nav.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False,
        'AUTH_ENABLED': False})

    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def chrome(page):
    """The dashboard's markup — every page extends the same base."""
    return page.test_client().get('/').get_data(as_text=True)


def _block(body, pattern):
    match = re.search(pattern, body, re.DOTALL)
    assert match, f'the chrome no longer contains a block matching {pattern!r}'
    return match.group(1)


def _rail_more(body):
    return _block(body, r'<details id="rail-more">(.*?)</details>')


def _profile_menu(body):
    return _block(body, r'<div id="profile-menu"[^>]*>(.*?)</nav>')


def _mobile_menu(body):
    return _block(body, r'<div id="mobile-menu"[^>]*>(.*?)</div>\s*</div>')


def test_see_more_carries_the_four_that_left_the_profile_menu(chrome):
    more = _rail_more(chrome)
    for href in SECONDARY:
        assert f'href="{href}"' in more, f'{href} is not under "See more"'


def test_the_profile_menu_is_settings_only(chrome):
    """The avatar stops being a drawer for anything that had nowhere else.

    That drawer is the thing this change is against: nobody looks under their
    own initial for "import a CSV". If one of the four turns up here again the
    two menus disagree about what the avatar means.
    """
    menu = _profile_menu(chrome)
    for href in SECONDARY:
        assert f'href="{href}"' not in menu, \
            f'{href} is back in the profile menu — it belongs under "See more"'
    assert 'href="/settings"' in menu
    assert 'href="/household"' in menu


def test_sign_out_is_not_behind_the_settings_disclosure(chrome):
    """Signing out stays one click from opening the menu.

    It was reachable in one click before this change and has to stay that way:
    the person who most needs it is on a machine they want to leave.
    """
    menu = _profile_menu(chrome)
    group = _block(menu, r'<details id="pm-settings">(.*?)</details>')
    assert '/logout' in menu, 'sign out left the profile menu entirely'
    assert '/logout' not in group, 'sign out is buried inside Settings'


def test_the_secondary_destinations_survive_a_phone(chrome):
    """A phone has no rail, so "See more" cannot be the only way in.

    The same argument tests/test_insights_hub.py makes for the pages Insights
    absorbed. These four used to reach a phone through the profile menu; that
    menu is settings-only now, so the touch sheet has to carry them.
    """
    menu = _mobile_menu(chrome)
    for href in SECONDARY:
        assert f'href="{href}"' in menu, \
            f'{href} is on the rail and nowhere a phone can reach it'


def test_the_primary_seven_are_untouched(chrome):
    """The rail's first tier did not absorb the second.

    tests/test_insights_hub.py counts these anchors, and reads the block with
    a non-greedy regex that stops at the first `</div>` — so a wrapper element
    added inside #primary-nav truncates what that test sees to nothing while
    still passing here. This asserts the shape it depends on.
    """
    nav = _block(chrome, r'<div id="primary-nav">(.*?)</div>')
    assert nav.count('<a href=') == 7
    for href in SECONDARY:
        assert f'href="{href}"' not in nav, \
            f'{href} is a primary destination again — it is second-tier'
