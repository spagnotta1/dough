"""Every page, in every theme, at every width — the final Phase 9 gate.

The static half of the theme system is already pinned: test_ui_invariants.py
proves no migrated template hardcodes a colour, and test_design_system.py
proves no ``.ds-*`` component does. What neither can prove is the runtime
half — that switching the theme actually restyles the rendered page, flips
``data-scheme`` for the status-colour ramps, rebuilds the charts without
throwing, and does all of that without the layout springing a leak at any
width. Those are properties of ``applyTheme()`` running in a real engine.

The sweep drives the real theme engine (``window.applyTheme``, the same
function the picker's swatches call) rather than stamping CSS variables
directly, so what is exercised is what a user's click runs — including the
``check:theme-changed`` event every chart page rebuilds its canvases on.

One page load per page-and-viewport, then all themes applied in place: the
matrix is every page × every theme × three widths, but the cost is ~40
navigations rather than ~650.

The ``page_health`` fixture (autouse, see conftest) turns every test here
into a console-error / uncaught-exception / 5xx check per theme for free.
"""

import pytest

from .conftest import wait_for_layout
from .test_pages import PAGE_PATHS, VIEWPORT_PARAMS

#: Pinned so a theme quietly added or dropped shows up as a failing sweep
#: rather than as silently reduced coverage. The product's docs said
#: "17 themes" until this sweep counted; the picker has shipped 16 since
#: `terminal` was retired (base.html scrubs it from saved preferences), and
#: the docs now agree.
THEME_COUNT = 16

#: Themes whose *curated* foreground/panel pair sits below WCAG AA's 4.5:1
#: for body text. Both predate this sweep — they are aesthetic choices, not
#: regressions, and the product palette is the theme author's to choose. They
#: are pinned here at a floor just under their measured ratios so they cannot
#: quietly get worse, and every other theme is held to the full 4.5.
#:   retrowave  fg #e94560 on panel #16213e → 4.15:1
#:   cute       fg #d4608a on panel #fff8fa → 3.44:1
KNOWN_LOW_CONTRAST = {'retrowave': 4.0, 'cute': 3.2}


def _hex_to_rgb_string(hex_color):
    h = hex_color.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f'rgb({r}, {g}, {b})'


def _theme_keys(page):
    keys = page.evaluate('Object.keys(window.THEMES || {})')
    assert len(keys) == THEME_COUNT, (
        f'the picker offers {len(keys)} themes, not {THEME_COUNT}: {keys} — '
        f'if a theme was added or retired on purpose, update THEME_COUNT and '
        f'every doc that names the theme count'
    )
    return keys


# ── The matrix ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name, width, height', VIEWPORT_PARAMS)
@pytest.mark.parametrize('path', PAGE_PATHS)
def test_page_survives_every_theme(signed_in, path, name, width, height):
    """Apply all themes to the rendered page; each must take and none may leak.

    Three assertions per theme:

      * the theme *took* — the body's computed background is the theme's own
        ``bg``, which is the difference between "the variables were set" and
        "a hardcoded colour somewhere is ignoring them";
      * ``data-scheme`` resolved to light or dark — the status-colour ramps
        switch on it, so a missing stamp silently paints dark-theme greens
        on white panels;
      * the document still does not scroll sideways — charts rebuild on the
        theme-change event, and a rebuilt canvas that resized its container
        is a layout leak the colour checks would never see.
    """
    page = signed_in
    page.set_viewport_size({'width': width, 'height': height})
    page.goto(path, wait_until='load')
    wait_for_layout(page)

    # base.html animates background-color over .28s on a theme switch, so a
    # computed style read in the same tick as applyTheme() reports a colour
    # mid-transition — every theme "failed" on the sweep's first run for
    # exactly that reason. The sweep measures end states, not motion, so
    # transitions are switched off the same way the app's own
    # prefers-reduced-motion path does. Injected after the app's stylesheets,
    # so at equal specificity this !important wins on source order.
    page.add_style_tag(content='body, nav, main, header, footer, input, '
                               'textarea, select, button, table, th, td '
                               '{ transition: none !important; }')

    failures = []
    for key in _theme_keys(page):
        state = page.evaluate("""(key) => {
          applyTheme(key);
          const t = THEMES[key];
          return {
            expectedBg: t.bg,
            gotBg: getComputedStyle(document.body).backgroundColor,
            scheme: document.documentElement.getAttribute('data-scheme'),
            clientWidth: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
          };
        }""", key)

        if state['gotBg'] != _hex_to_rgb_string(state['expectedBg']):
            failures.append(f"{key}: body bg is {state['gotBg']}, "
                            f"theme says {state['expectedBg']}")
        if state['scheme'] not in ('light', 'dark'):
            failures.append(f"{key}: data-scheme is {state['scheme']!r}")
        if state['scrollWidth'] > state['clientWidth'] + 1:
            failures.append(f"{key}: page scrolls sideways — "
                            f"{state['scrollWidth']}px in {state['clientWidth']}px")

    assert not failures, (
        f'{path} @ {name} ({width}px) broke under these themes:\n  '
        + '\n  '.join(failures))


# ── Properties of the theme set itself (once, not per page) ──────────────────

def test_every_theme_keeps_text_readable(signed_in):
    """fg-on-bg and fg-on-panel contrast, measured by the app's own engine.

    ``CheckScheme.contrast`` is the routine the product already trusts to
    derive ``--accent-ink``; using it here means the sweep and the runtime
    agree about what "readable" is. Every theme must clear WCAG AA (4.5:1)
    except the two recorded above, which are pinned where they are.
    """
    page = signed_in
    page.goto('/', wait_until='load')

    ratios = page.evaluate("""() => Object.fromEntries(
      Object.entries(THEMES).map(([key, t]) => [key, {
        fgBg:    CheckScheme.contrast(CheckScheme.rgb(t.fg), CheckScheme.rgb(t.bg)),
        fgPanel: CheckScheme.contrast(CheckScheme.rgb(t.fg), CheckScheme.rgb(t.panel)),
      }]))""")

    assert len(ratios) == THEME_COUNT
    failures = []
    for key, r in ratios.items():
        floor = KNOWN_LOW_CONTRAST.get(key, 4.5)
        worst = min(r['fgBg'], r['fgPanel'])
        if worst < floor:
            failures.append(f'{key}: worst text contrast {worst:.2f}:1 '
                            f'(floor {floor}:1)')
    assert not failures, 'themes with unreadable text:\n  ' + '\n  '.join(failures)


def test_the_derived_accent_ink_is_readable_on_every_panel(signed_in):
    """The engine's promise, held on all themes at once.

    ``--accent-ink`` exists because several themes' raw accents (Paper's
    gold, Cute's pink) sit at ~2:1 on their own panels; ``readable()`` walks
    them toward black or white until they clear 4.5. If that derivation
    regresses, accent-coloured text — every "Ask Dough", every active nav
    item — goes illegible on exactly the themes where nobody is looking.
    """
    page = signed_in
    page.goto('/', wait_until='load')

    derived = page.evaluate("""() => Object.fromEntries(
      Object.entries(THEMES).map(([key, t]) => {
        const ink = CheckScheme.readable(t.red, t.panel, 4.5);
        return [key, CheckScheme.contrast(CheckScheme.rgb(ink),
                                          CheckScheme.rgb(t.panel))];
      }))""")

    bad = {k: round(v, 2) for k, v in derived.items() if v < 4.5}
    assert not bad, f'readable() failed to derive a 4.5:1 accent ink: {bad}'


def test_design_tokens_resolve_under_every_theme(signed_in):
    """No token in the semantic layer may resolve to nothing.

    A missing custom property does not error — the declaration reading it is
    simply dropped, and the element renders with whatever it inherited. That
    is invisible to every other check here, so the core of the token
    vocabulary is asserted to produce a value under each theme in turn.
    """
    page = signed_in
    page.goto('/', wait_until='load')

    tokens = ['--surface', '--surface-sunken', '--ink-body', '--ink-faint',
              '--ok-ink', '--warn-ink', '--danger-ink', '--info-ink',
              '--ai-ink', '--accent', '--accent-on', '--hairline',
              '--focus-ring']

    failures = []
    for key in _theme_keys(page):
        missing = page.evaluate("""([key, tokens]) => {
          applyTheme(key);
          const cs = getComputedStyle(document.documentElement);
          return tokens.filter(t => cs.getPropertyValue(t).trim() === '');
        }""", [key, tokens])
        if missing:
            failures.append(f'{key}: {missing}')
    assert not failures, 'tokens resolving to nothing:\n  ' + '\n  '.join(failures)


def test_the_theme_choice_survives_a_reload(signed_in):
    """Persistence is half the feature — a theme that reverts on the next
    page load reads as the product forgetting a preference.

    Asserted on /upload rather than /: the theme engine lives in base.html so
    any page proves persistence, and the dashboard starts async copilot
    fetches on load — a reload can abort one mid-flight, and under full-suite
    timing that abort occasionally reaches the console as a first-party error
    the health guard rightly refuses to ignore. A page that fetches nothing
    after load leaves nothing to abort.
    """
    page = signed_in
    page.goto('/upload', wait_until='load')
    page.evaluate("applyTheme('midnight')")

    page.reload(wait_until='load')
    got = page.evaluate('getComputedStyle(document.body).backgroundColor')
    assert got == _hex_to_rgb_string('#0d1117'), (
        f'midnight did not survive the reload: body is {got}')
    assert page.evaluate("document.documentElement.getAttribute('data-scheme')") == 'dark'
