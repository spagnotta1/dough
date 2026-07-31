"""`/api/v1/settings` — what this installation can do, and what it is called.

A client needs to know things about the server that are not any household's
data: which AI models it will accept, what the page-size ceiling is, whether
registration is open. Before this a client would have to hardcode them, and a
hardcoded limit is one that silently disagrees with the server the day either
changes.

## What may and may not appear here

This endpoint is read by every client on startup, so it is a disclosure surface
and is treated as one. It carries **capabilities and limits**, never values:
whether an AI key is configured, never the key; which mail backend is named,
never its password; the invite TTL, never the invite. The rule is the one
`dough/blueprints/health.py` follows for the same reason — a settings endpoint
that echoed configuration would be the leak it exists to avoid.

Nothing here is writable. Changing an installation's configuration is an
operator action against `.env` and a restart, not something a phone does over
HTTP, and an endpoint that accepted writes would be a remote configuration
channel guarded by one bearer token.
"""

from __future__ import annotations

from flask import Blueprint, current_app

from dough.api.envelope import API_VERSION, ok
from dough.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from dough.ai import catalog
from dough.ai.service import current_ai
from dough.services import api_tokens
from dough.services.categorization import get_category_rules

bp = Blueprint('api_v1_settings', __name__)


@bp.route('/settings', methods=['GET'])
def settings():
    """Capabilities, limits and vocabularies, for a client's startup call."""
    ai = current_ai()
    config = current_app.config

    return ok({
        'api': {
            'version': API_VERSION,
            # A list rather than a single value, so a client can check whether
            # the version it speaks is still served *before* it is turned off.
            # This is what makes deprecating v1 a thing that can be announced in
            # band rather than discovered as a wall of 404s.
            'supported_versions': [API_VERSION],
            'default_page_size': DEFAULT_PAGE_SIZE,
            'max_page_size': MAX_PAGE_SIZE,
            'token_scopes': list(api_tokens.VALID_SCOPES),
        },
        'ai': {
            # Whether a key is configured. Never the key, never its prefix,
            # never its length.
            'available': bool(ai.is_available),
            'models': catalog.all_models(),
            'default_model': catalog.resolve().provider_id,
        },
        'features': {
            'registration_open': bool(config['ALLOW_REGISTRATION']),
            'auth_enabled': bool(config['AUTH_ENABLED']),
            'sync_enabled': bool(config.get('SYNC_AUTO_ENABLED', False)),
            'invite_ttl_hours': config['INVITE_TTL_HOURS'],
        },
        'categories': {
            # The rule engine's vocabulary, so a client's category picker offers
            # the same set the importer will assign rather than a copy that
            # drifts. Names only -- the rules themselves are how this household
            # categorizes and belong to `/rules`, which v1 does not yet expose.
            'known': sorted(_known_categories()),
        },
    })


def _known_categories():
    """Every category the rules engine can assign.

    Best-effort: the engine's internals are `rules.py`'s business and have
    changed shape before. A failure here must not take down the endpoint every
    client calls on startup, so it degrades to an empty list and the client
    falls back to `/transactions/categories`, which reads what is actually in
    use.
    """
    try:
        engine = get_category_rules()
        categories = getattr(engine, 'categories', None)
        if callable(categories):
            categories = categories()
        return set(categories or [])
    except Exception:
        current_app.logger.exception('could not enumerate rule categories')
        return set()
