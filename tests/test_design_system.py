"""The design system's two load-bearing rules, and the toast's safety.

Two of these encode invariants that have no other enforcement:

1. **Components never hardcode a colour.** The app ships 16 user-selectable
   themes whose panels run from #ffffff to #000000, and the only reason that
   works is that every ``.ds-*`` rule reads from the token layer instead of
   naming a colour. A single literal hex in a component is invisible on the
   theme it was authored against and unreadable on several of the others —
   the kind of regression nobody notices until a user on a black theme
   reports a black-on-black badge.

2. **Toasts render text, not markup.** ``showToast`` is called at 60 sites,
   many of them interpolating a string the browser did not author:
   ``'Error: ' + data.error``, ``err.display_message`` from Plaid,
   ``e.message``. ``rules.py`` returns ``f'Unexpected error: {e}'``, so
   exception text — which can carry user input — reaches the toast. Building
   that node with innerHTML, as it was built before, made every one of those
   an XSS sink. This test is the guard on the fix.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / 'static' / 'css' / 'design-system.css'
BASE = ROOT / 'templates' / 'base.html'


def _strip_comments(text):
    return re.sub(r'/\*.*?\*/', '', text, flags=re.S)


@pytest.fixture(scope='module')
def css():
    return _strip_comments(CSS.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def base_html():
    return BASE.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def toast_js(base_html):
    """The body of showToast(), where the message becomes DOM.

    Comments are stripped: the code there explains *why* it avoids innerHTML,
    and a scan for that name would otherwise match the explanation.
    """
    start = base_html.index('function showToast(')
    end = base_html.index('function dismissToast(')
    body = _strip_comments(base_html[start:end])
    return re.sub(r'(?m)//.*$', '', body)


# ── 1. Token discipline ──────────────────────────────────────────────────

def _component_rules(css):
    """(selector, body) for every rule whose selector names a .ds-* class."""
    for match in re.finditer(r'(?:^|\})\s*([^{}@]+?)\{([^{}]*)\}', css, re.M):
        selector, body = match.group(1).strip(), match.group(2)
        if '.ds-' in selector:
            yield ' '.join(selector.split()), body


def test_components_never_hardcode_a_colour(css):
    """Every colour in a component resolves through the token layer.

    Literals belong in the ``:root`` / ``html[data-scheme]`` blocks at the top
    of the file — those are the theme ramps, and they are the only place a
    colour is allowed to be spelled out.
    """
    offenders = [
        (selector, literal)
        for selector, body in _component_rules(css)
        for literal in re.findall(r'#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)', body)
    ]
    assert not offenders, (
        'hardcoded colours in design-system components — these will not '
        'survive a theme change:\n'
        + '\n'.join(f'  {sel} -> {lit}' for sel, lit in offenders)
    )


def test_new_primitives_are_defined(css):
    """The shells the templates are being migrated onto.

    Named explicitly so that deleting one shows up as a failing invariant
    rather than as a page that silently loses its styling.
    """
    for cls in ('.ds-toast', '.ds-empty', '.ds-table', '.ds-field',
                '.ds-input', '.ds-dialog'):
        assert re.search(re.escape(cls) + r'[\s,{:]', css), f'{cls} is missing'


def test_dialogs_and_tables_survive_a_phone(css):
    """Two mobile rules that are easy to drop and expensive to lose.

    A wide table must scroll inside its own container, or it stretches the
    page and the whole body scrolls sideways; a dialog must be reachable
    with a thumb.
    """
    assert 'overflow-x: auto' in css.split('.ds-table-wrap')[1][:200], \
        '.ds-table-wrap must scroll its own overflow'
    assert re.search(r'@media \(max-width: 640px\)[^@]*\.ds-dialog\b', css, re.S), \
        '.ds-dialog has no phone treatment'


# ── 2. Toast safety ──────────────────────────────────────────────────────

def test_toast_never_builds_its_message_as_markup(toast_js):
    """The XSS guard.

    ``showToast`` receives server-authored and Plaid-authored strings. If this
    fails, check whether the message is reaching the DOM through innerHTML,
    insertAdjacentHTML or an equivalent — it must go through textContent or a
    text node.
    """
    for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
        assert sink not in toast_js, (
            f'showToast() writes through {sink}; the message can carry a '
            'server error string and would be parsed as markup'
        )


def test_toast_message_goes_in_as_text(toast_js):
    assert 'createTextNode' in toast_js or '.textContent' in toast_js, \
        'showToast() no longer puts the message in as text'


def test_toast_container_is_a_live_region(base_html):
    """Without this, a screen reader user gets no confirmation that a sync
    started, a category saved, or a delete failed."""
    container = re.search(r'<div id="toast-container".*?>', base_html, re.S)
    assert container, '#toast-container is gone'
    markup = container.group(0)
    assert 'aria-live' in markup, '#toast-container is not a live region'
    assert 'aria-label' in markup, 'the live region has no accessible name'


def test_errors_interrupt_the_screen_reader(toast_js):
    """A failed sync should not wait politely behind whatever is being read."""
    tone_table = re.search(r'var TOAST_TONE = \{.*?\};',
                           BASE.read_text(encoding='utf-8'), re.S)
    assert tone_table, 'TOAST_TONE table is gone'
    table = tone_table.group(0)
    assert re.search(r"error:.*?'assertive'", table), \
        'error toasts must announce assertively'
    assert re.search(r"success:.*?'polite'", table), \
        'success toasts should not interrupt'


def test_toast_dismiss_button_is_labelled(toast_js):
    """It renders as "×", which a screen reader reads as "multiplication
    sign" — or as nothing at all."""
    assert 'aria-label' in toast_js, 'the dismiss button has no accessible name'
