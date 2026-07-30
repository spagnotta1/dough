"""The seams Phase 9 Wave 1b cut into the chat page.

chat.html was 3,154 lines: a 1,160-line ``<style>`` block, 86 lines of markup
and a 1,897-line ``<script>``. Splitting it into two static assets and four
partials was a pure structural change — but it introduced three failure modes
that did not exist while everything lived in one file, and none of them are
visible from any single file afterwards:

1. **The markup and the code no longer sit next to each other.** ``chat.js``
   looks up twenty elements by id. Renaming one in a partial now breaks the
   page silently at runtime instead of obviously at review time.

2. **A static asset is never rendered.** Jinja interpolated the model
   catalogue straight into the script. A file under ``static/`` is served
   byte-for-byte, so a surviving ``{{ … }}`` there is shipped to the browser
   as literal text — the page would boot with no models and no default.

3. **SPA navigation swaps ``<main>`` only.** The stylesheet and the script
   have to be linked from inside the content block or a soft navigation to
   ``/chat`` arrives unstyled and dead, while a hard refresh looks fine.

Design-system adoption is Wave 1c; these tests deliberately say nothing about
appearance, so they keep holding through it.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'templates'
PARTIALS = TEMPLATES / 'partials' / 'chat'
CHAT_JS = ROOT / 'static' / 'js' / 'chat.js'
CHAT_CSS = ROOT / 'static' / 'css' / 'chat.css'

PARTIAL_NAMES = ('_sidebar.html', '_topbar.html', '_thread.html', '_composer.html')


@pytest.fixture(scope='module')
def shell():
    return (TEMPLATES / 'chat.html').read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def chat_js():
    return CHAT_JS.read_text(encoding='utf-8')


@pytest.fixture()
def page(client):
    """The chat page as a browser receives it."""
    response = client.get('/chat')
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


# ── The decomposition itself ─────────────────────────────────────────────

def test_chat_html_is_a_shell(shell):
    """The point of the wave. If this file grows back past a few dozen lines,
    something is being written inline again instead of into an asset."""
    assert '<style' not in shell, 'CSS is creeping back into chat.html'
    assert len(shell.splitlines()) < 80, (
        f'chat.html is {len(shell.splitlines())} lines; it is meant to be a '
        f'shell of includes'
    )


@pytest.mark.parametrize('name', PARTIAL_NAMES)
def test_each_partial_exists_and_is_included(name, shell):
    path = PARTIALS / name
    assert path.exists(), f'{name} is missing'
    assert f'partials/chat/{name}' in shell, f'{name} is orphaned — nothing includes it'
    body = path.read_text(encoding='utf-8')
    assert '{% extends' not in body, f'{name} is a fragment, not a page'
    assert '{% block' not in body, f'{name} must not declare blocks'


def test_the_page_still_renders_end_to_end(page):
    """Four includes and two asset links, assembled by the server."""
    assert 'id="chat-root"' in page
    assert 'css/chat.css' in page
    assert 'js/chat.js' in page


# ── 1. The ids are a contract between files that never see each other ────

def test_every_id_chat_js_looks_up_is_rendered(chat_js, page):
    """Derived from the source rather than hardcoded, so the list cannot
    drift: whatever chat.js asks the DOM for, the DOM must have."""
    wanted = sorted(set(re.findall(r"getElementById\('([^']+)'\)", chat_js)))
    assert len(wanted) >= 15, f'only found {len(wanted)} lookups; did the regex rot?'
    missing = [i for i in wanted if f'id="{i}"' not in page]
    assert not missing, (
        f'chat.js looks these up but no partial renders them: {missing}'
    )


def test_the_boot_guard_and_teardown_hook_survive(chat_js):
    """An external script inside <main> is re-executed on every soft
    navigation. Without the guard, a second boot binds a second set of
    listeners to the live DOM; without the teardown hook, an in-flight stream
    keeps writing into a thread that has already been replaced."""
    assert 'root.dataset.booted' in chat_js, 'the re-entry guard is gone'
    assert 'window.__spaBeforeLeave' in chat_js, (
        'nothing aborts the stream when the user navigates away'
    )


# ── 2. A static asset is never rendered by Jinja ─────────────────────────

@pytest.mark.parametrize('asset', [CHAT_JS, CHAT_CSS])
def test_no_jinja_survives_in_an_extracted_asset(asset):
    """This is the one way the extraction could have failed quietly: Flask
    serves static files as-is, so ``{{ ai_models|tojson }}`` left behind here
    reaches the browser as six literal characters and a syntax error."""
    # Comments are inert, and both headers describe the Jinja block the asset
    # is linked from — scanning them would match the explanation, not a bug.
    body = re.sub(r'/\*.*?\*/', '', asset.read_text(encoding='utf-8'), flags=re.S)
    body = re.sub(r'(?m)^\s*//.*$', '', body)
    leftovers = re.findall(r'\{\{.*?\}\}|\{%.*?%\}', body)
    assert not leftovers, (
        f'{asset.name} still contains Jinja: {leftovers[:3]} — a file under '
        f'static/ is never rendered'
    )


def test_the_model_catalogue_arrives_as_data_not_code(page):
    """It moved from an interpolation inside the script to a JSON island.

    The type matters twice over: spaNavigate() only re-executes scripts whose
    type is a JavaScript type, so anything else is left intact — and a bare
    <script> would have the browser try to run the JSON.
    """
    island = re.search(
        r'<script type="application/json" id="chat-config">(.*?)</script>',
        page, re.S)
    assert island, 'the #chat-config data island is gone'
    config = json.loads(island.group(1))
    assert config['models'], 'the catalogue rendered empty'
    assert config['default_model'], 'no default model'
    assert config['default_model'] in [m['id'] for m in config['models']], \
        'the default is not one of the offered models'


def test_the_config_island_precedes_the_script_that_reads_it(page):
    """chat.js calls readConfig() during boot, so the island has to already
    be in the document — on a soft navigation both arrive in one innerHTML
    write, but on a hard load the script runs the moment it is parsed."""
    assert page.index('id="chat-config"') < page.index('js/chat.js'), \
        'chat.js is linked before the config it reads'


def test_chat_js_reads_the_island_rather_than_hardcoding_models(chat_js):
    assert "getElementById('chat-config')" in chat_js
    assert 'claude-' not in chat_js, (
        'a model id is hardcoded in chat.js; the catalogue lives in '
        'dough/ai/catalog.py and reaches the page through the context processor'
    )


# ── 3. The SPA constraint ────────────────────────────────────────────────

@pytest.mark.parametrize('asset', ['css/chat.css', 'js/chat.js'])
def test_assets_are_linked_from_inside_the_content_block(asset, shell):
    """<head> is never swapped, so an asset referenced from there arrives on
    a hard refresh and never on a client-side navigation — a bug that hides
    from anyone who tests by reloading."""
    content_at = shell.index('{% block content %}')
    assert shell.index(asset) > content_at, (
        f'{asset} is linked outside the content block; SPA navigation to '
        f'/chat would not load it'
    )
