"""Ask Dough, driven the way a person drives it.

``tests/test_chat_ux.py`` is the acceptance checklist for this surface, and it
asserts against the CSS and the JavaScript as text: that ``.u-bubble`` and
``.a-body`` do not resolve to the same background, that a ``thinking`` state
exists in the code, that ``overflow-wrap: anywhere`` is set. Those were the
strongest assertions available without a browser, and the file says so.

This is the other half. Here the sidebar actually opens, the composer actually
sends, tokens actually stream in, and the expanded chart is actually a modal
dialog. Chat gets its own file rather than joining the page sweep because it is
the only page in the product that is a live application rather than a document —
it holds a stream open, it mutates the DOM as the answer arrives, and almost
everything that can go wrong with it goes wrong after load.
"""

import json
import time

import pytest

from .conftest import assert_no_horizontal_overflow, visit, wait_for_layout

# A chart the model can be told to emit. Shaped to satisfy chat.js's parseSpec
# allowlist: a known type, labels, and one series whose data is the same length.
CHART_SPEC = {
    'type': 'bar',
    'title': 'Spending by category',
    'unit': 'usd',
    'labels': ['Groceries', 'Housing', 'Travel', 'Utilities'],
    'series': [{'name': 'July', 'data': [-225.28, -2150.0, -612.8, -118.4]}],
}
CHART_REPLY = ('Here is how July broke down.\n\n'
               '```chart\n' + json.dumps(CHART_SPEC) + '\n```\n')


#: The chart's own controls. Note this is *not* `.expand-btn`, which is the
#: "show more" affordance on a clamped long answer — a different control on a
#: different element that happens to read the same way in English.
EXPAND = '#turns [data-fig="expand"]'
SHOW_DATA = '#turns [data-fig="table"]'


def open_chat(page):
    """Open /chat on a conversation with nothing in it.

    Conversations are stored per user on the server, so a fresh browser context
    is not a fresh conversation: every test here signs in as the same person and
    /chat reopens whatever was last said. Without this, the second test to send
    a message sees the first one's turns, and `#turns .a-body` stops resolving
    to one element.
    """
    visit(page, '/chat')
    if page.locator('#turns .a-body').count():
        page.click('#new-chat')
        page.wait_for_selector('#chat-root.is-empty', timeout=5_000)
    return page


def _ask(page, question='What did I spend on groceries?'):
    """Type a question, send it, and wait for the answer to finish."""
    page.fill('#input', question)
    page.click('#send')
    # `.streaming` is on the answer body while tokens are arriving and comes off
    # when the stream ends, so its absence is the signal that the turn is done.
    page.wait_for_selector('#turns .a-body', timeout=15_000)
    page.wait_for_selector('#turns .a-body.streaming', state='detached', timeout=15_000)


def _script(page, ai, reply):
    """Queue the next answer, once nothing else is going to take it.

    Loading /chat fires `/api/copilot/brief`, which goes through the same
    adapter and pops the same queue. Scripting a reply before that lands hands
    the chart to the briefing card and leaves the conversation with a plain echo
    — which is exactly what happened while this file was being written, and it
    reads as "charts are broken" rather than "the test raced".
    """
    page.wait_for_load_state('networkidle')
    ai.scripted.clear()
    ai.scripted.append(reply)


# ── The page ────────────────────────────────────────────────────────────────

def test_an_empty_conversation_shows_the_hero(signed_in):
    page = signed_in
    visit(page, '/chat')
    assert page.locator('#chat-root.is-empty').count() == 1
    assert page.locator('#hero').is_visible()
    assert page.locator('#input').is_visible()


def test_chat_arrives_over_a_soft_navigation_too(signed_in):
    """chat.css and chat.js are linked from inside `{% block content %}`.

    That is deliberate — `spaNavigate()` swaps `<main>` and never touches
    `<head>`, so a stylesheet in the head would never arrive on a client-side
    navigation. The arrangement is load-bearing and completely invisible in the
    rendered HTML of a hard load, which makes this the one thing about chat that
    can only be checked by navigating to it the way the app does.
    """
    page = signed_in
    visit(page, '/transactions')
    page.click('a[href="/chat"]')
    page.wait_for_url('**/chat', timeout=10_000)
    page.wait_for_selector('#chat-root', timeout=10_000)

    # The stylesheet came with it: #chat-root is display:flex in chat.css and
    # has no styling at all without it.
    assert page.evaluate(
        "() => getComputedStyle(document.getElementById('chat-root')).display") == 'flex', \
        'chat.css did not arrive over a soft navigation'
    # And the script booted: #send starts disabled only because chat.js says so.
    assert page.locator('#send').is_disabled(), 'chat.js did not boot after a soft navigation'


# ── The composer ────────────────────────────────────────────────────────────

def test_the_send_button_tracks_whether_there_is_anything_to_send(signed_in):
    page = signed_in
    visit(page, '/chat')

    assert page.locator('#send').is_disabled(), 'send is live on an empty composer'
    page.fill('#input', 'Hello')
    assert page.locator('#send').is_enabled()
    page.fill('#input', '   ')
    assert page.locator('#send').is_disabled(), 'whitespace counts as a message'


def test_asking_a_question_produces_a_user_turn_and_an_answer(signed_in, ai):
    page = signed_in
    open_chat(page)
    _ask(page, 'What did I spend on groceries?')

    bubble = page.locator('#turns .u-bubble')
    assert bubble.count() == 1
    assert 'groceries' in bubble.inner_text().lower()

    answer = page.locator('#turns .a-body')
    assert answer.count() == 1
    assert answer.inner_text().strip(), 'the answer turn rendered empty'
    assert page.locator('#chat-root.is-empty').count() == 0, 'the hero survived the first turn'


def test_the_composer_empties_itself_after_sending(signed_in, ai):
    page = signed_in
    open_chat(page)
    _ask(page)
    assert page.input_value('#input') == '', 'the question was left in the composer'


# ── The sidebar ─────────────────────────────────────────────────────────────

def _sidebar_hidden(page):
    return page.evaluate(
        "() => document.getElementById('chat-root').classList.contains('side-hidden')")


def _on_screen(page, selector):
    """Whether an element occupies space inside the viewport.

    Not `is_visible()`. The sidebar closes by sliding out of the viewport rather
    than by being removed, so it keeps a box and Playwright rightly still calls
    it visible — it is visible, it is just somewhere you cannot see. What the
    closed state actually promises is that it is off the side of the screen.
    """
    box = page.locator(selector).bounding_box()
    return bool(box) and box['x'] + box['width'] > 1


def _wait_for_sidebar(page, open_):
    """Wait for the sidebar to finish arriving or leaving.

    On the geometry, not on the class. `side-hidden` goes on the instant the
    button is clicked and the panel then slides out over the following couple of
    hundred milliseconds, so a test that waited for the class and measured
    immediately would catch the sidebar mid-transition and read it as still
    on screen — which it is, briefly, and correctly.

    The two ends are not symmetrical, and the threshold is why. Desktop hides
    the sidebar by animating its width to zero; a phone slides it out on a
    transform with its width intact. "Gone" is therefore `right <= 1` either
    way, but "arrived" has to mean *most of the way in* — `right > 1` is true
    two pixels into a 260px slide, at which point the search field inside is
    still a zero-width box that Playwright rightly calls invisible.
    """
    page.wait_for_function(
        '''(want) => {
             const r = document.getElementById('side').getBoundingClientRect();
             return want ? (r.width > 100 && r.right > 100) : r.right <= 1;
           }''',
        arg=open_, timeout=5_000)


def test_the_sidebar_closes_and_reopens_on_a_desktop(signed_in):
    """It starts open here.

    The template ships `#chat-root` with `side-hidden` on it and chat.js takes
    it off during boot at desktop width — so the markup and the running page
    disagree about the initial state on purpose, and a test that trusted the
    markup would drive the toggle backwards.
    """
    page = signed_in
    visit(page, '/chat')
    assert not _sidebar_hidden(page), 'the sidebar did not open on a desktop'
    assert _on_screen(page, '#side'), 'the sidebar is open but not on screen'

    page.click('#side-close')
    _wait_for_sidebar(page, open_=False)
    assert not _on_screen(page, '#side'), 'closing the sidebar left it on screen'

    page.click('#side-open')
    _wait_for_sidebar(page, open_=True)
    assert _on_screen(page, '#side')
    assert page.locator('#conv-search').is_visible(), 'reopening left the sidebar empty'


def test_the_sidebar_is_a_drawer_on_a_phone(signed_in):
    """On a phone the sidebar overlays the conversation instead of displacing it.

    tests/test_chat_ux.py asserts the CSS says so. This asserts the browser
    agrees — which is a different claim, because the rule only applies if the
    media query matches and the selector wins.
    """
    page = signed_in
    page.set_viewport_size({'width': 375, 'height': 812})
    visit(page, '/chat', note=' at 375px')

    # Closed to begin with, which is the other half of the drawer idiom: a
    # phone gives the conversation the whole screen until asked otherwise.
    assert _sidebar_hidden(page), 'the sidebar starts open on a phone'
    thread_left = page.locator('#thread').bounding_box()['x']

    page.click('#side-open')
    _wait_for_sidebar(page, open_=True)

    # What makes it a drawer rather than a column is that it is out of flow and
    # lies *over* the conversation. Asserting the exact `position` value would
    # be asserting the implementation — absolute and fixed both overlay, and
    # which one is right depends on the containing block.
    position = page.evaluate("() => getComputedStyle(document.getElementById('side')).position")
    assert position in ('fixed', 'absolute'), \
        f'the sidebar is {position} on a phone, so it displaces the conversation'
    assert page.locator('#thread').bounding_box()['x'] == thread_left, \
        'opening the drawer pushed the conversation sideways instead of covering it'
    assert page.locator('#scrim').is_visible(), 'the drawer opened with no scrim behind it'

    # And it must not have pushed anything off the side of the screen.
    assert_no_horizontal_overflow(page, ' with the phone drawer open')


# ── The expanded chart ──────────────────────────────────────────────────────

def _require_charts(page):
    if not page.evaluate('() => !!window.Chart'):
        pytest.skip('Chart.js is loaded from a CDN and did not arrive')


def test_a_chart_in_an_answer_can_be_expanded_and_dismissed(signed_in, ai):
    """The expanded chart is a native `<dialog>` opened with `showModal()`.

    Wave 1c replaced a hand-rolled Tab trap with the platform's. That change is
    invisible in a screenshot and almost invisible in the source — the only way
    to tell the two apart is to ask the browser whether the element matches
    `:modal`, which is exactly what a real engine is for.
    """
    page = signed_in
    open_chat(page)
    _script(page, ai, CHART_REPLY)
    _ask(page, 'Show me July by category')
    _require_charts(page)

    expand = page.locator(EXPAND)
    expand.first.wait_for(state='visible', timeout=10_000)
    expand.first.click()

    dialog = page.locator('#fig-modal')
    dialog.wait_for(state='visible', timeout=5_000)
    assert dialog.evaluate('d => d.matches(":modal")'), \
        'the expanded chart is open but not modal — showModal() was not what opened it'
    assert page.evaluate(
        '() => document.getElementById("fig-modal").contains(document.activeElement)'), \
        'focus stayed outside the expanded chart'

    page.keyboard.press('Escape')
    dialog.wait_for(state='detached', timeout=5_000)
    assert page.locator('#fig-modal').count() == 0, \
        'closing the expanded chart left the dialog in the document'


def test_escape_closing_the_chart_does_not_also_stop_the_stream(signed_in, ai):
    """One keypress, one effect.

    `<dialog>` closes itself on Esc, so chat.js's own Esc handler still runs for
    the same keypress. If it did not return early, dismissing a chart would also
    abort whatever was streaming behind it. This is the regression that comment
    in chat.js is about, asserted from outside.
    """
    page = signed_in
    open_chat(page)
    _script(page, ai, CHART_REPLY)
    _ask(page, 'Show me July by category')
    _require_charts(page)

    answer_before = page.locator('#turns .a-body').inner_text()

    page.locator(EXPAND).first.wait_for(state='visible', timeout=10_000)
    page.locator(EXPAND).first.click()
    page.locator('#fig-modal').wait_for(state='visible', timeout=5_000)
    page.keyboard.press('Escape')
    page.locator('#fig-modal').wait_for(state='detached', timeout=5_000)

    assert page.locator('#turns .a-body').inner_text() == answer_before, \
        'dismissing the chart disturbed the answer behind it'


def test_a_chart_ships_the_numbers_behind_it(signed_in, ai):
    """A financial assistant that renders a picture of a number has to be able
    to show the number.

    The figures start collapsed to the canvas, so this exercises the "Show data"
    toggle: a `<canvas>` is opaque to a screen reader and to anyone who wants to
    check the arithmetic, and the table behind it is the answer to both.
    """
    page = signed_in
    open_chat(page)
    _script(page, ai, CHART_REPLY)
    _ask(page, 'Show me July by category')
    _require_charts(page)

    show_data = page.locator(SHOW_DATA).first
    show_data.wait_for(state='visible', timeout=10_000)
    assert show_data.get_attribute('aria-expanded') == 'false'
    show_data.click()
    assert show_data.get_attribute('aria-expanded') == 'true', \
        'the data toggle did not report itself expanded'

    text = page.locator('#turns figure').first.inner_text()
    for label in CHART_SPEC['labels']:
        assert label in text, f'{label!r} is in the chart but not readable as text'


# ── Arriving by soft navigation ─────────────────────────────────────────────

def test_the_composer_is_composer_sized_after_a_soft_navigation(signed_in):
    """Regression: navigating to /chat client-side rendered a ~42vh composer.

    spaNavigate() swaps <main> and re-executes the page's scripts, but the
    page stylesheet arrives as a <link> inside that same swap and loads
    asynchronously — chat.js's boot-time autosize measured the *unstyled*
    textarea and pinned what it saw as an inline height. A hard load never
    showed it, because the parser refuses to run a script before the
    stylesheets above it have loaded; spaNavigate now waits the same way.
    This drives the soft path a person actually takes: clicking "Ask Dough"
    in the nav.

    The stylesheet is delayed a beat through a route interception, because
    against a localhost server the CSS wins the race almost every time and
    the unfixed bug would pass here while failing on any real network.
    """
    page = signed_in
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.goto('/upload', wait_until='load')
    wait_for_layout(page)

    def delayed(route):
        time.sleep(0.5)
        route.continue_()
    page.route('**/static/css/chat.css*', delayed)

    page.click('#primary-nav a[href="/chat"]')   # SPA navigation, not a reload
    page.wait_for_selector('#composer', timeout=10_000)
    wait_for_layout(page)

    height = page.locator('#input').bounding_box()['height']
    assert height < 150, (
        f'the composer textarea is {height:.0f}px tall after a soft '
        f'navigation — autosize measured the page before chat.css arrived')

    # The state seen in the wild: an EMPTY composer pinned at autosize's
    # 42vh cap, which nothing re-measured until the next keystroke. autoGrow
    # now treats "empty" as "no height override at all", so any trigger —
    # here a window resize, the cheapest one a user produces — must heal a
    # poisoned height rather than trust it.
    # scrollHeight is also stubbed to lie, standing in for whatever the
    # engine reported in the broken state — an implementation that clears
    # ignores the lie; one that re-measures believes it and re-pins the cap.
    page.evaluate("""() => {
      const i = document.getElementById('input');
      i.style.height = '430px';
      Object.defineProperty(i, 'scrollHeight', { get: () => 800 });
    }""")
    page.set_viewport_size({'width': 1439, 'height': 900})
    wait_for_layout(page)
    healed = page.locator('#input').bounding_box()['height']
    assert healed < 150, (
        f'an empty composer stayed {healed:.0f}px tall through a resize — '
        f'autoGrow trusted a stale measurement instead of clearing it')
