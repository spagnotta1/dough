"""The categorization dialog — what a first-time connection actually looks like.

A freshly linked bank hands the automatic pass a household's whole history,
which it reads on the deep model. That is minutes of unattended work, and
before this dialog existed the only sign of it was the word "Categorizing…"
beside a spinner on the Connections page. Navigate away and even that was gone.

These tests drive the dialog off a stubbed ``/api/sync/status``, because the
thing under test is the browser half: whether the frames the scheduler
publishes become a bar somebody can read, and whether the finished report
survives long enough to be read. The server half — that the frames are
produced, that they count transactions and that they end at 100% — is
``tests/test_auto_categorize.py``.

Three properties are load-bearing:

- **It follows the user.** The pass outlives the page that started it, so the
  dialog lives outside ``<main>`` and a soft navigation must not destroy it.
- **It never traps anybody.** Minutes of background work behind a modal is a
  hostage situation; "keep going in the background" has to actually work.
- **It reports before the page reloads.** Connections reloads when everything
  is idle, and reloading out from under somebody still reading what Dough did
  would destroy the only place it was ever said.
"""

import json

from .conftest import visit, wait_for_layout

DIALOG = '#categorizing-dialog'


def _stub_status(page, frames):
    """Serve ``frames`` from ``/api/sync/status``, one per call, then repeat.

    A list rather than a single body because the dialog is a state machine over
    successive polls, and the transitions are what can break: it opens on the
    first frame that says ``categorizing``, and it is the *change* to idle that
    produces the finished report.
    """
    state = {'i': 0}

    def handler(route):
        i = min(state['i'], len(frames) - 1)
        state['i'] += 1
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps(frames[i]))

    page.route('**/api/sync/status', handler)


def _working(percent, done, total, first_run=True, phase='reading'):
    return {
        'running': False, 'categorizing': True, 'last_status': 'success',
        'connections': [], 'last_categorization': None,
        'categorization_progress': {
            'phase': phase, 'first_run': first_run, 'percent': percent,
            'batches_done': 1, 'batches_total': 4,
            'descriptions_done': done, 'descriptions_total': total,
            'transactions_done': done, 'transactions_total': total,
            'rules_added': 0, 'transactions_categorized': 0,
        },
    }


def _finished(categorized=412, rules=37, partial=False, remaining=0):
    return {
        'running': False, 'categorizing': False, 'last_status': 'success',
        'connections': [],
        'last_categorization': {
            'at': '2026-08-21T14:56:44', 'rules_added': rules,
            'transactions_categorized': categorized,
            'remaining_uncategorized': remaining, 'partial': partial,
            'skipped': False, 'first_run': True,
        },
        'categorization_progress': {
            'phase': 'done', 'first_run': True, 'percent': 100,
            'batches_done': 4, 'batches_total': 4,
            'descriptions_done': 480, 'descriptions_total': 480,
            'transactions_done': 1290, 'transactions_total': 1290,
            'rules_added': rules, 'transactions_categorized': categorized,
        },
    }


def _idle():
    """Nothing running and nothing ever categorized."""
    return {'running': False, 'categorizing': False, 'last_status': 'success',
            'connections': [], 'last_categorization': None,
            'categorization_progress': None}


def _open_dialog(page, frames):
    """Start a watch against `frames` and wait for the dialog to appear."""
    _stub_status(page, frames)
    page.evaluate('window.SyncWatch.start()')
    page.wait_for_selector(DIALOG + '[open]', timeout=10_000)


def test_it_shows_how_far_along_the_pass_is(signed_in):
    """The bar, the count and the copy all come off the same frame.

    The count is asserted in transactions rather than percent because that is
    the number that means something to the person watching: a percentage says
    how much of our batching is done, "412 of 1,290 transactions" says how much
    of their money has been read.
    """
    page = signed_in
    visit(page, '/')
    _open_dialog(page, [_working(32, 412, 1290)])
    wait_for_layout(page)

    dialog = page.locator(DIALOG)
    assert '412 of 1,290 transactions read' in dialog.inner_text()

    bar = page.locator(DIALOG + ' [data-cat-bar]')
    assert bar.get_attribute('aria-valuenow') == '32'
    # The fill has to be painted, not merely coloured: .ds-progress__fill
    # starts at width 0 and the whole component is invisible until something
    # sets it. That is the bug the design system's own comment records.
    assert page.evaluate(
        "() => document.querySelector('#categorizing-dialog [data-cat-fill]')"
        ".getBoundingClientRect().width") > 0


def test_the_bar_moves_as_the_pass_reads(signed_in):
    page = signed_in
    visit(page, '/')
    _open_dialog(page, [_working(10, 129, 1290), _working(75, 967, 1290)])

    page.wait_for_function(
        "() => document.querySelector('#categorizing-dialog [data-cat-bar]')"
        ".getAttribute('aria-valuenow') === '75'", timeout=10_000)
    assert '967 of 1,290 transactions read' in page.locator(DIALOG).inner_text()


def test_it_reports_what_it_did_and_waits_to_be_dismissed(signed_in):
    """The finished report is the whole point of holding the idle event.

    Connections reloads on ``check:sync-idle``. If that fired the moment the
    pass ended, the report would be replaced by a page load before anyone read
    a word of it — which is exactly what the toast it replaces used to do.
    """
    page = signed_in
    visit(page, '/')
    page.evaluate("""() => {
      window.__idle = [];
      document.addEventListener('check:sync-idle',
                                (e) => window.__idle.push(e.detail));
    }""")
    _open_dialog(page, [_working(90, 1161, 1290), _finished()])

    page.wait_for_selector(DIALOG + ' [data-cat-done]:not([hidden])', timeout=10_000)
    text = page.locator(DIALOG).inner_text()
    assert 'All sorted' in text
    assert '412 transactions' in text
    assert '37 category rules' in text

    assert page.evaluate('window.__idle.length') == 0, (
        'the idle event fired while the report was still on screen — anything '
        'listening for it would have reloaded the page out from under the user')

    page.click(DIALOG + ' [data-cat-done]')
    page.wait_for_function('() => window.__idle.length === 1', timeout=5_000)
    assert page.evaluate('window.__idle[0].reported') is True
    assert not page.locator(DIALOG + '[open]').count()


def test_a_partial_pass_says_so(signed_in):
    """A half-read history is a known state, not a mystery.

    Without this line the user sees bare rows and cannot tell "I never got to
    these" from "I could not place these" — and the fix is different for each.
    """
    page = signed_in
    visit(page, '/')
    _open_dialog(page, [_working(90, 1161, 1290), _finished(partial=True)])

    page.wait_for_selector(DIALOG + ' [data-cat-done]:not([hidden])', timeout=10_000)
    assert 'Analyze' in page.locator(DIALOG + ' [data-cat-note]').inner_text()


def test_it_says_what_it_could_not_place(signed_in):
    """"Not read yet" and "read and could not place" are different problems.

    From the ledger they look the same — a row with no category — and the fix
    is different for each, so the report names which one this is. Without it a
    tester's only reading of a bare row is that the import went wrong.
    """
    page = signed_in
    visit(page, '/')
    _open_dialog(page, [_working(90, 1161, 1290), _finished(remaining=53)])

    page.wait_for_selector(DIALOG + ' [data-cat-done]:not([hidden])', timeout=10_000)
    note = page.locator(DIALOG + ' [data-cat-note]').inner_text()
    assert '53 transactions are still uncategorized' in note


def test_it_can_be_dismissed_and_the_work_carries_on(signed_in):
    """Minutes of background work behind a modal would be a hostage situation.

    Dismissing has to be real in both directions: the panel goes away and stays
    away — a modal that reappears on the next poll is what teaches people to
    dismiss things without reading them — while the watch underneath keeps
    running, because it is what notices the pass ending.
    """
    page = signed_in
    visit(page, '/')
    _open_dialog(page, [_working(20, 258, 1290), _working(55, 710, 1290)])

    page.click(DIALOG + ' [data-cat-hide]')
    assert not page.locator(DIALOG + '[open]').count()
    page.locator('#toast-container .ds-toast',
                 has_text='keep sorting in the background').wait_for(timeout=5_000)

    # Still watching, and still closed, several polls later.
    page.wait_for_function(
        "() => window.SyncWatch.status() && "
        "window.SyncWatch.status().categorization_progress.percent === 55",
        timeout=10_000)
    assert not page.locator(DIALOG + '[open]').count(), (
        'the next progress frame reopened a dialog the user had dismissed')


def test_a_dismissed_pass_still_reports_what_it_did(signed_in):
    """Being left alone is not the same as being kept in the dark.

    Somebody who hid the panel has said they do not want it back, so the result
    arrives as a toast rather than as a modal reopening minutes later — but it
    does arrive, and `reported` still tells Connections not to say it twice.
    """
    page = signed_in
    visit(page, '/')
    page.evaluate("""() => {
      window.__idle = [];
      document.addEventListener('check:sync-idle',
                                (e) => window.__idle.push(e.detail));
    }""")
    _open_dialog(page, [_working(90, 1161, 1290), _finished()])

    page.click(DIALOG + ' [data-cat-hide]')
    page.locator('#toast-container .ds-toast',
                 has_text='412 transactions').wait_for(timeout=10_000)

    assert not page.locator(DIALOG + '[open]').count()
    page.wait_for_function('() => window.__idle.length === 1', timeout=5_000)
    assert page.evaluate('window.__idle[0].reported') is True


def test_a_quiet_pass_never_interrupts_anybody(signed_in):
    """A refresh that categorized nothing is a non-event.

    Popping a modal to report it would train people to dismiss the one that
    matters, so the dialog only ever appears while there is a pass in flight.
    """
    page = signed_in
    visit(page, '/')
    _stub_status(page, [_idle()])
    page.evaluate("""() => {
      window.__idle = [];
      document.addEventListener('check:sync-idle',
                                (e) => window.__idle.push(e.detail));
      window.SyncWatch.start();
    }""")

    page.wait_for_function('() => window.__idle.length === 1', timeout=10_000)
    assert page.evaluate('window.__idle[0].reported') is False
    assert not page.locator(DIALOG + '[open]').count()


def test_the_dialog_survives_a_soft_navigation(signed_in):
    """The pass outlives the page, so the dialog has to as well.

    This is the reason it lives outside ``<main>``: the SPA layer replaces that
    element wholesale, and a dialog inside it would be destroyed by the first
    click on the rail — mid-pass, with no way back to the report.
    """
    page = signed_in
    visit(page, '/')
    _open_dialog(page, [_working(40, 516, 1290)])

    # Clicked through the DOM rather than with the mouse, because the dialog is
    # doing its job: a modal intercepts pointer events over the whole page, and
    # the real user's route to this navigation is to dismiss it first. What is
    # under test is the swap, not the pointer.
    page.evaluate(
        "() => document.querySelector('a[href=\"/transactions\"]').click()")
    page.wait_for_function("() => location.pathname === '/transactions'",
                           timeout=10_000)

    assert page.locator(DIALOG + '[open]').count(), (
        'the dialog was destroyed by a soft navigation: it is inside <main>, '
        'which spaNavigate() replaces wholesale')
    assert '516 of 1,290 transactions read' in page.locator(DIALOG).inner_text()
