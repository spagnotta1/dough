"""`.ds-progress` — the bar has to be on screen, not merely in the stylesheet.

This file exists because of a bug no template test and no CSS test could have
caught. `.ds-progress__fill` sets `width` and `height` to percentages and takes
its colour from a status modifier, and callers write it as either a `<div>` or a
`<span>`. A `<span>` is inline, and percentages on an inline box have nothing to
resolve against: the element computed to `rgb(22, 163, 74)` at `width: 100%` and
painted a 0x0 box. Every assertion anybody would think to write passed — the
class was right, the inline style was right, `getComputedStyle` reported the
green — and the Financial health card on /insights showed four empty grey
tracks, which is the one thing that card exists not to do.

So the assertion here is the painted geometry: `getBoundingClientRect`, which is
the only thing in the stack that knows the difference.
"""

import pytest

from .conftest import visit


def _fills(page, selector='.ins-factor'):
    """Each factor's painted fill, as a fraction of its track."""
    return page.evaluate("""(sel) => Array.from(
      document.querySelectorAll(sel)).map(row => {
        const track = row.querySelector('.ds-progress');
        const fill = row.querySelector('.ds-progress__fill');
        const t = track.getBoundingClientRect();
        const f = fill.getBoundingClientRect();
        return {
          name: row.querySelector('.ins-factor__name').textContent.trim(),
          score: parseInt(row.querySelector('.ins-factor__score').textContent, 10),
          fraction: t.width ? f.width / t.width : 0,
          height: f.height,
          colour: getComputedStyle(fill).backgroundColor,
          badge: row.querySelector('.ds-badge').textContent.trim(),
        };
      })""", selector)


@pytest.fixture()
def health(signed_in):
    visit(signed_in, '/insights')
    signed_in.wait_for_selector('.ins-factor')
    rows = _fills(signed_in)
    assert rows, 'the Financial health card rendered no factors to measure'
    return rows


def test_every_factor_bar_paints_a_box(health):
    """The regression itself: an inline fill is 6px of nothing."""
    for row in health:
        assert row['height'] > 0, (
            f"{row['name']}'s bar has no height, so nothing it is coloured with "
            f"can reach the screen")


def test_the_fill_is_as_long_as_the_score_says(health):
    """A bar that does not track its own number is worse than no bar."""
    for row in health:
        expected = row['score'] / 100.0
        assert abs(row['fraction'] - expected) < 0.02, (
            f"{row['name']} scores {row['score']} but its bar fills "
            f"{row['fraction'] * 100:.0f}% of the track")


def test_a_full_score_is_visibly_full(health):
    """The end of the range is where an off-by-a-box error hides."""
    full = [r for r in health if r['score'] >= 99]
    if not full:
        pytest.skip('no factor is at the top of its range on this ledger')
    for row in full:
        assert row['fraction'] > 0.98


def test_status_is_carried_by_a_word_and_not_only_by_a_colour(health):
    """Greyscale, projector, red/green deficiency: the badge still reads."""
    known = {'Strong', 'Fair', 'Needs work', 'No data', 'Not measured'}
    for row in health:
        assert row['badge'] in known, (
            f"{row['name']} is badged {row['badge']!r}, which is not one of the "
            f"words the template maps a status to")


def test_the_colour_agrees_with_the_word(health):
    """Two signals that can disagree are worse than one."""
    green, red, amber = 'rgb(22, 163, 74)', 'rgb(239, 68, 68)', 'rgb(245, 158, 11)'
    expected = {'Strong': green, 'Needs work': red, 'Fair': amber}
    for row in health:
        want = expected.get(row['badge'])
        if want is None:            # "No data" is drawn from the foreground
            continue
        assert row['colour'] == want, (
            f"{row['name']} is badged {row['badge']} but its bar is "
            f"{row['colour']}")
