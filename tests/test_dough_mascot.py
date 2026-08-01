"""Dough's artwork — the mascot must be the reference image, and must fit.

Dough is the product's face. Two things about him have broken before, both
quietly, and this module exists to make both loud.

**He was redrawn.** The mascot shipped for a while as ~13 hand-authored SVG
paths "traced by measurement" from `static/img/dough_V2.jpg`. It rendered a
recognisably different dog — narrow strap ears instead of plush flared ones, a
smooth dome where the reference has fur scalloping, a small nose and tongue
against the reference's wide open smile. Nothing failed; it just was not the
brand asset. So the rule (AGENTS.md) is that the artwork is the image file, and
these tests refuse a drawn one.

**He was clipped.** `.dough-avatar` clips to a *disc*, so the framing question
is never "does the drawing fit the square" but "does it fit the circle that
square inscribes". Two hand-picked crops shaved his ear tips into a flat
vertical edge — and ear-to-ear is exactly the silhouette that makes him read as
a dog at 28px.

The crops themselves are produced by `tools/build_dough_assets.py`, which is
also runnable by hand.
"""

import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / 'static' / 'img'
TEMPLATES = ROOT / 'templates'
DOUGH_JS = ROOT / 'static' / 'js' / 'dough.js'

REFERENCE = ROOT / 'brand' / 'dough-master.jpg'
FULL = IMG / 'dough.png'
HEAD = IMG / 'dough-head.png'
MARK = IMG / 'dough-mark.png'


@pytest.fixture(scope='module')
def js():
    return DOUGH_JS.read_text(encoding='utf-8')


# ── The artwork is the image ────────────────────────────────────────────────

def test_the_reference_artwork_is_present():
    """Everything else is derived from this one file. Losing it means the
    crops cannot be regenerated and there is no source of truth left."""
    assert REFERENCE.exists(), (
        f'{REFERENCE.name} is missing — it is the mascot, and the only Dough '
        'this project has')


def test_the_master_is_not_served():
    """The archival master lives outside `static/` on purpose.

    Under static/ it was a publicly fetchable 183KB JPG with the background
    still baked in — one typo away from being the thing a page rendered, and
    an invitation to skip the pipeline. Runtime reads the PNGs; only the build
    scripts read the master.
    """
    assert not (IMG / 'dough_V2.jpg').exists(), (
        'the master is back under static/ — it belongs in brand/, which Flask '
        'does not serve')
    assert REFERENCE.parent.name == 'brand'


def test_the_shipped_crops_are_present():
    for path in (FULL, HEAD, MARK,
                 IMG / 'dough@2x.png', IMG / 'dough-head@2x.png'):
        assert path.exists(), (
            f'{path.name} is missing — run python tools/build_dough_assets.py')


def test_the_mascot_is_not_drawn_in_javascript(js):
    """No path data, no shape elements, no drawn dog.

    This is the regression that shipped: a renderer that composes Dough out of
    coordinates is a redraw however carefully those coordinates were measured.
    dough.js is allowed to *place* the artwork and nothing else.
    """
    body = re.sub(r'/\*.*?\*/', '', js, flags=re.S)      # prose may discuss it
    offenders = re.findall(r'<(?:path|ellipse|circle|polygon|rect)\b', body)
    assert not offenders, (
        f'dough.js draws {sorted(set(offenders))} — the mascot is '
        'static/img/dough_V2.jpg, not geometry. See AGENTS.md.')
    assert '<svg' not in body, 'dough.js emits an <svg>; the mascot is an image'
    assert re.search(r'<img\b', body), 'dough.js no longer emits an <img>'


def test_no_drawn_mascot_survives_in_static(js):
    """The icon pipeline used to rasterize a second, separately-drawn copy.

    Two files drawing the same dog is how the product ended up shipping two
    different ones. There is now a single artwork and every icon is a crop of
    it, so a reappearing app-icon.svg means someone started drawing again.
    """
    for stale in ('app-icon.svg', 'app-icon-mark.svg'):
        assert not (IMG / stale).exists(), (
            f'{stale} is back. Icons are composited from the reference by '
            'tools/build_icons.py; a vector mascot is a second dog.')


def test_the_crops_carry_transparency():
    """The reference has a baked cream background. Shipping it unremoved puts
    Dough in a beige rectangle on every themed panel and every dark scheme."""
    for path in (FULL, HEAD):
        alpha = np.asarray(Image.open(path).convert('RGBA').getchannel('A'))
        assert (alpha == 0).any(), f'{path.name} has no transparent pixels'
        clear = (alpha == 0).mean()
        assert clear > 0.10, (
            f'{path.name} is only {clear:.1%} transparent — the background '
            'does not look removed')


def test_the_eye_catchlights_survived_the_cutout():
    """The whites in Dough's eyes are 25 levels from the background cream.

    Any background removal done by colour distance instead of connectivity
    punches two holes in his eyes, and it is invisible until he is on a dark
    theme. `background_mask()` floods from the border for exactly this reason;
    this is the test that would catch someone simplifying it.
    """
    # Measured on the 2x crop: the 1x is only ~100 such pixels, close enough to
    # any threshold that the test would be about rounding rather than eyes.
    path = IMG / 'dough-head@2x.png'
    a = np.asarray(Image.open(path).convert('RGBA'))
    opaque_white = ((a[:, :, :3] > 235).all(axis=2)) & (a[:, :, 3] > 250)
    assert opaque_white.sum() > 200, (
        f'the eye catchlights are gone from {path.name} — background removal '
        'has eaten the whites inside his eyes')


# ── Framing ─────────────────────────────────────────────────────────────────

def _ink(path):
    """Coordinates of every visible pixel."""
    alpha = np.asarray(Image.open(path).convert('RGBA').getchannel('A'))
    ys, xs = np.nonzero(alpha > 8)
    return xs, ys, alpha.shape


def _portrait(path=HEAD):
    """The head crop split at the neck.

    The crop is a *portrait*: head and ears centred, with the shoulders
    tapering in at the bottom. The disc is meant to clip those shoulders — the
    same way a photograph does — so "does Dough fit" is a question about the
    head only, and asking it of every pixel would demand a crop with the whole
    dog inside the circle, which is the slack framing that makes him a speck.

    The split is measured, not assumed: the silhouette peaks at the ear tips
    and pinches hard at the neck (701px wide down to 338px in the source), and
    the ears stop well above that pinch.
    """
    alpha = np.asarray(Image.open(path).convert('RGBA').getchannel('A')) > 8
    width = alpha.sum(axis=1)
    h = len(width)
    ear = int(np.argmax(width))
    neck = ear + int(np.argmin(width[ear:int(h * 0.80)]))
    ys, xs = np.nonzero(alpha[:neck])
    return xs, ys, ear, neck, alpha.shape


def test_avatar_crop_is_square():
    """A non-square head crop would squash him — .dough-avatar sizes both axes
    and the image is object-fit: contain inside it."""
    im = Image.open(HEAD)
    assert im.width == im.height, f'head crop is {im.width}x{im.height}'


def test_head_and_ears_fit_inside_the_avatar_disc():
    """His face, whole, inside the circle the square inscribes.

    This is the one that has regressed before, twice — both times the ear tips
    were shaved into a flat vertical edge, and ear-to-ear is the silhouette
    that makes him read as a dog at 28px.
    """
    xs, ys, _, _, (h, w) = _portrait()
    cx, cy, radius = w / 2, h / 2, min(w, h) / 2
    worst = np.hypot(xs - cx, ys - cy).max()
    assert worst <= radius, (
        f'Dough is clipped: his head reaches {worst:.1f} from the disc centre '
        f'but the radius is {radius:.1f}. Re-run tools/build_dough_assets.py.')


def test_the_face_is_centred_in_the_avatar_disc():
    """Fitting is not enough — an off-centre face reads as a mistake even when
    nothing is cut, because the rim makes the gap obvious.

    Measured two ways, because the crop is a portrait and its ink is
    deliberately bottom-weighted: sideways on the head's own centre, and
    vertically on the *ear line*. The widest row has to sit on the widest part
    of the circle, or the ears run out of room before anything else does.
    """
    xs, ys, ear, _, (h, w) = _portrait()
    radius = min(w, h) / 2

    dx = abs(w / 2 - (xs.min() + xs.max()) / 2)
    assert dx / radius <= 0.04, (
        f'Dough sits {dx:.1f}px ({dx / radius:.1%}) left or right of the '
        'centre of his own disc')

    dy = abs(h / 2 - ear)
    assert dy / radius <= 0.14, (
        f'the ear line sits {dy:.1f}px ({dy / radius:.1%}) off the disc\'s '
        'horizontal diameter, which is the only place wide enough for it')


def test_mascot_very_nearly_fills_the_avatar_disc():
    """The complement of the clipping test. Adding air is the obvious way to
    "fix" clipping and it makes him a speck at 28px, so the floor is pinned."""
    xs, ys, _, _, (h, w) = _portrait()
    cx, cy, radius = w / 2, h / 2, min(w, h) / 2
    worst = np.hypot(xs - cx, ys - cy).max()
    assert worst / radius >= 0.88, (
        f'Dough only fills {worst / radius:.1%} of his disc — the crop has '
        'gone slack and he will read as tiny at avatar sizes')


def test_javascript_knows_the_real_pixel_sizes(js):
    """dough.js writes width/height onto the <img> so a slot reserves its space
    before the image loads. Those numbers are copied from the files, and a
    rebuild that changes a crop would silently leave them stale — which shows
    up as the page jolting on every load."""
    block = re.search(r'var ART = \{(.*?)\n  \};', js, re.S)
    assert block, 'could not find the ART table in dough.js'
    found = list(re.finditer(r"src: '([^']+)',\s*w: (\d+), h: (\d+)", block.group(1)))
    assert len(found) == 2, f'expected two crops in ART, found {len(found)}'
    for m in found:
        name, w, h = m.group(1), int(m.group(2)), int(m.group(3))
        real = Image.open(IMG / name).size
        assert real == (w, h), (
            f'dough.js says {name} is {w}x{h}, the file is {real[0]}x{real[1]} '
            '— rerun tools/build_dough_assets.py and update ART')


# ── Placement ───────────────────────────────────────────────────────────────

def test_avatar_svg_is_never_scaled_past_its_disc():
    """The other half of the old bug: the crop was also blown up to 112% inside
    an overflow:hidden disc, which cropped him again on top of the framing."""
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


def _vocabulary(js):
    """Every name a template may legally put in data-dough."""
    states = re.search(r'var STATES = \{(.*?)\n  \};', js, re.S)
    aliases = re.search(r'var ALIASES = \{(.*?)\n  \};', js, re.S)
    assert states and aliases, 'could not find STATES/ALIASES in dough.js'
    return (set(re.findall(r'^\s{4}(\w+):', states.group(1), re.M)),
            set(re.findall(r'^\s{4}(\w+):', aliases.group(1), re.M)))


def test_every_mascot_slot_names_a_real_state(js):
    """data-dough is the only attribute dough.js hydrates, and an unknown name
    silently falls back to the default. The error page previously used
    data-dough-expression and rendered nothing at all."""
    states, aliases = _vocabulary(js)
    known = states | aliases
    assert 'idle' in states and 'celebrate' in states, states

    for path in sorted(TEMPLATES.glob('*.html')):
        text = path.read_text(encoding='utf-8')
        assert 'data-dough-expression' not in text, (
            f'{path.name} uses data-dough-expression; dough.js hydrates '
            '[data-dough] only, so nothing would render')
        for name in re.findall(r'data-dough="([a-z]+)"', text):
            assert name in known, f'{path.name} asks for unknown state {name!r}'


def test_every_alias_points_at_a_real_state(js):
    """The aliases exist so ~20 templates did not need rewriting. An alias
    pointing at a state that has since been renamed resolves to the default
    and quietly downgrades that placement to a plain float."""
    states, _ = _vocabulary(js)
    block = re.search(r'var ALIASES = \{(.*?)\n  \};', js, re.S).group(1)
    for name, target in re.findall(r"(\w+):\s*'(\w+)'", block):
        assert target in states, (
            f'alias {name!r} points at {target!r}, which is not a state')


def test_the_state_effects_are_ui_not_artwork(js):
    """The dots and the confetti must be siblings of the image.

    They are the props that used to be drawn onto the mascot. Rebuilding them
    as anything that renders *inside* or *on top of* the artwork would be
    drawing on Dough again by another route.
    """
    assert 'dough-dots' in js and 'dough-confetti' in js
    body = re.sub(r'/\*.*?\*/', '', js, flags=re.S)
    # Whatever they are, they are spans — not SVG, and not an <img> overlay.
    assert '<svg' not in body
    assert len(re.findall(r'<img\b', body)) == 1, (
        'more than one <img> in dough.js — an overlay on the artwork is still '
        'a change to the artwork')
