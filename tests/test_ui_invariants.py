"""UI invariants for templates that have been migrated to the design system.

``MIGRATED`` is the migration ledger. A template joins it when its Phase 9
wave lands, and from that moment these rules hold for it forever:

  * no inline ``<style>`` block — page CSS lives in its own stylesheet
  * no literal colours — every colour resolves through a design token, which
    is the only reason 16 themes work
  * no Tailwind utility classes — this is what makes removing the CDN a
    one-line change at the end of Phase 9 instead of a second migration
  * tables are ``.ds-table``, form controls are ``.ds-field``/``.ds-input``,
    modals are native ``<dialog class="ds-dialog">``
  * user feedback goes through ``showToast()``, never ``alert()``

The second half of the file is different in kind: those are contract tests for
the transactions ledger specifically, and they exist because the page's
JavaScript addresses table cells **by index**. Nothing about that is visible
in either file alone, so a column reordered in the template would break the
edit flow silently and only for rows that had been edited.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'templates'
CSS = ROOT / 'static' / 'css'

#: Templates migrated to the design system, in wave order.
MIGRATED = [
    'transactions.html',
    'chat.html',
    'rules.html',
    'connections.html',
    'recurring.html',
    'upload.html',
    'anomalies.html',
    'sync_history.html',
    # These four were built on the design system by work parallel to the
    # Phase 9 waves (the dashboard/investments rebuilds) but never added
    # here, so the rules held for every page except the four largest. Audited
    # and brought fully up to the ledger's rules in the same pass that added
    # them: ds-table/ds-input/ds-select classes joined the page-local ones,
    # and budgets.html's inline <style> moved to static/css/budgets.css.
    'dashboard.html',
    'investments.html',
    'household.html',
    'budgets.html',
    # Phase 10.5. Both extend base.html and read the runtime theme engine, so
    # every rule above applies to them from the day they landed rather than
    # from a later audit — which is the whole reason this ledger exists.
    #
    # landing.html is the interesting one: it is reached by people who are *not*
    # signed in, which is the property that put login/join/setup on the exempt
    # list below. It is on the ledger anyway, and deliberately, because the
    # exemption is not about who visits a page — it is about whether the page
    # has a theme engine to read colours from. This one does. A returning
    # visitor who chose Midnight must not be shown a copper marketing page that
    # switches the instant they sign in. See the header of static/css/landing.css.
    'landing.html',
    'settings.html',
]

#: login.html, join.html, setup.html and error.html (Waves 3 and 4) are
#: deliberately not on this ledger — joined in Phase 10.5 by register.html,
#: forgot_password.html, reset_password.html and verified.html, which share the
#: same shell for the same reason. Every rule above assumes a page that
#: extends base.html and reads its colours from the runtime theme engine —
#: these are reached by people who may not be signed in or may have just
#: hit a failure, do not extend base.html, and intentionally do not load that
#: engine (see the header comment in static/css/auth.css, which they all
#: share). They lock their palette to fixed values instead, which "no literal
#: colours" cannot distinguish from a page that simply forgot to use a token.
#: They were audited by hand for their waves instead: one shared auth.css
#: rather than four copies of the same inline <style> block, real
#: (visually-hidden) <label> elements, a fixed grid-track layout bug (see
#: join.html), and a fixed --red mismatch that left Dough's mascot prop
#: violet on a copper page (see the header comment in auth.css).
#:
#: log.html (also nominally Wave 4) was not migrated at all: it turned out to
#: be dead code — no route has rendered it since /api/log/* stopped serving a
#: page of its own — so it and its React+Babel-CDN static/js/log.js were
#: deleted rather than polished.


def _pages(name):
    """A page and every partial it is assembled from.

    Wave 1b split chat.html into a 52-line shell plus four partials. Checking
    only the file named in ``MIGRATED`` would have passed on the shell — which
    contains no colours, no tables and no form controls — while every rule the
    ledger exists to enforce went unexamined in the four files that hold the
    actual markup. The ledger has to follow the code.
    """
    paths = [TEMPLATES / name]
    stem = Path(name).stem
    partials = TEMPLATES / 'partials' / stem
    if partials.is_dir():
        paths.extend(sorted(partials.glob('*.html')))
    return paths


def _source(name):
    return '\n'.join(p.read_text(encoding='utf-8') for p in _pages(name))


def _strip_jinja_comments(text):
    return re.sub(r'\{#.*?#\}', '', text, flags=re.S)


# ── Cross-cutting UI invariants ──────────────────────────────────────────

@pytest.mark.parametrize('template', MIGRATED)
def test_no_inline_style_block(template):
    """Page CSS belongs in a stylesheet, where the token layer can reach it.

    Note the constraint that makes this non-obvious: SPA navigation swaps
    ``<main>`` only, so a migrated page links its stylesheet from *inside*
    the content block rather than from ``<head>``.
    """
    # Comment-stripped, like every other check here. Without it the rule fires
    # on a template whose *comment* explains why it has no inline styles, which
    # is the opposite of the intent and exactly what happened when rules.html
    # was migrated.
    assert '<style' not in _strip_jinja_comments(_source(template)), (
        f'{template} still has an inline <style> block; move it to '
        f'static/css/ and link it from inside the content block'
    )


@pytest.mark.parametrize('template', MIGRATED)
def test_links_its_stylesheet_from_the_content_block(template):
    """A <link> in <head> would never arrive on a client-side navigation."""
    src = _source(template)
    if 'stylesheet' not in src:
        pytest.skip(f'{template} needs no page-specific CSS')
    head_pos = src.find('{% block content %}')
    assert src.find('stylesheet') > head_pos, (
        f'{template} links its stylesheet outside the content block, so SPA '
        f'navigation to this page would arrive unstyled'
    )


@pytest.mark.parametrize('template', MIGRATED)
def test_no_literal_colours(template):
    """One hardcoded colour is invisible on the theme it was authored against
    and unreadable on several of the other sixteen."""
    src = _strip_jinja_comments(_source(template))
    literals = re.findall(r'#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\([^)]*\)', src)
    # Character entities (&#x270E;) are not colours.
    literals = [lit for lit in literals if not re.fullmatch(r'#[0-9a-fA-F]{4};?', lit)]
    assert not literals, f'{template} hardcodes colours: {literals}'


#: Class patterns that only Tailwind produces. Deliberately narrow — a false
#: positive here blocks a migration, so this catches the utilities the
#: unmigrated templates actually use rather than trying to model Tailwind.
TAILWIND_PATTERNS = [
    r'\b(?:bg|text|border|ring|divide|from|to)-(?:gray|slate|zinc|indigo|red|green|blue|yellow|purple|white|black)(?:-\d{2,3})?\b',
    r'\b(?:px|py|pt|pb|pl|pr|mx|my|mt|mb|ml|mr)-\d',
    r'\bspace-[xy]-\d',
    r'(?<![-\w])(?:rounded|shadow)(?:-(?:sm|md|lg|xl|full|none))?(?=["\s])',
    r'\b(?:w|h|max-w|min-w)-(?:full|screen|xs|sm|md|lg|xl)\b',
    # A hyphen is part of a class name, so the boundaries here have to exclude
    # it explicitly: \bhidden\b matches inside "side-hidden", which is chat's
    # own sidebar state and nothing to do with Tailwind. \b alone reads a
    # hyphen as a word boundary, and a false positive here blocks a migration.
    r'class="[^"]*(?<![-\w])(?:hidden|flex|grid|block|inline-flex)(?![-\w])',
]


@pytest.mark.parametrize('template', MIGRATED)
def test_no_tailwind_utilities(template):
    """The CDN is Phase 9's exit criterion; a migrated page must not need it."""
    src = _strip_jinja_comments(_source(template))
    hits = []
    for pattern in TAILWIND_PATTERNS:
        hits.extend(re.findall(pattern, src))
    assert not hits, (
        f'{template} still uses Tailwind utilities: {sorted(set(hits))}'
    )


@pytest.mark.parametrize('template', MIGRATED)
def test_tables_forms_and_dialogs_use_the_design_system(template):
    src = _source(template)

    for match in re.finditer(r'<table\b[^>]*>', src):
        assert 'ds-table' in match.group(0), \
            f'{template} has a table that is not .ds-table: {match.group(0)[:80]}'

    # Text-entry controls carry .ds-input / .ds-select / .ds-textarea, or
    # .ds-control__input when the field is a composite — a search box with a
    # leading icon, a composer with a send button inside it. There the border
    # and the focus ring belong to the shell, so the input is deliberately
    # bare; it is still themed by the design system, just one level up.
    #
    # Checkboxes, radios and hidden fields are intentionally exempt: they are
    # styled by the row or the form around them, not as fields.
    for match in re.finditer(r'<(input|select|textarea)\b[^>]*>', src):
        tag = match.group(0)
        if re.search(r'type="(hidden|checkbox|radio|submit)"', tag):
            continue
        assert re.search(r'ds-(input|select|textarea|control__input)', tag), \
            f'{template} has an unmigrated form control: {tag[:90]}'

    if '<dialog' in src:
        assert 'ds-dialog' in src, f'{template} has a <dialog> without .ds-dialog'


@pytest.mark.parametrize('template', MIGRATED)
def test_feedback_goes_through_the_toast_system(template):
    """alert() cannot be themed, cannot be dismissed by a screen reader
    gracefully, and blocks the page."""
    src = _strip_jinja_comments(_source(template))
    assert not re.search(r'(?<![.\w])alert\s*\(', src), \
        f'{template} calls alert(); use showToast(message, type)'


# ── The stylesheet behind a migrated page ────────────────────────────────

def _stylesheets(template):
    """The page-specific stylesheets a template links, as paths."""
    names = re.findall(r"filename='css/([a-z0-9_-]+\.css)'", _source(template))
    return [CSS / n for n in names if (CSS / n).exists()]


def _declared_tokens(path):
    """Custom properties a stylesheet *defines* (not ones it reads)."""
    return set(re.findall(r'^\s*(--[a-z0-9-]+)\s*:', path.read_text(encoding='utf-8'), re.M))


#: Every token design-system.css defines. A page stylesheet may add to this
#: vocabulary; it may not quietly redefine a word already in it.
DS_TOKENS = _declared_tokens(CSS / 'design-system.css')

#: Set per-theme by base.html, not by the design system, but shadowed just as
#: destructively.
THEME_TOKENS = {'--bg', '--fg', '--panel', '--border', '--red', '--kb'}


@pytest.mark.parametrize('template', MIGRATED)
def test_page_css_does_not_shadow_a_design_system_token(template):
    """The failure this catches is silent, and it bit this project once.

    chat.css defined --ink, --surface, --accent and --accent-ink on #chat-root.
    Three are design-system token names and two meant something *different*
    there: --surface was the page background rather than a panel, and
    --accent-ink was the text laid *on* the accent, which the design system
    calls --accent-on. Nothing looked wrong, because nothing inside #chat-root
    used a .ds-* component. The moment one did, it inherited the redefinitions
    and rendered with the wrong background and the wrong contrast — on the
    themes where --bg and --panel differ most, which is most of them.

    A page may define its own tokens. It must namespace them.
    """
    for sheet in _stylesheets(template):
        shadowed = sorted(_declared_tokens(sheet) & (DS_TOKENS | THEME_TOKENS))
        assert not shadowed, (
            f'{sheet.name} redefines design-system tokens {shadowed}; every '
            f'.ds-* component rendered inside this page will silently pick '
            f'them up. Namespace them (--{Path(template).stem}-*) instead.'
        )


@pytest.mark.parametrize('template', MIGRATED)
def test_page_css_has_no_literal_colours(template):
    """The template-level check is not enough once a page has its own
    stylesheet — that is where the colours actually are."""
    for sheet in _stylesheets(template):
        body = re.sub(r'/\*.*?\*/', '', sheet.read_text(encoding='utf-8'), flags=re.S)
        literals = re.findall(
            r'#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\((?!var\()[^)]*\)', body)
        assert not literals, (
            f'{sheet.name} hardcodes colours: {sorted(set(literals))} — these '
            f'are the pixels that break on 15 of the 16 themes'
        )


# ── Correctness rules that apply to every template, migrated or not ─────────
#
# Everything above is about the design-system migration and is scoped to
# MIGRATED. What follows is not a style rule and does not get to wait for a
# wave: it is a way of writing markup that produces a page the browser parses
# wrongly.

#: `="` opened, then a `|tojson` before any `"` closes it. `[^"]*` is what makes
#: the match mean something: if the attribute had already been closed, the
#: tojson would not be inside it.
TOJSON_IN_DOUBLE_QUOTED_ATTR = re.compile(r'=\s*"[^"]*\|\s*tojson')


def _all_templates():
    return sorted(TEMPLATES.rglob('*.html'))


@pytest.mark.parametrize('path', _all_templates(), ids=lambda p: p.name)
def test_no_tojson_inside_a_double_quoted_attribute(path):
    """`|tojson` renders a JSON literal, and JSON strings are double-quoted.

    Put one inside a double-quoted HTML attribute and its opening quote closes
    the attribute. The transactions ledger shipped like this: the browser read

        onclick="openEditModal(8, '2026-07-21', -500.0, "

    and threw `SyntaxError: Unexpected end of input` on every click, so Edit did
    nothing on every row. 633 tests passed the whole time, because an `onclick`
    attribute is not compiled until it fires — the response body contains the
    right characters, the page loads clean, and only a real click finds it.

    Use a single-quoted attribute. Flask's `tojson` escapes `'` (along with
    `<`, `>` and `&`) to \\u form, so nothing it emits can reach that delimiter.

    Known limit: a `"` inside the Jinja expression itself — `{{ f("x")|tojson }}`
    — ends the `[^"]*` run and hides the match. That is a narrow enough gap to
    accept for a rule this cheap, and the single-quote fix makes it moot.
    """
    hits = TOJSON_IN_DOUBLE_QUOTED_ATTR.findall(path.read_text(encoding='utf-8'))
    assert not hits, (
        f'{path.name} uses |tojson inside a double-quoted attribute, which the '
        f'browser truncates at the JSON string\'s opening quote: {hits}\n'
        f'Use a single-quoted attribute instead.'
    )


def test_the_touch_breakpoint_agrees_across_files():
    """One width, written in two places, which must not drift apart.

    base.html's touch block is what *displays* the bottom tab bar. chat.css's
    PHONE / TOUCH block is what *reserves room* for it, via --chat-tabbar-h.
    A tab bar shown at a width where no room is reserved puts the composer
    underneath it; room reserved at a width where no bar is shown leaves 54px
    of dead space at the bottom of the conversation.

    Both were wrong in the second direction for a long time — 640px against
    860px — and nothing noticed, because the symptom is a gap rather than a
    crash. The complement of Tailwind's `lg:` is the number, because `lg:` is
    what reveals the desktop nav row.
    """
    base = (TEMPLATES / 'base.html').read_text(encoding='utf-8')
    chat = (CSS / 'chat.css').read_text(encoding='utf-8')

    marker = chat.index('PHONE / TOUCH')
    chat_bp = re.search(r'@media \(max-width: ([\d.]+)px\)', chat[marker:]).group(1)

    # base.html's is the block that contains the tab bar's display rule.
    base_bps = re.findall(r'@media \(max-width: ([\d.]+)px\) \{(.*?)\n  \}', base, re.S)
    owning = [bp for bp, body in base_bps if '#tab-bar' in body and 'display: flex' in body]
    assert len(owning) == 1, f'expected exactly one media query to show the tab bar, found {owning}'

    assert owning[0] == chat_bp == '1023.98', (
        f'the touch breakpoint has drifted: base.html shows the tab bar at '
        f'max-width {owning[0]}px, chat.css reserves room for it at {chat_bp}px. '
        f'Both must be 1023.98px — the complement of Tailwind\'s lg: (1024px), '
        f'which is what reveals the desktop nav row.'
    )


# ── Tailwind is gone, and stays gone ─────────────────────────────────────
#
# The UNMIGRATED_CONTROL that used to live here — a real unmigrated template
# the detectors had to keep firing on — retired with the CDN, exactly as its
# comment said it would: base.html was the last template to trip the
# detectors, and it stopped tripping them the day its nav and menus moved to
# the design system and the <script src="https://cdn.tailwindcss.com"> tag
# came out. What replaces it is one synthetic self-test (the guard on the
# guards, no longer needing a live specimen) and the permanent sweep below.

def test_the_tailwind_detectors_still_fire():
    """Guard on the guards.

    The sweep below is a negative assertion over every template, so a broken
    regex would pass silently forever. There is no longer a real unmigrated
    template to use as a control, so the specimen is synthetic — one line of
    the markup Wave 1 actually started from.
    """
    specimen = ('<style>.x{}</style><div class="bg-white shadow rounded-lg '
                'px-4 py-2 flex text-gray-500 space-y-2">')
    hits = []
    for pattern in TAILWIND_PATTERNS:
        hits.extend(re.findall(pattern, specimen))
    assert len(hits) >= 5, f'the Tailwind detectors have gone blind: {hits}'
    assert '<style' in specimen


def _strip_all_comments(text):
    """Jinja, HTML and CSS/JS block comments.

    The MIGRATED checks strip only Jinja comments, because a migrated page
    should not even *talk* about Tailwind in its markup comments. This sweep
    covers base.html too, whose stylesheet legitimately documents the old
    breakpoint bug in prose ("a drop shadow", "Tailwind's lg:") — prose
    cannot summon the CDN, so comments of every kind are out of scope here.
    """
    text = _strip_jinja_comments(text)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return text


@pytest.mark.parametrize('path', sorted(TEMPLATES.rglob('*.html')),
                         ids=lambda p: p.name)
def test_no_template_references_tailwind(path):
    """The regression guard behind the CDN removal — every template, forever.

    Two things are asserted. No template loads Tailwind (the CDN URL, or a
    future @tailwind directive smuggled in via a build step), and no template
    uses its utility classes. The second matters even though the first alone
    would keep the product working: with the CDN gone a stray
    class="flex px-4" does nothing *silently*, and markup that looks styled
    but is not is exactly the kind of regression that survives review.
    """
    src = _strip_all_comments(path.read_text(encoding='utf-8'))
    assert 'tailwindcss' not in src.lower(), \
        f'{path.name} references the Tailwind CDN'
    assert '@tailwind' not in src, f'{path.name} carries a @tailwind directive'
    hits = []
    for pattern in TAILWIND_PATTERNS:
        hits.extend(re.findall(pattern, src))
    assert not hits, (
        f'{path.name} uses Tailwind utility classes, which style nothing now '
        f'that the CDN is gone: {sorted(set(hits))}'
    )


# ── Transactions ledger: the contracts its JavaScript depends on ─────────

#: Void elements emit no end tag, so they must not move the nesting depth —
#: the row's first cell is an <input>, and counting it would swallow the rest
#: of the table.
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'param', 'source', 'track', 'wbr'}


class _RowParser(HTMLParser):
    """Collects the <td> cells of the first transaction row, in source order."""

    def __init__(self):
        super().__init__()
        self.in_row = False
        self.done = False       # one row is the sample; the rest are identical
        self.depth = 0
        self.cells = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        attrs = dict(attrs)
        if tag == 'tr' and (attrs.get('id') or '').startswith('row-'):
            self.in_row = True
        elif self.in_row and tag == 'td' and self.depth == 0:
            self._current = {'class': attrs.get('class', ''), 'html': ''}
            self.depth = 1
        elif self._current is not None:
            if tag not in _VOID:
                self.depth += 1
            self._current['html'] += f'<{tag}'
            for key, value in attrs.items():
                self._current['html'] += f' {key}="{value}"'
            self._current['html'] += '>'

    def handle_endtag(self, tag):
        if self._current is not None:
            self.depth -= 1
            if self.depth == 0 and tag == 'td':
                self.cells.append(self._current)
                self._current = None
            else:
                self._current['html'] += f'</{tag}>'
        elif tag == 'tr' and self.in_row and self.cells:
            self.in_row = False
            self.done = True

    def handle_data(self, data):
        if self._current is not None:
            self._current['html'] += data


@pytest.fixture()
def ledger(client):
    """The transactions page rendered with real rows — one charge, one
    deposit, one with a note."""
    from datetime import date

    from models import Transaction, db

    db.session.add(Transaction(account_name='Checking', date=date(2026, 3, 5),
                               description='AJI SUSHI', amount=-77.31,
                               category='Food', notes='dinner with M'))
    db.session.add(Transaction(account_name='Checking', date=date(2026, 3, 6),
                               description='PAYCHECK', amount=2410.00,
                               category='Income'))
    db.session.commit()
    return client.get('/transactions').get_data(as_text=True)


@pytest.fixture()
def first_row(ledger):
    parser = _RowParser()
    parser.feed(ledger)
    assert parser.cells, 'no transaction rows rendered'
    return parser.cells


def test_cell_order_matches_what_saveedit_addresses(first_row):
    """saveEdit() writes to row.cells[2], [3], [4] and [6] by index.

    If a column is inserted or moved in the template, editing a transaction
    starts writing the date into the description — and only on rows the user
    has edited, so it survives a casual look at the page.
    """
    assert len(first_row) == 8, \
        f'the row has {len(first_row)} cells; the script assumes 8'

    # The page defaults to date-descending, so the 03-06 deposit leads.
    assert '2026-03-06' in first_row[2]['html'], 'cells[2] is not the date column'
    assert 'PAYCHECK' in first_row[3]['html'], 'cells[3] is not the description column'
    assert 'Checking' in first_row[4]['html'], 'cells[4] is not the account column'
    assert '$2,410.00' in first_row[6]['html'], 'cells[6] is not the amount column'

    assert 'row-check' in first_row[0]['html'], 'cells[0] is not the select column'
    assert 'category-select' in first_row[5]['html'], 'cells[5] is not the category column'


def test_amount_shows_its_sign_not_just_a_colour(ledger):
    """Direction of money is the fact users are here for; it must not live
    only in a colour channel."""
    assert '-$77.31' in ledger, 'a charge does not render with a minus sign'
    assert '$2,410.00' in ledger, 'a deposit does not render'


def test_server_and_client_format_amounts_identically(ledger):
    """These disagreed before the migration: the template rendered "$-77.31"
    and an edit re-rendered the same row as "$77.31", so editing a charge
    made it read as a deposit until the next reload."""
    script = _source('transactions.html')
    assert "(value < 0 ? '-$' : '$')" in script, \
        'formatAmount() no longer matches the server-side sign placement'
    assert '-$77.31' in ledger


def test_the_hooks_the_page_script_needs_are_present(ledger):
    for hook in ('id="txnTable"', 'id="selectAll"', 'id="bulkBar"',
                 'id="selectedCount"', 'id="bulkCategory"', 'id="txnPresets"',
                 'id="txnFilterForm"', 'id="filtersToggle"', 'id="filtersChevron"',
                 'id="exportBtn"', 'id="editModal"', 'class="row-check"'):
        assert hook in ledger, f'transactions page lost {hook}'
    for field in ('edit_date', 'edit_amount', 'edit_description',
                  'edit_account', 'edit_category', 'edit_notes'):
        assert f'id="{field}"' in ledger, f'edit dialog lost {field}'


def test_bulk_bar_hides_with_the_hidden_attribute(ledger):
    """It used Tailwind's .hidden class, which would stop working the moment
    the CDN goes. [hidden] is in design-system.css instead."""
    bar = re.search(r'<div id="bulkBar"[^>]*>', ledger)
    assert bar and ' hidden' in bar.group(0), \
        'the bulk bar no longer starts hidden via the hidden attribute'


def test_the_edit_modal_is_a_native_dialog(ledger):
    """The div-with-a-backdrop it replaced had no focus trap, so Tab walked
    into the page behind it."""
    assert re.search(r'<dialog id="editModal"[^>]*class="[^"]*ds-dialog', ledger)
    assert 'showModal()' in _source('transactions.html')


def test_empty_state_is_rendered_when_nothing_matches(client):
    html = client.get('/transactions?search=nothingmatchesthisstring').get_data(as_text=True)
    assert 'ds-empty' in html, 'a filter matching nothing renders bare space'
    assert 'Nothing matches that' in html
