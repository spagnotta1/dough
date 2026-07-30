"""Dough's framing — the mascot must never be clipped by his own avatar disc.

Dough is the product's face and he appears at a dozen sizes across the app, so
his crop is the kind of thing that breaks quietly: nothing errors, he just
starts rendering as a brown blob with flat vertical edges where his ears were.
That has now happened twice, both times because a viewBox was chosen by eye.

The rule these tests encode: ``.dough-avatar`` clips to a **disc**, so the
question is never "does the drawing fit the square viewBox" but "does it fit
the circle that square inscribes" — and it has to fit in every pose and at
every point of every idle animation, because those run forever.

The geometry itself lives in tools/dough_bbox.py, which is also runnable by
hand for the full report (``python tools/dough_bbox.py``).
"""

import importlib.util
import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'templates'


@pytest.fixture(scope='module')
def geom():
    spec = importlib.util.spec_from_file_location(
        'dough_bbox', ROOT / 'tools' / 'dough_bbox.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def ink(geom):
    """Every unit of ink Dough can occupy: poses x animation extremes."""
    return geom.every_point()


def test_avatar_crop_is_square(geom):
    """A non-square tight viewBox would squash him — .dough-avatar sizes both
    axes, so the SVG's aspect has to match the disc's."""
    _, _, w, h = geom.read_viewboxes()['TIGHT']
    assert w == h, f'tight viewBox is {w}x{h}; .dough-avatar would distort it'


def test_mascot_fits_inside_the_avatar_disc(geom, ink):
    """The whole dog, inside the circle, in every pose and mid-animation.

    This is the one that has regressed before: the previous crop
    ('46 120 420 420') ran the ear tips 17 units past each edge, and the disc
    turned the overhang into a flat vertical shave.
    """
    x, y, w, h = geom.read_viewboxes()['TIGHT']
    cx, cy, radius = x + w / 2, y + h / 2, min(w, h) / 2

    worst = max(math.hypot(px - cx, py - cy) for px, py in ink)
    assert worst <= radius, (
        f'Dough is clipped: ink reaches {worst:.1f} from the disc centre but '
        f'the disc radius is {radius:.1f}. Re-run tools/dough_bbox.py.')


def test_mascot_is_centred_in_the_avatar_disc(geom, ink):
    """Fitting is not enough — an off-centre dog reads as a mistake even when
    nothing is cut, because the disc's rim makes the gap obvious."""
    x, y, w, h = geom.read_viewboxes()['TIGHT']
    radius = min(w, h) / 2
    x0, y0, x1, y1 = geom.bbox(ink)
    offset = math.hypot((x + w / 2) - (x0 + x1) / 2, (y + h / 2) - (y0 + y1) / 2)
    assert offset / radius <= 0.03, (
        f'Dough sits {offset:.1f} units ({offset / radius:.1%}) off the centre '
        'of his own disc')


def test_mascot_very_nearly_fills_the_avatar_disc(geom, ink):
    """The complement of the clipping test. A crop with too much air is the
    obvious way to "fix" clipping and it makes him a speck at 28px, so the
    floor is pinned too."""
    x, y, w, h = geom.read_viewboxes()['TIGHT']
    cx, cy, radius = x + w / 2, y + h / 2, min(w, h) / 2
    worst = max(math.hypot(px - cx, py - cy) for px, py in ink)
    assert worst / radius >= 0.88, (
        f'Dough only fills {worst / radius:.1%} of his disc — the crop has gone '
        'slack and he will read as tiny at avatar sizes')


def test_avatar_svg_is_never_scaled_past_its_disc():
    """The other half of the old bug: the crop was also blown up to 112% inside
    an overflow:hidden disc, which cropped him again on top of the viewBox."""
    css = (ROOT / 'static' / 'css' / 'dough.css').read_text(encoding='utf-8')
    rule = re.search(r'\.dough-avatar\s+\.dough\s*\{([^}]*)\}', css)
    assert rule, '.dough-avatar .dough sizing rule is missing'
    for value in re.findall(r'(?:width|height)\s*:\s*([\d.]+)%', rule.group(1)):
        assert float(value) <= 100, (
            f'.dough-avatar .dough is scaled to {value}% inside a clipped disc; '
            'anything over 100% comes straight off his ears')


def test_avatar_placements_use_the_shared_size_scale():
    """Every disc sizes itself through --dough-size.

    Inline width/height on a .dough-avatar silently opts that one placement out
    of the scale, which is how the sizes drifted apart (22/23/24/30/56px) in the
    first place.
    """
    offenders = []
    for path in sorted(TEMPLATES.glob('*.html')):
        for line_no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if 'dough-avatar' in line and re.search(r'style=[^>]*\b(width|height)\s*:', line):
                offenders.append(f'{path.name}:{line_no}')
    assert not offenders, (
        'these .dough-avatar placements hardcode a size instead of setting '
        '--dough-size: ' + ', '.join(offenders))


def test_every_mascot_slot_names_a_real_expression(geom):
    """data-dough is the only attribute dough.js hydrates, and an unknown mood
    silently falls back to `happy`. The error page previously used
    data-dough-expression and rendered nothing at all."""
    js = (ROOT / 'static' / 'js' / 'dough.js').read_text(encoding='utf-8')
    block = re.search(r'var EXPRESSIONS = \{(.*?)\n  \};', js, re.S)
    assert block, 'could not find the EXPRESSIONS table in dough.js'
    known = set(re.findall(r'^\s{4}(\w+):', block.group(1), re.M))
    assert 'happy' in known and 'concerned' in known, known

    for path in sorted(TEMPLATES.glob('*.html')):
        text = path.read_text(encoding='utf-8')
        assert 'data-dough-expression' not in text, (
            f'{path.name} uses data-dough-expression; dough.js hydrates '
            '[data-dough] only, so nothing would render')
        for mood in re.findall(r'data-dough="([a-z]+)"', text):
            assert mood in known, f'{path.name} asks for unknown mood {mood!r}'
