"""Cut the shipped mascot PNGs out of the reference artwork.

`static/img/dough_V2.jpg` is the mascot. It is the *only* Dough artwork this
project has, and everything the app renders is a crop or a scale of it — see
AGENTS.md. This script is how those crops are produced, so that "which pixels
ship" is a recorded measurement rather than something somebody did once in an
image editor and cannot reproduce.

    python tools/build_dough_assets.py

Two things here are less obvious than they look.

**The background has to come off by connectivity, not by colour.** The
reference's background is a near-uniform cream (243, 239, 230) — but the
catchlights in Dough's eyes are pure white, which is only 25 levels away from
it. Any "make every pixel near the background transparent" pass punches two
holes in his eyes and nobody notices until he is on a dark theme. So the
transparency is a flood fill seeded from the image border: the background is
whatever is *connected to the edge*, and the enclosed white stays.

The tolerance is deliberately generous (55). The reference's soft drop shadow
is part of that connected region and is removed with it, because `.dough` casts
its own shadow in CSS and a baked one cannot follow the theme.

**Transparent pixels keep a dog-coloured RGB.** A transparent cream pixel is
still cream to a downscaler, which is where the pale halo around a shrunk PNG
comes from. After the alpha is computed, the colour of the cleared region is
overwritten by dilating Dough's own edge colours outward, so a 36px avatar
samples brown-into-brown instead of brown-into-cream.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / 'static' / 'img'

#: The archival master. Deliberately outside `static/`: it is the source the
#: runtime assets are cut from, not something the application serves. Leaving
#: it under static/ meant a 183KB background-baked JPG was publicly fetchable
#: and one typo away from being the thing a page rendered.
REFERENCE = ROOT / 'brand' / 'dough-master.jpg'

#: Max per-channel distance from the border colour still counted as background.
#: 55 clears the soft drop shadow; Dough's lightest fur is ~170 away, and his
#: outline ~190, so there is a wide margin before this could bite into him.
TOLERANCE = 55

#: How far dog colour is pushed out into the cleared region, in pixels. Only
#: has to survive the worst downscale (a 1168px source into a 36px avatar).
DECONTAMINATE = 12

#: Emitted sizes. The full body is measured on height, the head crop is square
#: because `.dough-avatar` clips it to a disc.
FULL_HEIGHTS = {'dough.png': 512, 'dough@2x.png': 1024}
HEAD_SIZES = {'dough-head.png': 256, 'dough-head@2x.png': 512}

#: The small-size mark — eyes and nose only. See `mark_box()`.
MARK_SIZES = {'dough-mark.png': 256}

#: How much wider than the facial features the mark's square is. 1.30 was
#: chosen by rendering the candidates at 16, 32 and 48px and picking the one
#: that survived 16 — it is a frozen brand decision, not a tunable.
MARK_MARGIN = 1.30


def background_mask(rgb):
    """True where the pixel is background: near the border colour *and*
    reachable from the border without crossing Dough."""
    bg = np.median(np.concatenate(
        [rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]), axis=0)
    candidate = np.abs(rgb.astype(np.int16) - bg).max(axis=2) <= TOLERANCE

    reached = np.zeros(candidate.shape, bool)
    reached[0], reached[-1] = candidate[0], candidate[-1]
    reached[:, 0], reached[:, -1] = candidate[:, 0], candidate[:, -1]

    # Grow the seed through the candidate region until it stops changing. Four
    # shifts per pass; a few hundred passes covers the deepest pocket (the gap
    # between his front legs), and each pass is a handful of numpy ops.
    while True:
        grown = reached.copy()
        grown[1:] |= reached[:-1]
        grown[:-1] |= reached[1:]
        grown[:, 1:] |= reached[:, :-1]
        grown[:, :-1] |= reached[:, 1:]
        grown &= candidate
        if np.array_equal(grown, reached):
            return reached
        reached = grown


def grow(seed, within):
    """Flood `seed` outward, constrained to `within`, until it stops changing."""
    reached = seed
    while True:
        g = reached.copy()
        g[1:] |= reached[:-1]
        g[:-1] |= reached[1:]
        g[:, 1:] |= reached[:, :-1]
        g[:, :-1] |= reached[:, 1:]
        g &= within
        if np.array_equal(g, reached):
            return reached
        reached = g


def only_dough(foreground, rgb):
    """Keep Dough's own blob and discard everything else.

    The flood fill leaves fragments behind: the core of the baked drop shadow
    sits in the pocket under his chest, enclosed by his legs and the ground
    shading rather than connected to the image border, so it is never reached
    and survives as grey specks around his paws — invisible on a cream page and
    obvious the moment he is on a dark theme.

    Dough is a single closed shape, so the fix is to keep one connected
    component. The seed is the darkest pixel in the image, which is always his
    outline: the shadow is a light grey and cannot compete for that.
    """
    lum = rgb.astype(np.int32) @ np.array([299, 587, 114])
    seed = np.zeros(foreground.shape, bool)
    seed[np.unravel_index(np.argmin(np.where(foreground, lum, 1 << 30)),
                          foreground.shape)] = True
    return grow(seed, foreground)


def decontaminate(rgb, alpha):
    """Push Dough's colours outward into the cleared pixels."""
    out = rgb.copy()
    known = alpha > 0
    for _ in range(DECONTAMINATE):
        if known.all():
            break
        src, dst = np.zeros_like(known), np.zeros_like(known)
        for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
            rolled_known = np.roll(known, shift, axis=axis)
            rolled_rgb = np.roll(out, shift, axis=axis)
            fill = rolled_known & ~known & ~dst
            out[fill] = rolled_rgb[fill]
            dst |= fill
            src |= rolled_known
        if not dst.any():
            break
        known |= dst
    return out


def cut_out():
    """The reference as RGBA, background removed, trimmed to Dough."""
    im = Image.open(REFERENCE).convert('RGB')
    rgb = np.asarray(im)

    bg = background_mask(rgb)
    hard = np.where(only_dough(~bg, rgb), 255, 0).astype(np.uint8)

    # Order matters, and getting it backwards is what produces a pale halo.
    # The feather below pushes alpha *outward* onto pixels that are still
    # cream, so the colour has to be replaced first, against the hard mask.
    # Decontaminating afterwards would skip exactly those pixels — they are no
    # longer fully clear — and leave a ring of half-opaque background around
    # him, visible on every dark theme and on nothing else.
    rgb = decontaminate(rgb, hard)

    # Feather half a pixel so the outline does not stair-step when scaled.
    alpha = np.asarray(Image.fromarray(hard).filter(
        ImageFilter.GaussianBlur(0.6)))

    rgba = Image.fromarray(np.dstack([rgb, alpha]), 'RGBA')
    return rgba.crop(rgba.getchannel('A').getbbox())


def head_box(rgba):
    """The square around head + ears, found from the silhouette's own profile.

    Dough's width profile has a clear shape: it peaks at the ear tips, pinches
    at the neck, then widens again at the shoulders. The pinch is the crop's
    bottom edge. Measuring it beats guessing a viewBox — two hand-picked crops
    have already shaved his ear tips into a flat vertical edge.
    """
    a = np.asarray(rgba.getchannel('A')) > 8
    width = a.sum(axis=1)
    h = len(width)

    ear = int(np.argmax(width[:h // 2]))            # widest row up top
    neck = ear + int(np.argmin(width[ear:int(h * 0.65)]))  # the pinch below it

    cols = np.where(a[:neck].any(axis=0))[0]
    left, right = int(cols[0]), int(cols[-1])
    rows = np.where(a[:neck].any(axis=1))[0]
    top = int(rows[0])

    # Square it around the head's *centre* on both axes. Anchoring the square
    # at the top instead drags half the body in, because ear-to-ear is wider
    # than crown-to-neck and the square takes the larger side. Centring also
    # puts the ear tips on the disc's horizontal diameter, which is the only
    # place a circle is wide enough to hold them.
    cx, cy = (left + right) / 2, (top + neck) / 2
    side = max(right - left, neck - top) * 1.06  # 6% air so nothing grazes
    x0, y0 = cx - side / 2, cy - side / 2
    # A box outside the source is fine: PIL pads it transparent.
    return (round(x0), round(y0), round(x0 + side), round(y0 + side)), ear, neck


def mark_box(rgba):
    """The frozen small-size mark: Dough's eyes and nose, nothing else.

    A mascot illustration does not survive 16x16 — the full seated dog, and
    even the head crop, collapse into a brown smudge. Every brand that ships a
    favicon simplifies for it, so this is Dough's simplified mark, and like the
    rest of him it is a *crop of the approved artwork* rather than a new
    drawing.

    Finding the face by measurement rather than by hand-picked coordinates:
    the eyes and nose are the only **thick** dark masses in the drawing. The
    outline is just as dark but only ~14px wide at this scale, so eroding the
    dark mask repeatedly strips the linework and leaves the features.

    The stopping rule is "erode past the collapse, then stop on the plateau".
    Two simpler rules both fail here, and both failed silently:

      *Last non-empty step* depends on the iteration cap — with a generous one
      the mask shrinks to a single pixel and the mark becomes a 1x2 crop.

      *First plateau* fires immediately, because the width barely moves for
      the first several passes while the outline is only losing its ends
      (679px → 679 → 679 …).

    The width sequence is 679 → 469 → 274 → 269: a cliff as the linework goes,
    then a plateau on the feature cores. So the collapse has to be observed
    before a plateau counts as the answer.

    Restricted to above the neck first, or the body's outline joins in. Guarded
    at the end, because every failure mode here produces a *plausible-looking*
    square that is simply the wrong part of the dog.
    """
    a = np.asarray(rgba)
    alpha, rgb = a[:, :, 3], a[:, :, :3]

    width = (alpha > 8).sum(axis=1)
    h = len(width)
    ear = int(np.argmax(width[:h // 2]))
    neck = ear + int(np.argmin(width[ear:int(h * 0.65)]))

    lum = rgb.astype(np.int32) @ np.array([299, 587, 114]) // 1000
    dark = (lum < 90) & (alpha > 200)
    dark[neck:] = False

    def span(mask):
        cols = np.nonzero(mask.any(axis=0))[0]
        return (cols[-1] - cols[0]) if len(cols) else 0

    prev, last, collapsed = dark, span(dark), False
    for _ in range(64):                      # cap: this must terminate
        e = prev.copy()
        e[1:] &= prev[:-1]; e[:-1] &= prev[1:]
        e[:, 1:] &= prev[:, :-1]; e[:, :-1] &= prev[:, 1:]
        if not e.any():
            break
        now = span(e)
        if now <= last * 0.90:
            collapsed = True
        elif collapsed and now >= last * 0.97:
            prev = e
            break
        prev, last = e, now

    ys, xs = np.nonzero(prev)
    features = xs.max() - xs.min()
    head_width = span(alpha[:neck] > 8)
    if not 0.25 <= features / head_width <= 0.70:
        raise SystemExit(
            f'mark_box() found a {features}px feature mass inside a '
            f'{head_width}px head ({features / head_width:.0%}) — that is not '
            'the eyes and nose. The erosion heuristic has not survived a '
            'change to the artwork; re-measure it before shipping a favicon.')

    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    side = features * MARK_MARGIN
    return (round(cx - side / 2), round(cy - side / 2),
            round(cx + side / 2), round(cy + side / 2))


def fit(im, size):
    """Scale onto a transparent square without distorting or clipping."""
    im = im.copy()
    im.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
    return canvas


def main():
    if not REFERENCE.exists():
        sys.exit(f'missing the reference artwork: {REFERENCE}')

    dog = cut_out()
    print(f'reference   {Image.open(REFERENCE).size}')
    print(f'cut out     {dog.size}  (background + drop shadow removed)')

    for name, height in FULL_HEIGHTS.items():
        w = round(dog.width * height / dog.height)
        dog.resize((w, height), Image.LANCZOS).save(IMG / name)
        print(f'  {name:<20} {w}x{height}')

    box, ear, neck = head_box(dog)
    head = dog.crop(box)
    print(f'head crop   {box}  (ear tips row {ear}, neck row {neck})')
    for name, size in HEAD_SIZES.items():
        fit(head, size).save(IMG / name)
        print(f'  {name:<20} {size}x{size}')

    mbox = mark_box(dog)
    mark = dog.crop(mbox)
    print(f'mark crop   {mbox}  (eyes + nose)')
    for name, size in MARK_SIZES.items():
        fit(mark, size).save(IMG / name)
        print(f'  {name:<20} {size}x{size}')

    return dog, head, mark


if __name__ == '__main__':
    main()
