"""Regenerate the app icons from the mascot artwork.

    python tools/build_dough_assets.py   # first — cuts Dough out of the JPG
    python tools/build_icons.py          # then — composites him onto tiles

Everything below is generated and should not be hand-edited:

    app-icon-512.png           PWA / install prompt
    app-icon-192.png           PWA / Android home screen
    app-icon-maskable-512.png  Android adaptive icon (full-bleed, safe-zone fit)
    app-icon-maskable-192.png  ditto
    apple-touch-icon.png       iOS home screen (180px)
    favicon-48.png             browser tab / Windows
    favicon-32.png             browser tab
    favicon-16.png             browser tab
    favicon.ico                legacy / Windows pinned sites
    ../plaid_app_icon.png      uploaded to Plaid's dashboard by hand (1024px)

plaid_app_icon.png is generated here rather than kept by hand because that is
exactly how it went stale: nothing in the codebase reads it — it is uploaded to
Plaid's dashboard manually — so it sat as a purple checkmark from two logos ago
while every other icon moved on. It lives at the repo root because that is
where the upload step looks for it.

**The source is the photograph, not a drawing.** This script used to rasterize
`static/img/app-icon.svg`, a hand-authored vector redraw of the mascot, by
screenshotting it in headless Edge. Both are gone: the redraw because it was a
different dog (see dough.js), and the browser because compositing two PNGs
needs no browser. `tools/build_dough_assets.py` produces the cut-outs; this
script only places them on a tile.

Two framings, because a launcher and a browser tab are not the same problem:

    full  the whole seated puppy on a rounded tile — home screens, install
          prompts, anywhere the icon gets 180px or more.
    mark  Dough's eyes and nose, filling the tile — 16, 32 and 48px, and the
          .ico. A mascot illustration does not survive 16x16: the seated dog
          and even the head crop collapse into a brown smudge, which is why
          every brand simplifies for a favicon. The mark is a *crop* of the
          same approved artwork (see mark_box in build_dough_assets.py), not a
          second drawing, and it is frozen — it should not be re-derived on
          taste.
"""

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "static", "img")

FULL = os.path.join(IMG, "dough.png")
MARK = os.path.join(IMG, "dough-mark.png")

#: The reference artwork's own background, so the tile and the drawing are the
#: pairing the illustrator chose rather than one this script invented.
TILE = (243, 239, 230, 255)

#: Corner radius as a fraction of the tile, matching the previous icon set.
RADIUS = 114 / 512

#: How much of the tile the drawing occupies, by height. The mark runs close to
#: the edge on purpose: at 16px every pixel of padding is 6% of the icon.
FILL_FULL = 0.76
FILL_MARK = 0.94

#: Android guarantees only the central 80% *circle* of a maskable icon. A
#: bounding box survives that circle when its diagonal fits, and the full
#: drawing is 465x512, whose diagonal is 1.351 of its height — so 0.8/1.351
#: is the tallest it may be. Rounded down for margin.
FILL_MASKABLE = 0.58


def tile(px, radius):
    """A rounded (or square) background tile, transparent outside the corners."""
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if radius:
        d.rounded_rectangle([0, 0, px - 1, px - 1], radius=radius, fill=TILE)
    else:
        d.rectangle([0, 0, px - 1, px - 1], fill=TILE)
    return img


def compose(art, px, fill, radius, drop=0.0):
    """Place `art` on a tile of `px`, occupying `fill` of it by height.

    `drop` nudges the drawing down as a fraction of the tile — a seated dog
    centred on its bounding box reads as floating, because the visual weight
    is all in the lower half.
    """
    canvas = tile(px, round(px * radius) if radius else 0)
    h = round(px * fill)
    w = round(art.width * h / art.height)
    art = art.resize((w, h), Image.LANCZOS)
    canvas.paste(art, ((px - w) // 2, round((px - h) / 2 + px * drop)), art)
    return canvas


def main():
    for path in (FULL, MARK):
        if not os.path.exists(path):
            raise SystemExit(
                f"missing {os.path.basename(path)} — run "
                "`python tools/build_dough_assets.py` first")

    full = Image.open(FULL).convert("RGBA")
    mark = Image.open(MARK).convert("RGBA")

    big = compose(full, 1024, FILL_FULL, RADIUS, drop=0.02)
    bleed = compose(full, 1024, FILL_MASKABLE, 0)
    small = compose(mark, 512, FILL_MARK, RADIUS)

    for size, name in [(512, "app-icon-512.png"), (192, "app-icon-192.png"),
                       (180, "apple-touch-icon.png")]:
        big.resize((size, size), Image.LANCZOS).save(os.path.join(IMG, name))
    for size, name in [(512, "app-icon-maskable-512.png"),
                       (192, "app-icon-maskable-192.png")]:
        bleed.resize((size, size), Image.LANCZOS).save(os.path.join(IMG, name))
    for size, name in [(48, "favicon-48.png"), (32, "favicon-32.png"),
                       (16, "favicon-16.png")]:
        small.resize((size, size), Image.LANCZOS).save(os.path.join(IMG, name))

    big.save(os.path.join(ROOT, "plaid_app_icon.png"))
    # Legacy .ico only needs the small sizes — the PNG links cover the rest.
    small.resize((64, 64), Image.LANCZOS).save(
        os.path.join(IMG, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)])

    print("icons written to static/img")


if __name__ == "__main__":
    main()
