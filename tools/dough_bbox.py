"""Measure the Dough mascot artwork against the viewBoxes that frame it.

Why this exists
---------------
``.dough-avatar`` clips Dough to a **disc**. That makes the framing constraint
non-obvious: it is not "does the drawing fit inside the square viewBox", it is
"does the drawing fit inside the circle that square inscribes". Two hand-picked
crops got this wrong before, both times shaving the ear tips into a flat
vertical edge — and ear-to-ear is exactly the silhouette that makes Dough read
as a dog at 25-30px.

So the crop is measured rather than eyeballed. This script flattens every path
in ``static/js/dough.js``, inflates each sample by half its stroke width, walks
the poses (the baked -5 degree tilt) and the idle animations at their extremes
(bounce lifts 5%, ears twitch +-5 degrees), and reports the smallest circle that
contains all of it. ``TIGHT`` in dough.js is that circle plus ~5% air.

Run it after touching the artwork:

    python tools/dough_bbox.py

It exits non-zero if either viewBox in dough.js no longer contains the mascot.
"""

import math
import re
import sys
from pathlib import Path

DOUGH_JS = Path(__file__).resolve().parent.parent / 'static' / 'js' / 'dough.js'

# ─────────────────────────────────────────────────────────── path flattening
TOK = re.compile(r'([MmLlCcQqZzHhVv])|(-?\d*\.?\d+)')


def flatten(d, steps=48):
    """Sample points along a path. Absolute commands only, as authored."""
    toks = [m.group(1) or m.group(2) for m in TOK.finditer(d)]
    pts, i, cur, start, cmd = [], 0, (0.0, 0.0), (0.0, 0.0), None

    def num():
        nonlocal i
        v = float(toks[i]); i += 1
        return v

    while i < len(toks):
        t = toks[i]
        if isinstance(t, str) and t.isalpha():
            cmd = t; i += 1
            if cmd in 'Zz':
                cur = start
                continue
        if cmd == 'M':
            cur = (num(), num()); start = cur; pts.append(cur); cmd = 'L'
        elif cmd == 'L':
            cur = (num(), num()); pts.append(cur)
        elif cmd == 'C':
            p1, p2, p3 = (num(), num()), (num(), num()), (num(), num())
            for s in range(1, steps + 1):
                u = s / steps; m = 1 - u
                pts.append((m**3*cur[0] + 3*m*m*u*p1[0] + 3*m*u*u*p2[0] + u**3*p3[0],
                            m**3*cur[1] + 3*m*m*u*p1[1] + 3*m*u*u*p2[1] + u**3*p3[1]))
            cur = p3
        elif cmd == 'Q':
            p1, p2 = (num(), num()), (num(), num())
            for s in range(1, steps + 1):
                u = s / steps; m = 1 - u
                pts.append((m*m*cur[0] + 2*m*u*p1[0] + u*u*p2[0],
                            m*m*cur[1] + 2*m*u*p1[1] + u*u*p2[1]))
            cur = p2
        else:
            raise ValueError(f'unhandled path command {cmd!r}')
    return pts


# ───────────────────────────────────────────────────────────────── the parts
# Mirrors the geometry constants in dough.js. Kept as literals rather than
# scraped out of the JS so a rename there fails loudly here instead of
# silently measuring nothing.
EAR_L = ('M176 216 C132 196, 82 216, 58 262 C30 282, 26 340, 48 378 '
         'C58 420, 100 452, 142 440 C170 430, 182 406, 177 380 '
         'C186 326, 186 250, 176 216 Z')
EAR_R = ('M336 216 C380 196, 430 216, 454 262 C482 282, 486 340, 464 378 '
         'C454 420, 412 452, 370 440 C342 430, 330 406, 335 380 '
         'C326 326, 326 250, 336 216 Z')
HEAD = ('M131.9 278.5 Q125.1 255.7 144.8 241.8 Q145.4 217.9 168.5 210.8 '
        'Q176.5 188.3 200.8 188.6 Q215.5 169.5 238.5 177.2 Q258.5 163.6 277.9 177.9 '
        'Q301.2 171.0 315.2 190.5 Q339.4 191.1 346.6 213.9 Q369.4 221.9 369.2 245.7 '
        'Q388.4 260.2 380.8 282.7 C388 340, 380 396, 330 428 C302 445, 210 445, 182 428 '
        'C132 396, 124 340, 131.9 278.5 Z')
MUZZLE = ('M256 374 C250 356, 226 350, 206 352 C168 356, 146 380, 146 404 '
          'C146 432, 174 450, 206 450 C232 450, 250 440, 256 424 '
          'C262 440, 280 450, 306 450 C338 450, 366 432, 366 404 '
          'C366 380, 344 356, 306 352 C286 350, 262 356, 256 374 Z')
PAW_L = ('M150 430 C112 430, 92 448, 92 468 C92 486, 116 494, 152 494 '
         'C190 494, 214 486, 214 468 C214 448, 190 430, 150 430 Z')
PAW_R = ('M362 430 C400 430, 420 448, 420 468 C420 486, 396 494, 360 494 '
         'C322 494, 298 486, 298 468 C298 448, 322 430, 362 430 Z')
TOES = ['M130 462 L128 492', 'M172 462 L172 494',
        'M382 462 L384 492', 'M340 462 L340 494']
TUFT = 'M240 246 C244 224, 256 224, 258 242 C262 224, 274 226, 274 246'
TEXTURE = ['M84 300 C76 326, 78 356, 92 380', 'M132 332 C128 352, 132 372, 141 386',
           'M428 300 C436 326, 434 356, 420 380', 'M380 332 C384 352, 380 372, 371 386']
GRIN = 'M212 424 C222 472, 290 472, 300 424 Z'   # the widest mouth

# Props only ever draw in the full frame; the tight crop suppresses them.
# Static extents, from the shapes themselves. The lowercase z's are the one
# thing not measurable from geometry alone, so they use a conservative
# x-height model (a 'z' has neither ascender nor descender): the glyph runs
# from the baseline up 0.55em and right 0.55em per character.
def _z(x, y, size):
    return (x, y - 0.55 * size, x + 0.55 * size, y)


PROP_EXTENTS = [
    ('magnifier rim', 424 - 60, 228 - 60, 424 + 60, 228 + 60),
    ('magnifier handle', 461 - 10, 265 - 10, 498 + 10, 302 + 10),
    ('sparkle left', 56, 194, 128, 266),
    ('sparkle right', 424 - 29, 172 - 29, 424 + 29, 172 + 29),
    ('sparkle small', 372 - 21, 232 - 21, 372 + 21, 232 + 21),
    ('zzz large', *_z(398, 236, 52)),
    ('zzz medium', *_z(440, 190, 38)),
    ('zzz small', *_z(470, 154, 26)),
    ('confetti', 88, 154, 428, 216),
]

# The prop *animations* (sweep, twinkle, float, fall) push a few units past
# these boxes at their extremes. That is harmless — .dough sets
# overflow: visible, so the full frame never clips — but it means the FULL
# check wants slack where the disc check wants none.
FULL_TOLERANCE = 16.0


def scale_about(pts, cx, cy, k):
    return [(cx + (x - cx) * k, cy + (y - cy) * k) for x, y in pts]


def rotate_about(pts, cx, cy, deg):
    a = math.radians(deg); c, s = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * c - (y - cy) * s,
             cy + (x - cx) * s + (y - cy) * c) for x, y in pts]


def parts():
    """(sample points, half stroke width) for every shape drawn in tight mode."""
    out = [(flatten(EAR_L), 5), (flatten(EAR_R), 5), (flatten(HEAD), 5),
           (scale_about(flatten(MUZZLE), 256, 404, 1.1), 5),
           (flatten(TUFT), 4), (flatten(GRIN), 4.5),
           (flatten(PAW_L), 5), (flatten(PAW_R), 5)]
    out += [(flatten(t), 3.5) for t in TEXTURE]
    out += [(flatten(t), 4) for t in TOES]
    return out


def inflate(pts, hw):
    """Push each sample out by half its stroke, in eight directions."""
    return [(x + hw * math.cos(k * math.pi / 4), y + hw * math.sin(k * math.pi / 4))
            for x, y in pts for k in range(8)]


def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def enclosing_circle(pts):
    """Smallest enclosing circle. Iterative shrink — good to well under a unit."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    step = 64.0
    for _ in range(400):
        fx, fy = max(pts, key=lambda p: (p[0]-cx)**2 + (p[1]-cy)**2)
        cx += (fx - cx) * step / 1000.0
        cy += (fy - cy) * step / 1000.0
        step *= 0.99
    return cx, cy, max(math.hypot(p[0]-cx, p[1]-cy) for p in pts)


# ───────────────────────────────────────────────── poses and idle animations
def head_poses(base):
    """Whole-head transforms: dough-tilt, dough-breathe, dough-bounce.

    All three use transform-box: fill-box, so their percentage origins and
    translations resolve against the artwork's own bounding box.
    """
    x0, y0, x1, y1 = bbox(base)
    w, h = x1 - x0, y1 - y0
    out = [base]
    ox, oy = x0 + .5 * w, y0 + .95 * h              # tilt origin: 50% 95%
    out += [rotate_about(base, ox, oy, deg) for deg in (-4.5, 2.5)]
    ox, oy = x0 + .5 * w, y1                        # breathe/bounce: 50% 100%
    for dy, sx, sy in ((-.012, 1.012, 1.012), (-.05, .985, 1.02), (0, 1.02, .98)):
        out.append([(ox + (x - ox) * sx, oy + (y - oy) * sy + dy * h) for x, y in base])
    return out


def ear_twitch(shapes):
    """dough-twitch-l/r: each ear rotates +-5 degrees about its own 88%/12%."""
    out = []
    for idx, origin_x in ((0, .88), (1, .12)):
        x0, y0, x1, y1 = bbox(shapes[idx])
        ox, oy = x0 + origin_x * (x1 - x0), y0 + .12 * (y1 - y0)
        out += [rotate_about(shapes[idx], ox, oy, deg) for deg in (-5, 5)]
    return out


def every_point():
    """Every unit of ink the mascot can occupy, across poses and animations."""
    shapes = [inflate(pts, hw) for pts, hw in parts()]
    pts = []
    for tilt in (0, -5):        # the resting tilt baked into curious/thinking
        posed = [rotate_about(s, 256, 470, tilt) if tilt else s for s in shapes]
        flat = [p for s in posed for p in s]
        for variant in head_poses(flat):
            pts += variant
        for variant in ear_twitch(posed):
            pts += variant
    return pts


# ────────────────────────────────────────────────────────────────── checking
def read_viewboxes():
    src = DOUGH_JS.read_text(encoding='utf-8')
    out = {}
    for name in ('FULL', 'TIGHT'):
        m = re.search(rf"var\s+{name}\s*=\s*'([-\d.\s]+)'", src)
        if not m:
            raise SystemExit(f'could not find {name} viewBox in {DOUGH_JS}')
        out[name] = [float(v) for v in m.group(1).split()]
    return out


def main():
    pts = every_point()
    x0, y0, x1, y1 = bbox(pts)
    cx, cy, r = enclosing_circle(pts)
    vb = read_viewboxes()
    ok = True

    print('mascot ink, every pose and animation extreme')
    print(f'  bbox            x {x0:7.1f} .. {x1:7.1f}   y {y0:7.1f} .. {y1:7.1f}')
    print(f'  enclosing circle  centre ({cx:.1f}, {cy:.1f})   radius {r:.1f}')

    # TIGHT is clipped to a disc, so the circle is the constraint.
    tx, ty, tw, th = vb['TIGHT']
    print(f'\nTIGHT  "{tx:g} {ty:g} {tw:g} {th:g}"  (avatar crop, clipped to a disc)')
    if abs(tw - th) > 0.01:
        print('  FAIL  not square — .dough-avatar sizes both axes, so Dough would squash')
        ok = False
    disc_cx, disc_cy, disc_r = tx + tw / 2, ty + th / 2, min(tw, th) / 2
    worst = max(math.hypot(p[0] - disc_cx, p[1] - disc_cy) for p in pts)
    print(f'  disc            centre ({disc_cx:.1f}, {disc_cy:.1f})   radius {disc_r:.1f}')
    print(f'  furthest ink    {worst:.1f} from centre  ({worst / disc_r:.1%} of the radius)')
    if worst > disc_r:
        print(f'  FAIL  clipped by {worst - disc_r:.1f} units')
        ok = False
    elif worst / disc_r > 0.98:
        print('  WARN  ink is touching the rim; give it a little more air')
    else:
        print(f'  ok    {disc_r - worst:.1f} units of air at the tightest point')
    off = math.hypot(disc_cx - cx, disc_cy - cy)
    print(f'  off-centre      {off:.1f} units ({off / disc_r:.1%} of the radius)')
    if off / disc_r > 0.03:
        print('  FAIL  Dough is not centred in his own disc')
        ok = False

    # FULL is never clipped (.dough has overflow: visible); the check is that
    # Dough and his props sit inside their declared box, so a caller that
    # reserves the SVG's own footprint gets no overlap with what is beside it.
    fx, fy, fw, fh = vb['FULL']
    print(f'\nFULL   "{fx:g} {fy:g} {fw:g} {fh:g}"  (empty states, heroes — props draw here)')
    print(f'       {FULL_TOLERANCE:g} units of slack allowed: prop animations graze the edge '
          'and nothing clips there')
    checks = [('mascot', x0, y0, x1, y1)] + list(PROP_EXTENTS)
    t = FULL_TOLERANCE
    for name, a, b, c, d in checks:
        over = max(fx - a, fy - b, c - (fx + fw), d - (fy + fh), 0.0)
        print(f'  {"ok  " if over <= t else "FAIL"}  {name:<18} '
              f'x {a:7.1f}..{c:7.1f}  y {b:7.1f}..{d:7.1f}'
              + (f'   ({over:.1f} past the edge)' if over else ''))
        ok = ok and over <= t

    print('\n' + ('all framing checks passed' if ok else 'FRAMING IS WRONG — see above'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
