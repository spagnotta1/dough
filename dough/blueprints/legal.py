"""The privacy policy and the terms of service.
[Phase 10.7 — a prerequisite for Plaid production access and for real users]

Two `@public` routes rendering two templates. There is no logic here and there
should never be any: the moment a legal page renders differently for different
readers, "what did it say when they agreed" stops having one answer.

## Why these are templates and not a CMS, a Markdown file, or a database row

Because the version that matters is the one in git. A policy is a promise with a
date on it, and the question asked after an incident is "what did this say on
the day they signed up" — which `git log templates/privacy.html` answers exactly
and a database row does not answer at all.

## What the content is, and what it is not

**It is a factual description of what this application actually does**, written
from the code: which third parties receive data (Plaid, Anthropic, Postmark),
what is stored, what is encrypted, how long things are kept, and what a person
can do about it. Every claim in it is one that can be checked against a module
in this repository, and where the code changes the page has to change with it.

**It is not legal advice and has not been reviewed by a lawyer.** The
placeholders it carries — the operating entity, the governing jurisdiction, the
contact address — are deliberately left as configuration rather than invented,
because a made-up entity name in a privacy policy is worse than an obvious gap:
the gap gets filled before launch and the invention gets shipped.

`LEGAL_ENTITY`, `LEGAL_CONTACT_EMAIL` and `LEGAL_JURISDICTION` are read from
configuration for that reason, and `docs/runbooks/launch-checklist.md` makes
setting them a gate on opening registration.
"""

from flask import Blueprint, current_app, render_template

from dough.auth import public

bp = Blueprint('legal', __name__)

#: The date the wording last changed materially. Updated by hand, deliberately:
#: a timestamp derived from the file's mtime would move on every deploy and
#: tell a reader that the terms had changed when they had not.
LAST_UPDATED = '2026-08-01'


def _context():
    """The placeholders both pages share.

    Absent values render as a visible `[…]` marker rather than as an empty
    string. An unset entity name that renders as nothing produces a sentence
    that reads as finished and names nobody — which is exactly the failure this
    is trying to avoid, arriving silently.
    """
    config = current_app.config
    return {
        'last_updated': LAST_UPDATED,
        'entity': config.get('LEGAL_ENTITY') or '[OPERATING ENTITY — set LEGAL_ENTITY]',
        'contact': (config.get('LEGAL_CONTACT_EMAIL')
                    or '[CONTACT ADDRESS — set LEGAL_CONTACT_EMAIL]'),
        'jurisdiction': (config.get('LEGAL_JURISDICTION')
                         or '[JURISDICTION — set LEGAL_JURISDICTION]'),
        'ai_enabled': bool(config.get('ANTHROPIC_API_KEY')),
        # Whether `/settings` exists to be linked to. `dough/blueprints/` only
        # registers `settings` when `AUTH_ENABLED`, so on an installation with
        # authentication off, `url_for('settings.index')` raises and takes the
        # whole page down -- a privacy policy 500ing because it mentioned the
        # export link. The pages describe the rights either way; only the
        # hyperlink is conditional.
        'accounts_enabled': bool(config.get('AUTH_ENABLED')),
    }


@bp.route('/privacy')
@public
def privacy():
    return render_template('privacy.html', **_context())


@bp.route('/terms')
@public
def terms():
    return render_template('terms.html', **_context())
