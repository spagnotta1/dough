"""The chart palette, exercised in a real engine.

`static/js/chart-theme.js` is where the colorblind-safety guarantees live, and
none of them can be checked by reading Python: the slot registry is client
state, the neutral is picked from the live color scheme, and the folding
decision depends on both. So these drive the module directly through
`page.evaluate` rather than through a rendered chart.

Driving it directly is also what keeps the shared browser ledger out of it.
Reproducing the reported bug through a rendered dashboard would mean seeding
more than eight spending categories into `conftest.py`, and that fixture's
transaction and category counts are asserted on by a dozen other tests.
"""

from .conftest import visit


#: More categories than the palette has slots. Registration order *is* slot
#: order, so `tail_*` are the ones past the eighth — the overflow.
REGISTRY = ['Food', 'Gas', 'Shopping', 'Insurance', 'Travel', 'Health',
            'Subs', 'Pets', 'tail_one', 'tail_two', 'tail_three']


def _fold(page, series, months=None):
    """Register REGISTRY, then fold `series` and return the datasets."""
    return page.evaluate(
        """([registry, months, series]) => {
             CheckCharts.registerCategories(registry);
             return CheckCharts.foldOverflow(months, series).map(s => ({
               label: s.label, data: s.data, color: s.color
             }));
           }""",
        [REGISTRY, months if months is not None else ['2026-07', '2026-08'],
         series])


def test_series_past_the_palette_become_one_other_band(signed_in):
    """The reported bug: three legend entries, one indistinguishable color.

    "Spending by category over time" mapped every series it was given, so
    Insurance, Entertainment and Uncategorized — all holding slots past the
    eighth — came out the identical overflow gray. The legend named three
    things and the bars showed one.
    """
    visit(signed_in, '/')

    folded = _fold(signed_in, {
        'Food':       [10, 20],
        'tail_one':   [1, 2],
        'tail_two':   [3, 4],
        'tail_three': [5, 6],
    })

    labels = [d['label'] for d in folded]
    assert labels == ['Food', 'Other'], (
        f'the three overflow series must fold into one band, got {labels}')


def test_the_folded_band_still_reconciles_to_the_true_total(signed_in):
    """A fold that loses money is worse than the ambiguity it fixed."""
    visit(signed_in, '/')

    folded = _fold(signed_in, {
        'tail_one':   [1, 2],
        'tail_two':   [3, 4],
        'tail_three': [5, 6],
    })

    other = next(d for d in folded if d['label'] == 'Other')
    assert other['data'] == [9, 12]


def test_no_two_bands_ever_share_a_color(signed_in):
    """The invariant the whole exercise is for.

    Whatever the mix of named and overflow series, two bands must never be
    drawn in the same fill — that is precisely what the reader cannot decode.
    """
    visit(signed_in, '/')

    folded = _fold(signed_in, {
        'Food': [1, 1], 'Gas': [1, 1], 'Shopping': [1, 1], 'Insurance': [1, 1],
        'Travel': [1, 1], 'Health': [1, 1], 'Subs': [1, 1], 'Pets': [1, 1],
        'tail_one': [1, 1], 'tail_two': [1, 1], 'tail_three': [1, 1],
    })

    colors = [d['color'] for d in folded]
    assert len(colors) == len(set(colors)), (
        f'two bands share a fill color: {colors}')
    # Eight hues plus the one folded band.
    assert len(folded) == 9


def test_a_chart_inside_the_palette_is_left_alone(signed_in):
    """No "Other" appears when every series has a hue of its own.

    The fold is a safety net, not a permanent cap — a household with few
    categories must keep every one of them named.
    """
    visit(signed_in, '/')

    folded = _fold(signed_in, {'Food': [1, 1], 'Gas': [2, 2]})

    assert [d['label'] for d in folded] == ['Food', 'Gas']


def test_named_bands_come_back_in_palette_slot_order(signed_in):
    """Touching segments must hold consecutive slots.

    That adjacency is the palette's colorblind-safety mechanism — the pairs
    were validated as neighbours, so feeding datasets in dictionary order
    stacks pairs nobody checked. See `orderBySlot` in chart-theme.js.
    """
    visit(signed_in, '/')

    folded = _fold(signed_in, {'Shopping': [1, 1], 'Food': [1, 1],
                               'Gas': [1, 1], 'tail_one': [1, 1]})

    assert [d['label'] for d in folded] == ['Food', 'Gas', 'Shopping', 'Other']
