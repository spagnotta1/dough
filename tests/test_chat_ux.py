"""Acceptance checklist for the chat surface.

Chat is the flagship AI experience, and it is the one page where the product
makes claims about someone's money in prose. The invariants in
test_ui_invariants.py say the page is *built* from the design system;
test_chat_shell.py says the Wave 1b seams still line up. Neither says the
conversation itself is any good.

This file is the third thing: an acceptance checklist, written as tests. Each
one names a property a user would notice the absence of, and pins it to the
line of CSS or JavaScript that provides it. They are static checks — a real
browser is where the mobile and streaming items ultimately have to be proven,
and Phase 11's Playwright suite is where that belongs.

Three items on the checklist are deliberately *not* tested here:

  * "Streaming feels continuous" and "partial responses survive disconnects"
    are timing properties. What is asserted below is the weaker, checkable
    thing: the code paths that make them possible exist and keep what
    arrived.
  * "No fake certainty styling" is a content judgement about what Dough says,
    not about how it is painted. The nearest checkable proxy — that the page
    tells the user Dough can be wrong — is asserted.
  * Citations are not built yet. The checklist item is about what happens
    when they are, so there is nothing here to hold to it.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHAT_JS = (ROOT / 'static' / 'js' / 'chat.js').read_text(encoding='utf-8')
CHAT_CSS = (ROOT / 'static' / 'css' / 'chat.css').read_text(encoding='utf-8')
DS_CSS = (ROOT / 'static' / 'css' / 'design-system.css').read_text(encoding='utf-8')


def _rules(css, selector):
    """Every declaration block whose selector list mentions `selector`."""
    out = []
    for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        if selector in match.group(1):
            out.append(match.group(2))
    return out


def _phone_block(css):
    """Chat's touch media query, whatever width it is currently set at.

    Found by the PHONE / TOUCH marker rather than by the number. The number
    moved once already — 860px to 1023.98px, when it had to be made to agree
    with base.html's — and a helper that hard-codes it turns that kind of change
    into a puzzling failure in a file about something else.
    """
    marker = css.index('PHONE / TOUCH')
    start = css.index('@media (max-width:', marker)
    depth, i = 0, css.index('{', start)
    for j in range(i, len(css)):
        if css[j] == '{':
            depth += 1
        elif css[j] == '}':
            depth -= 1
            if depth == 0:
                return css[i:j]
    raise AssertionError('the phone media query is unbalanced')


# ── Conversation experience ──────────────────────────────────────────────

def test_a_user_turn_and_an_answer_do_not_look_alike():
    """The first thing a reader has to be able to do is tell who is talking.

    Two channels, because either alone fails someone: the user's turn is a
    right-aligned bubble, and every answer carries a byline with Dough's face.
    """
    assert '.u-bubble' in CHAT_CSS, 'the user bubble is gone'
    assert _rules(CHAT_CSS, '.turn.user .col'), \
        'user turns are no longer aligned apart from answers'
    assert 'doughFrom()' in CHAT_JS, 'answers lost their byline'
    # Shape, not just alignment: the bubble's corner points at its author.
    bubble = ' '.join(_rules(CHAT_CSS, '.u-bubble'))
    assert 'border-radius' in bubble and len(bubble.split('border-radius')[1].split(';')[0].split()) > 1, \
        'the user bubble is no longer asymmetric, so shape stops carrying authorship'


def test_there_is_a_visible_thinking_state_before_the_first_token():
    """The gap between sending and the first token is the moment a user
    decides whether the thing is broken."""
    assert 'thinking' in CHAT_JS and 'thinkingLine()' in CHAT_JS
    assert _rules(CHAT_CSS, '.thinking-label'), 'the thinking shimmer is gone'
    assert 'data-dough="thinking"' in CHAT_JS, 'Dough no longer does the waiting'


def test_streaming_is_visibly_live():
    """A caret trailing the text is what distinguishes "still arriving" from
    "finished and short"."""
    assert _rules(CHAT_CSS, '.streaming'), 'the streaming caret is gone'
    assert 'firstToken()' in CHAT_JS, \
        'nothing swaps the thinking state for the first token'


def test_an_interrupted_answer_keeps_what_arrived():
    """Both ways a stream can end early — the user presses Stop, or the
    connection drops mid-read — must commit the partial rather than discard
    it. Throwing away a half-written answer about someone's money and leaving
    an empty turn is the worst available outcome."""
    assert re.search(r"if \(acc\) commit\(\); else turn\.remove\(\);", CHAT_JS), \
        'pressing Stop no longer keeps the partial answer'
    assert re.search(r'try \{ chunk = await reader\.read\(\); \} catch \(e\) \{ break; \}', CHAT_JS), \
        'a mid-stream read failure no longer falls through to the commit path'


def test_failures_are_sentences_and_offer_a_way_forward():
    """An error in a financial assistant has to say what happened in words the
    user can act on, and put the retry next to it."""
    messages = re.findall(r"fail\('([^']+)'\)|fail\(\"([^\"]+)\"\)", CHAT_JS)
    messages = [a or b for a, b in messages]
    assert messages, 'no failure messages found; did fail() get renamed?'
    for message in messages:
        assert message[0].isupper() and message.rstrip().endswith(('.', '!')), \
            f'error text is not a sentence: {message!r}'
        assert not re.search(r'\b(null|undefined|Error|exception|traceback)\b', message), \
            f'error text leaks implementation detail: {message!r}'
    assert "actionBtn('retry'" in CHAT_JS, 'a failed answer offers no retry'


def test_an_error_uses_the_status_tokens_rather_than_a_red_of_its_own():
    """This is the financial-trust item: a warning has to look like every
    other warning in the product, or it reads as a different class of event."""
    assert 'ds-card--danger' in CHAT_JS, 'the error notice is not a danger card'
    assert 'role="alert"' in CHAT_JS, 'a failure is not announced'
    err = ' '.join(_rules(CHAT_CSS, '.err'))
    assert '--danger-mark' in err or not re.search(r'#[0-9a-f]{3,6}', err), \
        'the error notice paints its own red'


def test_a_very_long_answer_is_clamped_rather_than_left_to_run():
    assert 'clampIfLong' in CHAT_JS
    assert _rules(CHAT_CSS, '.a-body.clipped'), 'the fade on a clipped answer is gone'
    assert 'expand-btn' in CHAT_JS, 'nothing lets the user see the rest'


@pytest.mark.parametrize('selector', ['.u-bubble', '.a-body'])
def test_message_text_cannot_push_the_column_sideways(selector):
    """A pasted account number or a URL with no spaces is one token. Without
    overflow-wrap it becomes horizontal scroll on the whole page."""
    body = ' '.join(_rules(CHAT_CSS, selector))
    assert 'overflow-wrap: anywhere' in body, \
        f'{selector} can be widened past the column by a single long token'


def test_wide_content_scrolls_inside_its_own_box():
    """A table of transactions or a code block is the other way an answer gets
    wider than the column. Each has to scroll in place."""
    assert 'ds-table-wrap ds-scroll' in CHAT_JS, \
        'tables in an answer no longer scroll inside their own container'
    assert 'overflow-x: auto' in ' '.join(_rules(CHAT_CSS, '.cb pre')), \
        'code blocks no longer scroll'


# ── Mobile ───────────────────────────────────────────────────────────────

def test_the_composer_rides_the_on_screen_keyboard():
    """base.html publishes the keyboard height as --kb. If chat stops
    subtracting it, the composer sits underneath the keyboard on iOS and the
    user cannot see what they are typing."""
    phone = _phone_block(CHAT_CSS)
    assert 'html.kb-open #chat-root' in phone, 'chat no longer reacts to the keyboard'
    assert 'var(--kb, 0px)' in phone, \
        'the keyboard height is not subtracted from the chat height'
    assert '--kb' in (ROOT / 'templates' / 'base.html').read_text(encoding='utf-8'), \
        'base.html no longer publishes --kb, so the rule above is dead'


def test_the_sidebar_becomes_a_drawer_on_a_phone():
    """268px of permanent sidebar on a 375px screen leaves no conversation."""
    phone = _phone_block(CHAT_CSS)
    side = ' '.join(_rules(phone, '#side'))
    assert 'position: absolute' in side, 'the sidebar still takes column width on a phone'
    assert 'translateX(-102%)' in side, 'the drawer no longer starts off-screen'
    assert '#scrim { display: block' in re.sub(r'\s+', ' ', phone), \
        'the drawer opens without a scrim, so tapping away does not close it'


def test_touch_targets_reach_the_accessible_minimum():
    """WCAG 2.2 AA is 44px. The design system holds --tap-min for exactly this,
    and the phone block is where chat's controls have to reach it."""
    assert '--tap-min: 44px' in DS_CSS, 'the tap-target token moved'
    phone = _phone_block(CHAT_CSS)
    assert 'var(--tap-min)' in phone, \
        'no control in the phone block is sized against the tap minimum'
    # The two the thumb actually lands on.
    assert 'height: 40px' in ' '.join(_rules(phone, '.ibtn')), 'icon buttons shrank'
    assert 'height: 40px' in ' '.join(_rules(phone, '#send')), 'the send button shrank'


def test_the_message_column_is_full_width_on_a_phone():
    assert '--chat-col: 100%' in _phone_block(CHAT_CSS), \
        'the conversation column is still capped at reading width on a phone'


# ── Financial trust ──────────────────────────────────────────────────────

def test_money_in_an_answer_is_set_as_a_value_not_as_prose():
    """The same figure appears on the ledger and in an answer. Tabular
    numerals are what stop the two from reading as different numbers."""
    amt = ' '.join(_rules(CHAT_CSS, '.a-body .amt'))
    assert 'tabular-nums' in amt, 'amounts in an answer lost tabular numerals'
    assert re.search(r"AMOUNT[^\n]*\n?.*class=\"amt\"", CHAT_JS, re.S), \
        'nothing marks up amounts in a streamed answer'
    numeric = ' '.join(_rules(CHAT_CSS, '.a-body .ds-table td.num'))
    assert 'tabular-nums' in numeric and 'text-align: right' in numeric, \
        'numeric table columns no longer align as numbers'


def test_the_page_says_out_loud_that_dough_can_be_wrong():
    """The honest version of "no fake certainty": the disclaimer sits under
    the composer, on the page, not buried in a settings screen."""
    note = (ROOT / 'templates' / 'partials' / 'chat' / '_composer.html').read_text(encoding='utf-8')
    assert 'dock-note' in note, 'the composer disclaimer is gone'
    assert re.search(r'double-check|can get things wrong', note, re.I), \
        'the disclaimer no longer says Dough can be wrong'
    assert 'sends your account data' in note, \
        'the disclaimer no longer says where the data goes'


def test_a_chart_ships_the_numbers_behind_it():
    """A chart a user cannot check is a claim. Every figure carries a data
    table, which is also what makes it readable to a screen reader."""
    assert 'dataTable(spec)' in CHAT_JS, 'figures no longer render their data'
    assert 'aria-label="' in CHAT_JS and 'describe(spec)' in CHAT_JS, \
        'the chart canvas has no text alternative'


def test_the_expanded_chart_is_a_real_dialog():
    """It was a <div role="dialog"> with a hand-rolled Tab trap. Native
    <dialog> is what makes Esc, the focus trap and the inertness of the
    conversation behind it actually correct."""
    assert "createElement('dialog')" in CHAT_JS, 'the chart modal is not a <dialog>'
    assert 'dlg.showModal()' in CHAT_JS, 'the chart modal opens non-modally'
    assert 'ds-dialog' in CHAT_JS, 'the chart modal is not styled by the design system'
    assert 'e.key !== \'Tab\'' not in CHAT_JS, \
        'the hand-rolled focus trap is back; <dialog> already does this'


def test_reduced_motion_is_honoured():
    """Streaming carets, shimmer and a pulsing orb are exactly the motion that
    triggers vestibular symptoms."""
    assert '@media (prefers-reduced-motion: reduce)' in CHAT_CSS
    reduced = CHAT_CSS[CHAT_CSS.index('@media (prefers-reduced-motion: reduce)'):]
    assert '.orb { animation: none' in reduced, 'the pulsing orb still pulses'
    assert 'scroll-behavior: auto' in reduced, 'the thread still smooth-scrolls'
