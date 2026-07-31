"""The specification and the application must describe the same API.  [Phase 10]

`docs/api/openapi.yaml` is hand-written, deliberately -- see the comment at the
top of that file. A generated spec always agrees with the code and therefore can
never catch the code changing, which is the one thing a spec is for.

This file supplies the guarantee a generator would have given: every
(path, method) the application serves under `/api/v1` is documented, and every
documented one is served. A route added without a spec entry fails here, as does
a spec entry for a route that was renamed or removed.

What it deliberately does *not* assert is response bodies against their schemas.
That would need a validator and a fixture per endpoint, and it would drift into
re-testing what `tests/test_api_v1.py` already asserts behaviourally. The
contract this file protects is the surface: which endpoints exist, what they are
called, and which methods they answer.
"""

import os

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(REPO_ROOT, 'docs', 'api', 'openapi.yaml')

API_PREFIX = '/api/v1'

#: Methods a spec entry may declare. Anything else in a path item is a key like
#: `parameters` or `summary`, not an operation.
HTTP_METHODS = frozenset({'get', 'put', 'post', 'delete', 'patch', 'head',
                          'options', 'trace'})

yaml = pytest.importorskip(
    'yaml', reason='PyYAML is needed to read the OpenAPI specification')


@pytest.fixture(scope='module')
def spec():
    with open(SPEC_PATH, encoding='utf-8') as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope='module')
def served():
    """Every (path, METHOD) the application answers under /api/v1.

    Built with AUTH_ENABLED on, because the v1 surface is registered
    unconditionally and this must be checked against the fullest configuration
    rather than the test default.

    Werkzeug's `<int:id>` converters are rewritten to OpenAPI's `{id}` so the
    two vocabularies can be compared at all.
    """
    scheduler_module._scheduler = None
    app = create_app(test_config={
        'TESTING': True, 'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'SYNC_AUTO_ENABLED': False, 'SYNC_SYNCHRONOUS': True,
    })
    out = set()
    for rule in app.url_map.iter_rules():
        path = str(rule.rule)
        if not path.startswith(API_PREFIX):
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            out.add((_to_openapi_path(path), method.upper()))
    scheduler_module._scheduler = None
    return out


def _to_openapi_path(rule):
    """`/api/v1/transactions/<int:transaction_id>` -> `/transactions/{transaction_id}`.

    The server prefix is stripped because the spec declares it under `servers`,
    which is where a base path belongs -- repeating it in every path would make
    mounting the API elsewhere a change to sixty lines.
    """
    trimmed = rule[len(API_PREFIX):] or '/'
    out = []
    for segment in trimmed.split('/'):
        if segment.startswith('<') and segment.endswith('>'):
            name = segment[1:-1].split(':')[-1]
            out.append('{' + name + '}')
        else:
            out.append(segment)
    return '/'.join(out)


def _documented(spec):
    return {(path, method.upper())
            for path, item in (spec.get('paths') or {}).items()
            for method in item
            if method.lower() in HTTP_METHODS}


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------

def test_every_served_endpoint_is_documented(spec, served):
    """An undocumented endpoint is a public contract nobody reviewed."""
    missing = sorted(served - _documented(spec))
    assert not missing, (
        'These /api/v1 endpoints are served but absent from '
        'docs/api/openapi.yaml:\n'
        + '\n'.join(f'  {method} {path}' for path, method in missing)
        + '\nAdd them in the same commit that added the route.')


def test_every_documented_endpoint_is_served(spec, served):
    """The other direction, and it catches the more embarrassing failure.

    A client written against a documented endpoint that does not exist gets a
    404 for something we told it to call. That is worse than an undocumented
    endpoint, which merely goes unused.
    """
    extra = sorted(_documented(spec) - served)
    assert not extra, (
        'These endpoints are documented but not served:\n'
        + '\n'.join(f'  {method} {path}' for path, method in extra)
        + '\nThey were probably renamed or removed without updating the spec.')


def test_the_spec_covers_a_plausible_number_of_endpoints(spec):
    """Guards against the two tests above passing vacuously.

    A spec whose `paths` failed to parse would make both comparisons empty-set
    against empty-set and pass while checking nothing.
    """
    documented = _documented(spec)
    assert len(documented) >= 40, f'only {len(documented)} operations parsed'


# ---------------------------------------------------------------------------
# The parts of the contract a client actually depends on
# ---------------------------------------------------------------------------

def test_the_declared_version_matches_the_envelope(spec):
    from dough.api.envelope import API_VERSION

    assert spec['servers'][0]['url'] == f'/api/{API_VERSION}'


def test_every_error_code_the_application_emits_is_documented(spec):
    """`error.code` is what a client switches on, so the enum has to be complete.

    Derived from `ErrorCode`'s attributes rather than a hand-kept list, so a
    code added to that class without a spec entry fails here — which is the
    moment it becomes part of the contract.
    """
    from dough.api.errors import ErrorCode

    emitted = {value for name, value in vars(ErrorCode).items()
               if name.isupper() and isinstance(value, str)}
    documented = set(spec['components']['schemas']['ErrorCode']['enum'])

    assert emitted - documented == set(), (
        f'undocumented error codes: {sorted(emitted - documented)}')
    assert documented - emitted == set(), (
        f'documented but never emitted: {sorted(documented - emitted)}')


def test_the_documented_page_limits_match_the_implementation(spec):
    """A client that trusts the spec's maximum and gets clamped is a client
    that silently loses rows it believes it asked for.
    """
    from dough.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

    page_size = spec['components']['parameters']['PageSize']['schema']
    assert page_size['default'] == DEFAULT_PAGE_SIZE
    assert page_size['maximum'] == MAX_PAGE_SIZE


def test_the_documented_scopes_match_the_implementation(spec):
    from dough.services.api_tokens import VALID_SCOPES

    assert spec['components']['schemas']['Scope']['enum'] == list(VALID_SCOPES)


def test_the_documented_token_states_match_the_implementation(spec):
    """A state the spec does not list is a value a generated client cannot hold.

    Added when `stale` was — the state a token reaches when a password change
    invalidates the generation it was issued under.  [Phase 10.5] The spec had
    to be edited by hand for that, which is precisely the edit somebody makes
    the model change and forgets.
    """
    from models import ApiToken

    documented = set(spec['components']['schemas']['ApiToken']
                     ['properties']['state']['enum'])
    assert documented == set(ApiToken.STATES)


def test_the_spec_declares_no_credential_in_a_query_string(spec):
    """The API deliberately reads no token from a query string.

    Query strings are written to access logs by every proxy, kept in browser
    history, and sent in `Referer`. Documenting one would invite a client to
    send it, and the endpoint would then be the only thing standing between that
    client and a credential in somebody's log aggregator.
    """
    for name, scheme in spec['components']['securitySchemes'].items():
        assert scheme.get('in') != 'query', f'{name} is declared in a query string'


def test_the_only_public_endpoint_is_the_login(spec):
    """`security: []` on an operation opts it out of authentication.

    Exactly one may do so, and it is the endpoint credentials come from.
    Anything else with an empty security list is an unauthenticated route that
    reached the spec without reaching `tests/test_route_guard.py`'s reviewed
    allowlist.
    """
    public = [(path, method)
              for path, item in spec['paths'].items()
              for method, operation in item.items()
              if method.lower() in HTTP_METHODS
              and operation.get('security') == []]
    assert public == [('/auth/login', 'post')]
