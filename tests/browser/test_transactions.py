"""The ledger: filtering, and the edit dialog.

Transactions was the first page migrated to the design system (Phase 9 Wave 1a),
and the edit dialog was rebuilt on a native ``<dialog>`` at the same time. Both
of those changes are the kind that a template test signs off on and a browser
disagrees with: ``tests/test_ui_invariants.py`` can see that the markup says
``<dialog class="ds-dialog">``, but only an engine can say whether calling
``showModal()`` on it puts anything on the screen.

The filter assertions are here for a different reason. The filters are a GET
form, so every one of them is already covered server-side by
``tests/test_routes.py`` — what is *not* covered anywhere else is that the form
on the page produces those query strings, which is a property of the markup and
the browser's form serialisation rather than of the view.
"""

from .conftest import TRANSACTION_COUNT, visit

ROWS = 'table tbody tr'


def _descriptions(page):
    """The description cell of every row currently rendered."""
    return page.locator(f'{ROWS} td:nth-child(4)').all_inner_texts()


def test_the_ledger_lists_the_seeded_transactions(signed_in):
    visit(signed_in, '/transactions')
    assert signed_in.locator(ROWS).count() == TRANSACTION_COUNT, \
        'the seeded ledger did not render'
    assert any('Netflix' in d for d in _descriptions(signed_in))


def test_filtering_by_account_narrows_the_ledger(signed_in):
    page = signed_in
    visit(page, '/transactions')
    before = page.locator(ROWS).count()

    page.select_option('#account', 'Visa')
    page.click('#txnFilterForm button[type="submit"]')
    page.wait_for_load_state('load')

    after = page.locator(ROWS).count()
    assert 0 < after < before, f'filtering to one account changed nothing ({before} → {after})'
    accounts = page.locator(f'{ROWS} td:nth-child(5)').all_inner_texts()
    assert all('Visa' in a for a in accounts), f'the account filter leaked other rows: {accounts}'
    # The filter has to survive into the URL, or it cannot be linked or bookmarked.
    assert 'account=Visa' in page.url


def test_searching_matches_on_the_description(signed_in):
    page = signed_in
    visit(page, '/transactions')

    page.fill('#search', 'Netflix')
    page.click('#txnFilterForm button[type="submit"]')
    page.wait_for_load_state('load')

    descriptions = _descriptions(page)
    assert descriptions, 'searching for a description that exists returned nothing'
    assert all('Netflix' in d for d in descriptions)


def test_clearing_the_filters_restores_the_whole_ledger(signed_in):
    page = signed_in
    visit(page, '/transactions')

    page.select_option('#account', 'Visa')
    page.click('#txnFilterForm button[type="submit"]')
    page.wait_for_load_state('load')
    assert page.locator(ROWS).count() < TRANSACTION_COUNT

    page.click('a:has-text("Clear")')
    page.wait_for_load_state('load')
    assert page.locator(ROWS).count() == TRANSACTION_COUNT, \
        'Clear did not restore the unfiltered ledger'


# ── The edit dialog ─────────────────────────────────────────────────────────

def _open_first_edit(page):
    page.locator('button[aria-label^="Edit transaction"]').first.click()
    dialog = page.locator('#editModal')
    dialog.wait_for(state='visible', timeout=5_000)
    return dialog


def test_the_edit_dialog_opens_as_a_real_modal(signed_in):
    """`showModal()` and not just an `open` attribute.

    The difference is the whole reason the dialog was rebuilt: a modal dialog
    gets a focus trap, Esc, a backdrop and inertness of the page behind it from
    the platform. `[open]` alone gets none of those, and looks identical in a
    screenshot.
    """
    page = signed_in
    visit(page, '/transactions')
    dialog = _open_first_edit(page)

    assert dialog.evaluate('d => d.matches(":modal")'), \
        'the dialog is open but not modal — showModal() was not what opened it'
    # Focus is inside the dialog, which is what makes the trap meaningful.
    assert page.evaluate(
        '() => document.getElementById("editModal").contains(document.activeElement)'), \
        'focus stayed outside the dialog after it opened'


def test_the_edit_dialog_is_populated_from_the_row_it_was_opened_from(signed_in):
    page = signed_in
    visit(page, '/transactions')

    first_description = _descriptions(page)[0]
    _open_first_edit(page)

    assert page.input_value('#edit_description') == first_description, \
        'the dialog opened on a different transaction than the row that was clicked'
    assert page.input_value('#edit_amount'), 'the amount field came up empty'


def test_escape_closes_the_edit_dialog(signed_in):
    page = signed_in
    visit(page, '/transactions')
    dialog = _open_first_edit(page)

    page.keyboard.press('Escape')
    dialog.wait_for(state='hidden', timeout=5_000)
    assert not dialog.evaluate('d => d.open')


def test_cancel_closes_the_edit_dialog_without_saving(signed_in):
    page = signed_in
    visit(page, '/transactions')

    before = _descriptions(page)[0]
    dialog = _open_first_edit(page)
    page.fill('#edit_description', 'Typed then abandoned')
    page.click('#editModal button:has-text("Cancel")')
    dialog.wait_for(state='hidden', timeout=5_000)

    assert _descriptions(page)[0] == before, 'Cancel wrote the edit anyway'


def test_saving_an_edit_updates_the_row_in_place(signed_in):
    """The row has to change without a reload.

    `saveEdit()` PUTs and then rewrites the cells itself, which means this
    exercises two things a server-side test cannot reach: that the PUT carries
    the CSRF header base.html's fetch wrapper is responsible for adding, and
    that the row the answer is written back into is the right one.
    """
    page = signed_in
    visit(page, '/transactions')

    dialog = _open_first_edit(page)
    page.fill('#edit_description', 'Renamed by a browser test')
    page.click('#editModal button:has-text("Save Changes")')
    dialog.wait_for(state='hidden', timeout=5_000)

    assert _descriptions(page)[0] == 'Renamed by a browser test', \
        'the row did not pick up the saved description'

    # And it persisted, rather than only being painted into the DOM.
    page.reload(wait_until='load')
    assert 'Renamed by a browser test' in _descriptions(page), \
        'the edit disappeared on reload — the PUT did not stick'
