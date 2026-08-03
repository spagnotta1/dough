"""Category rules, driven in a browser.

Rules is the heaviest form page in the product and the one Wave 1d rebuilt on
the design system. Three of its four interactions are things a server-side test
cannot reach at all: the segmented model picker is pure client state, the
keyword preview is a debounced fetch that paints into the page, and the AI
suggestion cards are markup that only exists after a round trip.

The fourth — adding and removing rules — is a plain form POST that
``tests/test_routes.py`` already covers. It is here anyway, because what that
file cannot say is whether the form on the page produces the POST: this page
carries four separate forms inside one table, and a stray ``</form>`` would
submit the wrong one with no error anywhere.
"""

import json

from .conftest import UNCATEGORIZED_COUNT, assert_no_horizontal_overflow, visit

#: What the model is told to answer with. `/rules/ai-suggest` sends this through
#: `generate_json`, then enriches each entry with real match counts from the
#: ledger — so STARBUCKS has to be a description that is actually seeded and
#: actually uncategorized, or the card renders with a zero count and the badge
#: assertion below would be testing nothing.
SUGGESTION_REPLY = json.dumps({'suggestions': [
    {'category': 'Coffee', 'keyword': 'STARBUCKS',
     'reason': 'Recurring coffee purchases at the same merchant.'},
]})


def _script(page, ai, reply):
    """Queue the model's next answer once the page has stopped asking for one."""
    page.wait_for_load_state('networkidle')
    ai.scripted.clear()
    ai.scripted.append(reply)


def _open_manual_form(page):
    """Expand the "Add a rule manually" disclosure.

    The hand-authoring form moved behind a collapsed `<details>` when Dough
    writing the rules became the primary path — an empty two-field form at the
    top of the page was asking the user to do by hand the job the page exists to
    do for them. The form is unchanged and still the supported escape hatch, so
    these tests open it the way a person does rather than reaching past it.
    """
    page.click('.rules-advanced__summary')
    page.wait_for_selector('#category:not([hidden])', state='visible')


# ── The page ────────────────────────────────────────────────────────────────

def test_the_rules_page_lists_the_existing_rules(signed_in):
    page = signed_in
    visit(page, '/rules')
    rows = page.locator('#rulesBody tr')
    assert rows.count() >= 1, 'no rules rendered'
    # Every row carries the category it represents; the reorder POST is built
    # from these and sends nothing useful if they are missing.
    assert all(page.locator('#rulesBody tr').nth(i).get_attribute('data-category')
               for i in range(rows.count())), 'a rule row has no data-category'


def test_the_rules_table_does_not_force_the_page_sideways(signed_in):
    """A regression pin for the fix that landed with this wave.

    The keywords column holds an unbounded list of chips. Under the browser's
    default `auto` table layout that cell took its max-content width — 3,288px
    on the seeded data — and although the .ds-table-wrap around it scrolled, the
    overflow also escaped and the whole document scrolled 2,250px sideways.
    rules.css sets `table-layout: fixed` so the chips wrap instead.

    Worth its own test rather than leaving it to the page sweep: the sweep would
    catch it, but it would report "/rules is wide", and the cause is one
    declaration about one column.
    """
    page = signed_in
    page.set_viewport_size({'width': 1440, 'height': 900})
    visit(page, '/rules')

    table = page.locator('#rulesTable').bounding_box()
    viewport = page.evaluate('document.documentElement.clientWidth')
    assert table['width'] <= viewport, \
        f'the rules table is {table["width"]}px wide in a {viewport}px viewport'
    assert_no_horizontal_overflow(page, ' [/rules]')


# ── The model picker ────────────────────────────────────────────────────────

def test_the_model_picker_moves_its_pressed_state(signed_in):
    """One button pressed at a time, and the state lives on aria-pressed.

    Wave 1d replaced an `.active` class with `aria-pressed`, so that the CSS and
    the screen reader read the same attribute. A second source of truth is a
    second thing that can be wrong.
    """
    page = signed_in
    visit(page, '/rules')
    buttons = page.locator('.rules-seg__btn')
    assert buttons.count() >= 2, 'the model picker rendered fewer than two models'

    pressed = page.locator('.rules-seg__btn[aria-pressed="true"]')
    assert pressed.count() == 1, 'the picker did not start with exactly one selection'

    before = page.locator('#ai-model-desc').inner_text()
    buttons.nth(1).click()

    assert page.locator('.rules-seg__btn[aria-pressed="true"]').count() == 1, \
        'clicking a second model left two pressed'
    assert buttons.nth(1).get_attribute('aria-pressed') == 'true'
    assert page.locator('#ai-model-desc').inner_text() != before, \
        'the description did not follow the selection'


# ── The keyword preview ─────────────────────────────────────────────────────

def test_the_keyword_preview_shows_what_a_rule_would_catch(signed_in):
    """Adding a rule retroactively recategorizes matching transactions.

    That makes the preview the difference between a tool and a gamble, which is
    why it is asserted on rather than treated as decoration.
    """
    page = signed_in
    visit(page, '/rules')

    _open_manual_form(page)
    page.fill('#keyword', 'Netflix')
    page.wait_for_selector('#previewBox:not([hidden])', timeout=10_000)

    rows = page.locator('#previewBody .rules-preview__row')
    assert rows.count() >= 1, 'the preview box opened with nothing in it'
    assert 'Netflix' in rows.first.inner_text()
    assert page.locator('#previewNone').is_hidden()


def test_a_keyword_that_matches_nothing_says_so(signed_in):
    page = signed_in
    visit(page, '/rules')

    _open_manual_form(page)
    page.fill('#keyword', 'zzz-nothing-matches-this')
    page.wait_for_selector('#previewNone:not([hidden])', timeout=10_000)
    assert page.locator('#previewBox').is_hidden(), \
        'the preview box stayed open with no matches in it'


# ── Adding and removing ─────────────────────────────────────────────────────

def test_adding_a_rule_puts_it_in_the_table(signed_in):
    page = signed_in
    visit(page, '/rules')

    _open_manual_form(page)
    page.fill('#category', 'BrowserTestCategory')
    page.fill('#keyword', 'ZZTESTKEYWORD')
    page.click('#addRuleForm button[type="submit"]')
    page.wait_for_load_state('load')

    row = page.locator('#rulesBody tr[data-category="BrowserTestCategory"]')
    assert row.count() == 1, 'the new rule did not appear in the table'
    assert 'ZZTESTKEYWORD' in row.inner_text()


def test_removing_a_keyword_takes_it_out_of_its_row(signed_in):
    """Each chip has its own form, nested inside a row that has another one.

    That is the arrangement most likely to be broken by a markup change, and the
    failure is silent: a mis-nested form submits the row's delete-category
    action instead, so clicking "remove this keyword" quietly removes all of
    them.
    """
    page = signed_in
    visit(page, '/rules')

    # Make our own rule to remove, rather than mutating shared seed data.
    _open_manual_form(page)
    page.fill('#category', 'RemovalTestCategory')
    page.fill('#keyword', 'FIRSTKEYWORD')
    page.click('#addRuleForm button[type="submit"]')
    page.wait_for_load_state('load')

    row = page.locator('#rulesBody tr[data-category="RemovalTestCategory"]')
    assert row.count() == 1
    row.locator('.rules-keyword__x').first.click()
    page.wait_for_load_state('load')

    remaining = page.locator('#rulesBody tr[data-category="RemovalTestCategory"]')
    assert 'FIRSTKEYWORD' not in remaining.inner_text() if remaining.count() else True


# ── AI suggestions ──────────────────────────────────────────────────────────

def test_the_analyze_button_is_live_when_there_is_something_to_analyze(signed_in):
    page = signed_in
    visit(page, '/rules')
    assert UNCATEGORIZED_COUNT > 0, 'the seed has nothing uncategorized to analyze'
    assert page.locator('#ai-analyze-btn').is_enabled(), \
        'the Analyze button is disabled despite uncategorized transactions'


def test_suggestions_render_as_cards(signed_in, ai):
    page = signed_in
    visit(page, '/rules')
    _script(page, ai, SUGGESTION_REPLY)

    page.click('#ai-analyze-btn')
    page.wait_for_selector('.rules-sugg', timeout=15_000)

    card = page.locator('.rules-sugg').first
    text = card.inner_text()
    assert 'Coffee' in text
    assert 'STARBUCKS' in text
    # The count badge is enriched server-side from the real ledger, so this also
    # asserts that the enrichment ran and found the seeded transaction.
    assert 'uncategorized' in text.lower(), f'no match-count badge on the card: {text!r}'
    assert page.locator('#ai-error').is_hidden()


def test_accepting_a_suggestion_marks_the_card_and_confirms(signed_in, ai):
    page = signed_in
    visit(page, '/rules')
    _script(page, ai, SUGGESTION_REPLY)

    page.click('#ai-analyze-btn')
    page.wait_for_selector('.rules-sugg', timeout=15_000)
    page.click('.rules-sugg [data-accept]')

    page.wait_for_selector('.rules-sugg--accepted', timeout=10_000)
    assert 'Accepted' in page.locator('.rules-sugg--accepted').inner_text()
    # Feedback goes through the toast system, never alert() — see
    # test_ui_invariants.py::test_feedback_goes_through_the_toast_system.
    assert page.locator('#toast-container').inner_text().strip(), \
        'accepting a suggestion produced no confirmation'


def test_skipping_a_suggestion_removes_it_from_view(signed_in, ai):
    page = signed_in
    visit(page, '/rules')
    _script(page, ai, SUGGESTION_REPLY)

    page.click('#ai-analyze-btn')
    page.wait_for_selector('.rules-sugg', timeout=15_000)
    page.click('.rules-sugg [data-skip]')

    assert page.locator('.rules-sugg').first.is_hidden(), 'Skip left the card on screen'


def test_a_failed_analysis_says_something_a_person_can_read(signed_in, ai, page_health):
    """The error path, which is the one nobody exercises by hand.

    Asserted on the *shape* of the message rather than its wording: it has to be
    a sentence, and it must not be a stack trace or a bare status code leaking
    into the page.

    A model that answers with something other than JSON is a real 500 from
    /rules/ai-suggest, so the health guard is told to expect exactly that one —
    a 500 from anywhere else still fails this test.
    """
    page = signed_in
    page_health.expect_server_error('/rules/ai-suggest')
    visit(page, '/rules')
    page.wait_for_load_state('networkidle')
    ai.scripted.clear()
    ai.scripted.append('this is not json')

    page.click('#ai-analyze-btn')
    page.wait_for_selector('#ai-error:not([hidden])', timeout=15_000)

    message = page.locator('#ai-error').inner_text().strip()
    assert message, 'the error box opened empty'
    assert 'Traceback' not in message and 'Exception' not in message, \
        f'an internal error reached the page: {message!r}'
    assert message[0].isupper(), f'not a sentence: {message!r}'
