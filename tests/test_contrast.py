"""dough/contrast.py — the server's half of the ink derivation.

There are now two implementations of this arithmetic: this module and
``CheckScheme`` in base.html. They are allowed to coexist (one runs where the
theme lives, the other where the *data* lives) on one condition — that they
never disagree. The last test here pins the handful of inputs the product
actually ships through it; ``tests/browser/test_connections.py`` measures the
rendered result in a real engine.
"""

import pytest

from dough.contrast import contrast, on_color, parse_hex


@pytest.mark.parametrize('text, expected', [
    ('#000000', (0, 0, 0)),
    ('#ffffff', (255, 255, 255)),
    ('#0052ff', (0, 82, 255)),
    ('0052ff', (0, 82, 255)),        # the hash is optional
    ('  #0052FF  ', (0, 82, 255)),   # and so is tidiness
    ('#abc', (170, 187, 204)),       # three-digit shorthand expands
])
def test_parse_hex_reads_the_forms_that_occur(text, expected):
    assert parse_hex(text) == expected


@pytest.mark.parametrize('junk', ['', None, 'rebeccapurple', '#12', '#zzzzzz'])
def test_unparseable_colour_falls_back_to_black_rather_than_raising(junk):
    """An adapter with a malformed accent colour must not take the page down.

    Black is the safe fallback precisely because it is the worst case for the
    default ink: it forces ``on_color`` to return white, which is readable.
    """
    assert parse_hex(junk) == (0, 0, 0)
    assert on_color(junk) == '#ffffff'


def test_contrast_matches_the_wcag_reference_points():
    """The two anchors of the WCAG scale, which any correct implementation hits
    exactly: identical colours are 1:1, black on white is 21:1."""
    assert contrast((0, 0, 0), (0, 0, 0)) == pytest.approx(1.0)
    assert contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast((0, 0, 0), (255, 255, 255)) == contrast((255, 255, 255), (0, 0, 0))


@pytest.mark.parametrize('background, ink', [
    ('#000000', '#ffffff'),   # Plaid — the colour that started this
    ('#ffffff', '#111113'),
    ('#0052ff', '#ffffff'),   # Coinbase blue
    ('#c47d5a', '#111113'),   # Dough's own terracotta accent
])
def test_the_chosen_ink_is_the_readable_one(background, ink):
    assert on_color(background) == ink


def test_every_shipped_accent_can_carry_readable_text():
    """The check that has to hold on real data, run against real data.

    White and near-black are the extremes — if neither clears 4.5:1 on a
    background then *no* ink does, and the only fix is the background itself.
    So this is a constraint on the palette, not on the picker, and it belongs
    on the actual adapter classes rather than on a hand-copied list of hexes.
    It has already earned its keep: the inherited default was #6366f1, whose
    best possible ink is 4.47:1.
    """
    from finance_sync.adapters import available_institutions
    from finance_sync.adapters.base import FinancialInstitutionAdapter

    palette = {cls.__name__: cls.accent_color for cls in available_institutions()}
    palette['(the inherited default)'] = FinancialInstitutionAdapter.accent_color

    bad = {}
    for name, colour in palette.items():
        ratio = contrast(parse_hex(on_color(colour)), parse_hex(colour))
        if ratio < 4.5:
            bad[name] = f'{colour} → best ink {on_color(colour)} at {ratio:.2f}:1'
    assert not bad, ('accent colours no ink can be read on:\n  '
                     + '\n  '.join(f'{k}: {v}' for k, v in bad.items()))
