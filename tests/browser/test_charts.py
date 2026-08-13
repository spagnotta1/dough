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


# ── Drill-down on the ranked bars ───────────────────────────────────────────
#
# These do render a chart, because the behaviour under test is a pointer event
# landing on a canvas — there is no DOM element to click and no server route to
# exercise. The panel is collapsed for a first-time reader, so each test opens
# it and waits for the chart to be built lazily.

#: Enough extra spending categories to push the chart past its six bars, so an
#: "Other" bucket is guaranteed. Added to the rendered payload rather than to
#: the ledger: the shared fixture is read-only here, and how many of its
#: categories fall inside the dashboard's default window depends on the day of
#: the month — seeded rows would make the overflow appear only some of the time.
OVERFLOW_PAD = 8


def _open_ranked(page, pad=False):
    """Expand "Top categories" and wait for its spending chart to exist.

    `pad` rewrites `#dashData` and re-runs the dashboard's init before the
    panel is opened, which is the same door the SPA router comes through on
    every navigation — the charts are built lazily, so nothing has read the
    payload yet.
    """
    visit(page, '/')
    if pad:
        page.evaluate(
            """(n) => {
                 const node = document.getElementById('dashData');
                 const data = JSON.parse(node.textContent);
                 for (let i = 0; i < n; i++) {
                   data.categoryStats['Filler ' + i] = { inbound: 0, outbound: 5 + i };
                 }
                 node.textContent = JSON.stringify(data);
                 window._initDashboard();
               }""", OVERFLOW_PAD)
    page.click('button[data-toggle="ranked"]')
    page.wait_for_function(
        "() => { const c = Chart.getChart(document.getElementById('outgoChart'));"
        "        return c && c.getDatasetMeta(0).data.length > 0; }")


def _bars(page):
    """Every spending bar as {label, x, y, hit} in viewport coordinates.

    The point is the bar's own midpoint — `el.base` is where it starts and
    `el.x` is its tip — because a short bar is only a few pixels long and an
    offset from the tip would land outside it. `hit` is Chart.js's own answer
    for whether that point is on the bar, so a test cannot pass by clicking
    empty canvas: the charts use `intersect: true`, and a miss is a no-op that
    looks exactly like "this bar does not drill down".
    """
    return page.evaluate(
        """() => {
             const canvas = document.getElementById('outgoChart');
             const chart = Chart.getChart(canvas);
             const box = canvas.getBoundingClientRect();
             return chart.getDatasetMeta(0).data.map((el, i) => {
               const cx = (el.base + el.x) / 2;
               return {
                 label: chart.data.labels[i],
                 x: box.left + cx,
                 y: box.top + el.y,
                 hit: el.inRange(cx, el.y)
               };
             });
           }""")


def test_clicking_a_spending_bar_opens_that_category_in_the_ledger(signed_in):
    """The drill-down, and the filters it has to carry.

    Category alone is not enough. The ledger's filters are sticky in the
    session (`sticky_filter`), so a link carrying only `category` lands on
    that category crossed with whatever range was last set over there — a
    different number than the bar the user just clicked.
    """
    page = signed_in
    _open_ranked(page)

    # The largest bar rather than a named category: which categories fall
    # inside the dashboard's default window depends on the day of the month.
    target = next(b for b in _bars(page) if b['label'] != 'Other')
    assert target['hit'], f'the click point missed the {target["label"]} bar'

    # Survives a soft navigation, not a document load — see the assertion below.
    page.evaluate('() => { window.__drillProbe = 1; }')
    page.mouse.click(target['x'], target['y'])
    page.wait_for_url('**/transactions?**')

    url = page.url
    assert f'category={target["label"]}' in url.replace('%20', ' '), \
        f'the clicked category did not reach the ledger: {url}'
    assert 'type=outgo' in url, f'the drill-down lost the direction: {url}'
    assert 'start_date=' in url and 'end_date=' in url, \
        f'the drill-down lost the dashboard period: {url}'

    rows = page.locator('table tbody tr')
    assert rows.count() > 0, 'the drill-down landed on an empty ledger'
    categories = page.locator('table tbody tr td:nth-child(6) select').evaluate_all(
        '(els) => els.map(e => e.value)')
    assert all(c == target['label'] for c in categories), \
        f'the drill-down leaked other categories: {categories}'

    # A bar has to reach the ledger the same way a link does. The fallback in
    # `navigate()` is a full document load, which works but drops the SPA's
    # transition and its history entry — worth knowing if it ever becomes the
    # path everyone takes because `spaNavigate` stopped being reachable.
    assert page.evaluate('() => window.__drillProbe') == 1, \
        'the drill-down fell back to a hard page load instead of the SPA router'


def test_the_other_bar_does_not_drill_down(signed_in):
    """"Other" is several categories folded together.

    The ledger filters on one category at a time, so any URL built for that
    bar would show less than the bar does. It must do nothing rather than
    quietly under-report.
    """
    page = signed_in
    _open_ranked(page, pad=True)

    other = next((b for b in _bars(page) if b['label'] == 'Other'), None)
    assert other is not None, \
        f'padding the payload by {OVERFLOW_PAD} categories produced no "Other" bar'
    assert other['hit'], 'the click point missed the "Other" bar, so this proves nothing'

    page.mouse.click(other['x'], other['y'])
    page.wait_for_timeout(400)

    assert '/transactions' not in page.url, \
        f'clicking "Other" navigated to a filter that cannot represent it: {page.url}'
