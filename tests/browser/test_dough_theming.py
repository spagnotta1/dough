"""Dough against every theme, measured in a real engine.

This file used to check that Dough's ``--dough-*`` colour tokens resolved
sensibly in all 16 themes. That layer is gone: the mascot is a photograph of a
finished brand asset (``static/img/dough_V2.jpg``) and you cannot re-tint a
raster without altering the artwork, which AGENTS.md forbids.

Removing the tokens did not remove the problem they were solving, though — it
inverted it. A themed Dough could always be pushed away from the panel behind
him. A fixed Dough cannot, so if a palette's surface drifts toward his fur, he
quietly turns into a smudge and the only available fix is to change the
*panel*. That is worth knowing at test time rather than from a screenshot
somebody happens to look at.

So the checks are now:

  **He renders at all.** An ``<img>`` whose ``src`` 404s is an invisible
  failure — no console error a test would catch, no layout change, just a
  missing dog. ``naturalWidth`` is the only honest signal.

  **Nobody is drawing him again.** The redraw shipped once. If a ``<svg>``
  reappears inside a mascot slot, that is it coming back.

  **He separates from every panel**, measured from his own rendered pixels
  rather than from a token, because there is no token any more.

Thresholds are deliberately low. These are not text-contrast rules — Dough
carries no information and every placement repeats it in adjacent copy — they
are "can you see the shape" rules, and holding a mascot to 4.5:1 would rule out
the warm, low-contrast palettes the product chose on purpose.
"""

import pytest

#: Fur against the panel behind it. Below this the dog stops being a shape and
#: becomes a smudge. 1.35 is roughly where a mid-tone fill starts reading as an
#: object rather than as a tint of the background.
MIN_FUR_ON_PANEL = 1.35

#: The avatar disc against the surface it sits on. The disc is the one part of
#: the mascot's presentation that still follows the theme, and it is what gives
#: a 28px Dough an edge instead of letting him float.
MIN_DISC_ON_SURFACE = 1.06


@pytest.fixture()
def page(signed_in):
    signed_in.goto('/', wait_until='load')
    return signed_in


def test_the_mascot_image_actually_loads(page):
    """A 404 on the artwork is silent: no console error, no layout shift."""
    broken = page.evaluate("""async () => {
      const imgs = Array.from(document.querySelectorAll('img.dough'));
      await Promise.all(imgs.map(i => i.decode().catch(() => {})));
      return imgs.filter(i => !i.naturalWidth).map(i => i.currentSrc || i.src);
    }""")
    assert not broken, f'these mascot images failed to load: {broken}'

    count = page.evaluate("document.querySelectorAll('img.dough').length")
    assert count, 'no mascot rendered on the dashboard at all'


def test_nothing_draws_a_vector_mascot(page):
    """The redraw shipped once. This is what it would look like coming back."""
    drawn = page.evaluate(
        "document.querySelectorAll('[data-dough] svg, svg.dough').length")
    assert drawn == 0, (
        f'{drawn} mascot slots contain an <svg> — Dough is '
        'static/img/dough_V2.jpg, not geometry. See AGENTS.md.')


def _measure(page):
    """Dough's own rendered pixels against each theme's panel.

    The fur colour is sampled from the decoded image rather than assumed, so
    this keeps working if the artwork is ever re-exported: it asks "what colour
    is the dog actually painting" and compares that to the surface.
    """
    return page.evaluate("""async () => {
      const img = document.querySelector('img.dough');
      await img.decode();

      // Modal opaque colour of the artwork — his fur, since it is most of him.
      const c = document.createElement('canvas');
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      const cx = c.getContext('2d', { willReadFrequently: true });
      cx.drawImage(img, 0, 0);
      const d = cx.getImageData(0, 0, c.width, c.height).data;
      const bins = new Map();
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] < 250) continue;
        // Quantise to 16 levels so antialiasing does not shatter the mode.
        const k = (d[i] >> 4) + ',' + (d[i+1] >> 4) + ',' + (d[i+2] >> 4);
        bins.set(k, (bins.get(k) || 0) + 1);
      }
      let best = null, most = 0;
      for (const [k, n] of bins) if (n > most) { most = n; best = k; }
      const fur = best.split(',').map(v => parseInt(v, 10) * 16 + 8);

      const probe = document.createElement('canvas').getContext('2d', {
        willReadFrequently: true,
      });
      const rgb = (css) => {
        probe.clearRect(0, 0, 1, 1);
        probe.fillStyle = '#000';
        probe.fillStyle = css;
        probe.fillRect(0, 0, 1, 1);
        return Array.from(probe.getImageData(0, 0, 1, 1).data).slice(0, 3);
      };
      const lum = (t) => {
        const [r, g, b] = t.map(v => {
          v /= 255;
          return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const ratio = (a, b) => {
        const la = lum(a), lb = lum(b);
        return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
      };

      const out = {};
      for (const key of Object.keys(THEMES)) {
        applyTheme(key);
        const root = getComputedStyle(document.documentElement);
        const panel = root.getPropertyValue('--panel').trim();
        const surface = root.getPropertyValue('--surface').trim();
        const disc = getComputedStyle(
          document.querySelector('.dough-avatar') || document.body
        ).backgroundColor;
        out[key] = {
          fur: fur.join(','),
          furOnPanel: ratio(fur, rgb(panel)),
          discOnSurface: ratio(rgb(disc), rgb(surface)),
        };
      }
      return out;
    }""")


@pytest.fixture()
def measured(page):
    return _measure(page)


def test_dough_is_visible_against_every_theme_panel(measured):
    """His fur separates from the surface behind him, in all 16 themes.

    He can no longer be tinted away from a panel he clashes with, so a failure
    here is fixed on the *theme* side — lift or drop that palette's --panel —
    not by touching the artwork.
    """
    weak = {k: round(v['furOnPanel'], 2) for k, v in measured.items()
            if v['furOnPanel'] < MIN_FUR_ON_PANEL}
    assert not weak, (
        f'Dough vanishes into the panel in these themes (need '
        f'{MIN_FUR_ON_PANEL}:1): {weak} — the mascot is fixed now, so adjust '
        f'--panel for those palettes')


def test_the_avatar_disc_still_reads_in_every_theme(measured):
    """The disc is what gives a 28px Dough an edge instead of letting him
    float, and it is now the only part of his presentation the theme drives."""
    flat = {k: round(v['discOnSurface'], 3) for k, v in measured.items()
            if v['discOnSurface'] < MIN_DISC_ON_SURFACE}
    assert not flat, (
        f'the avatar disc disappears into the surface in these themes (need '
        f'{MIN_DISC_ON_SURFACE}:1): {flat}')
