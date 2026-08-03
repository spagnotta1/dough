"""Structural tests for `dough/services/` — the Phase 3 extraction.

These do not re-test the extracted logic; the existing suites already cover
behaviour, and the extraction was a pure move, so those passing *is* the
behavioural evidence. What is untested by them is the structure itself: that
`app.py` no longer defines the helpers, that the services stay importable
without a Flask app, that the dependency rules in
`dough/services/README.md` actually hold, and that the one deliberate
duplication (`finance_sync` building its own `CategoryRules`) survives.

Every check here is AST- or import-based rather than a text search, so a rule
named in a comment or docstring cannot make a test pass or fail by accident.
"""

import ast
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_DIR = os.path.join(REPO_ROOT, 'dough', 'services')
BLUEPRINTS_DIR = os.path.join(REPO_ROOT, 'dough', 'blueprints')
API_V1_DIR = os.path.join(REPO_ROOT, 'dough', 'api', 'v1')   # [Phase 10]

# Modules in dough/services/ and the functions each is expected to own.
SERVICE_FUNCTIONS = {
    'transactions': ('compute_anomaly_scores', 'build_transaction_query',
                     'sticky_filter'),
    'recurring_service': ('dismissed_recurring_keys', 'detect_recurring_summary',
                          'detect_recurring_full'),
    'networth': ('compute_net_worth', 'portfolio_snapshot', 'monthly_outgo',
                 'snapshot_history', 'wealth_snapshot'),
    'finance_context': ('build_finance_context', 'copilot_context',
                        'wealth_context', 'months_ago'),
    'categorization': ('get_category_rules', 'reset_category_rules'),
    # Phase 5. The only service that exports a class rather than functions --
    # `household_scope` is the module-level def, TenantScopedTTLCache is the
    # thing callers use.
    'cache': ('household_scope',),
    # Phase 6. The membership rules -- who is in a household, and the one that
    # matters: a household always keeps an owner.
    'membership': ('issue_invite', 'revoke_invite', 'accept_invite',
                   'find_redeemable_invite', 'set_member_role',
                   'remove_member', 'household_members', 'household_invites',
                   'hash_invite_token'),
    # Phase 8. `record` appends, `recent` is the only read path, `redact` is
    # exported because the values it has to strip are the ones no call site
    # produces today -- so it is tested directly rather than through record().
    'audit': ('record', 'recent', 'redact'),
    # ---------------------------------------------------------------------
    # Phase 10. The write-side logic the web blueprints used to hold inline,
    # extracted so `/api/v1` performs the same operations rather than a second
    # implementation of them. Each of these has two callers, which is the point:
    # a single caller would not have justified the move.
    # ---------------------------------------------------------------------
    # The ledger's write side. `dough/services/transactions.py` above is the
    # read side; the two are kept apart because the read side is what the sync
    # scheduler and the anomaly scorer use, and it has no business importing
    # pandas-driven CSV handling.
    # `detect_columns` is here because the importer accepts any bank's CSV and
    # the header mapping is the part worth testing directly against a header
    # row. `CsvFormatError` is exported too — the upload route catches it — but
    # this table is functions only, and it is a class.
    'ledger': ('serialize', 'infer_signed_amount', 'import_csv', 'undo_import',
               'create_transaction', 'update_transaction', 'delete_transaction',
               'bulk_update_category', 'bulk_delete', 'export_rows',
               'detect_columns'),
    'budgets': ('serialize', 'list_budgets', 'upsert_budget', 'delete_budget',
                'month_window', 'spend_by_category', 'status'),
    'holdings': ('list_holdings', 'create_holding', 'update_holding',
                 'delete_holding'),
    'accounts': ('synced_accounts', 'connections', 'last_sync_at',
                 'manual_balances', 'set_manual_balance',
                 'ledger_account_names', 'overview'),
    # The credential lifecycle for `/api/v1`. The only module that reads
    # `api_tokens`, which is the isolation guarantee for a table the ORM tenant
    # backstop deliberately does not cover.
    'api_tokens': ('hash_token', 'normalize_scopes', 'household_tokens',
                   'authenticate', 'issue', 'revoke', 'touch'),
    # ---------------------------------------------------------------------
    # Phase 10.5. The identity lifecycle and the two abstractions it needs.
    # ---------------------------------------------------------------------
    # Creating an account, proving an address, replacing a password, and
    # invalidating every credential the account holds. Three surfaces call these
    # -- `/register`, `/settings` and the reset flow -- which is the duplication
    # this module exists to prevent: three copies of the password rules is how
    # the third one ends up accepting six characters.
    'identity': ('normalize_email', 'validate_email', 'validate_username',
                 'validate_password', 'find_by_email', 'find_by_username',
                 'register_account', 'set_password', 'set_email',
                 'revoke_all_credentials', 'hash_token', 'issue_token',
                 'redeem', 'mark_email_verified'),
    # Where a verification or reset link goes. `build_backend` is the switch
    # `MAIL_BACKEND` actually operates; `current_email` is the per-application
    # lookup, mirroring `current_ai`.
    'email': ('build_backend', 'current_email'),
    # SEC-0018's seam. `policy_for` and `build_backend` are module-level for the
    # same reason everything else here is: a test asserts on the declared
    # policies without building an application, which is what makes a loosened
    # limit fail a test rather than surface in a bill.
    'ratelimit': ('policy_for', 'build_backend', 'current_limiter'),
    # Phase 10.6. Verified snapshots, and the loop that takes them unattended.
    # `backup`, `verify` and `prune` are the mechanics `tools/backup_db.py` used
    # to hold; they are module-level functions taking paths, so a test can
    # snapshot a temporary database without an application. `backup_target` is
    # the one with a decision in it -- where snapshots go, given a config -- and
    # is tested directly because getting it wrong writes backups into a
    # container image that the next deploy erases, which is a failure nothing
    # reports until a restore.
    'backup': ('backup', 'verify', 'prune', 'backup_target', 'describe',
               'contents', 'init_backup_scheduler', 'get_backup_scheduler',
               'install'),
    # Phase 10.7. The erasure and portability half of the privacy policy.
    # `deletion_preview` is public and not an internal detail on purpose: the
    # confirmation page is rendered from it, so what somebody is warned about is
    # produced by the same code that does the removing.
    'account_lifecycle': ('export_account', 'delete_account',
                          'deletion_preview'),
    # ---------------------------------------------------------------------
    # Phase 11. The analytics layer the copilot reasons over.
    # ---------------------------------------------------------------------
    # The aggregation primitives. Every other module in this block reads its
    # numbers from here rather than issuing its own SQL, which is what makes
    # "does the trend agree with the dashboard?" a question with one answer.
    # `period_summary` is the hot path -- one GROUP BY, asserted as one query in
    # tests/test_analytics.py.
    'analytics': ('resolve_window', 'preceding_window', 'custom_window',
                  'month_keys_between', 'base_query', 'period_summary',
                  'category_totals', 'merchant_totals', 'largest_purchases',
                  'monthly_series', 'monthly_category_series', 'coverage',
                  'pct_change'),
    # Feature 2. Two windows in, structured findings out -- deliberately no
    # prose, so the only thing the model can get wrong is the wording.
    'periods': ('compare', 'compare_kind', 'compare_ranges'),
    # Feature 3. A slope across months rather than a delta across two, which is
    # the difference between a trend and an expensive Tuesday. `unit_cost_trend`
    # is separate because "prices rose" and "you shopped more" need different
    # advice and are one indistinguishable number until they are split.
    'trends': ('category_trends', 'merchant_trends', 'unit_cost_trend'),
    # Movement between the household's own accounts, identified by pairing a
    # debit against an equal credit in another account rather than by reading
    # the description. Every income and spending total already excluded the
    # `Transfer` category and nothing was writing it, so a checking-to-savings
    # sweep was counted as income.
    'transfers': ('find_transfer_pairs', 'net_out_transfers',
                  'looks_like_transfer'),
    # Feature 6. Named causes rather than a bare flag -- the ledger's
    # `anomaly_score` column already answers "is this odd?" and cannot answer
    # "why?", which is the only part a user can act on. Median-and-MAD
    # throughout, because household spending has a long right tail and one
    # annual premium hides every later outlier from a standard deviation.
    'anomalies': ('detect', 'summary', 'open_flagged', 'large_purchases',
                  'duplicates', 'category_spikes', 'missing_income',
                  'bill_increases'),
    # Feature 4. Gathers the measurable inputs and hands them to
    # `dashboard_intel.health_score` -- deliberately not a second scorer, so the
    # dashboard and the copilot cannot show two numbers both called "financial
    # health". The methodology table lives in this module's docstring.
    'health': ('score', 'cash_flow_stability', 'debt_burden', 'improvements'),
    # Feature 7. Parse, then query, then answer over the result -- so the model
    # chooses the question and never does the arithmetic. `parse` is public
    # because a caller that disagrees with the reading needs to see it.
    'finsearch': ('search', 'parse'),
    # Feature 11. The only module here that decides what is worth saying when
    # nobody asked, which is why it is a scoring function and a hard cap rather
    # than a list of rules. An empty result is the common case and is correct.
    'proactive': ('insights', 'digest'),
    # Feature 12. Conclusions plus their figures, not the rows behind them.
    # About a sixth of `finance_context`'s detailed context on a household with
    # real history, and -- the property that matters -- it does not grow as the
    # ledger does.
    'ai_context': ('build', 'estimated_tokens'),
    # ---------------------------------------------------------------------
    # Phase 11A.1. Category rules, per household.
    # ---------------------------------------------------------------------
    # They used to live in one `category_rules.json` at the repo root, shared by
    # the whole installation -- so the second household to sign in read the
    # first's rules, which name the merchants that household actually pays. Not
    # a leak through a missing filter: the rules were never tenanted.
    # `remove_category` is new, and its absence was a user-visible bug: the
    # Rules page's delete button called `remove_rule(category, None)`, matched
    # nothing, and reported success.
    # Feature 9. Two shapes -- a one-off cost out of savings, a recurring
    # commitment out of surplus -- and a verdict that is deliberately a band
    # with a reason, never a yes. Medians rather than means throughout: one tax
    # refund moves a mean enough to change the answer, in the direction that
    # encourages spending.
    'affordability': ('assess', 'capacity'),
    # ---------------------------------------------------------------------
    # Phase 11B. Feature 10 -- the only part of Phase 11 that needs a table.
    # ---------------------------------------------------------------------
    # A goal is a statement of intent, not an opinion about transactions: no
    # amount of history reveals that somebody is saving for a wedding rather
    # than a deposit, and inferring it would be the fabrication the rest of the
    # phase exists to prevent. `momentum` is the recent rate rather than the
    # lifetime average, because an abandoned goal has a healthy average and no
    # momentum, and `projection` returns None rather than a distant date when
    # there is no rate to project from.
    'goals': ('list_goals', 'describe', 'momentum', 'projection', 'summary',
              'create_goal', 'update_goal', 'contribute', 'delete_goal',
              'contributions'),
    'rules_service': ('all_rules', 'as_engine', 'categories', 'clear_all',
                      'add_rule', 'remove_rule', 'remove_category',
                      'rename_category', 'reorder', 'replace_all',
                      'rule_counts'),
}

# The closure names Phase 3 removed from app.py. Any of these reappearing as a
# nested def means a helper was re-inlined instead of extended in place.
EXTRACTED_CLOSURES = (
    '_compute_anomaly_scores', '_sticky_filter', '_build_transaction_query',
    '_dismissed_recurring_keys', '_detect_recurring_summary',
    '_detect_recurring_full', '_compute_net_worth', '_portfolio_snapshot',
    '_monthly_outgo', '_snapshot_history', '_wealth_snapshot',
    '_build_finance_context', '_copilot_context', '_wealth_context',
)

# Imports no module under dough/services/ may make. `app` is the cycle the
# extraction removed; the Flask response helpers are the route's job; anthropic
# belongs to dough/ai/ (Phase 4).
FORBIDDEN_MODULES = ('app', 'anthropic')
FORBIDDEN_FROM_FLASK = ('render_template', 'url_for', 'redirect', 'flash',
                        'jsonify', 'Blueprint', 'Flask')


def _service_modules():
    return sorted(name[:-3] for name in os.listdir(SERVICES_DIR)
                  if name.endswith('.py') and name != '__init__.py')


def _parse(path):
    with open(path, encoding='utf-8') as handle:
        return ast.parse(handle.read())


def _module_path(name):
    return os.path.join(SERVICES_DIR, name + '.py')


def _toplevel_functions(tree):
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


# ---------------------------------------------------------------------------
# The move happened, and happened completely
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('module,expected', sorted(SERVICE_FUNCTIONS.items()))
def test_service_defines_its_functions_at_module_level(module, expected):
    """Each service owns its functions as plain module-level defs.

    Module level specifically -- a def nested inside another function would be
    unreachable from a test or the scheduler, which is the whole problem Phase 3
    set out to fix.
    """
    defined = _toplevel_functions(_parse(_module_path(module)))
    missing = [name for name in expected if name not in defined]
    assert missing == [], f'{module}.py is missing {missing}'


def test_every_service_module_is_covered_by_this_test_file():
    """A new service must be added to SERVICE_FUNCTIONS, not quietly skipped."""
    assert _service_modules() == sorted(SERVICE_FUNCTIONS)


def test_app_no_longer_defines_the_extracted_closures():
    """None of the moved helpers may exist as a def in app.py, at any nesting."""
    tree = _parse(os.path.join(REPO_ROOT, 'app.py'))
    found = sorted({node.name for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in EXTRACTED_CLOSURES})
    assert found == [], f'still defined in app.py: {found}'


def test_create_app_is_meaningfully_smaller():
    """create_app() must not still contain the extracted bodies.

    A line count is a crude measure, but the failure it catches is real: an
    extraction that adds a module while leaving the original in place. The
    threshold is deliberately loose -- it is a regression guard, not a target.

    History, because the number moving is the thing worth watching:

        3,161  before Phase 3
        2,600  after the services came out
        2,900  raised in Phase 6 for six new membership routes
          800  after Phase 7 moved every route to dough/blueprints/
          810  Phase 10.6, for two lines: an import and `install_backups(app)`

    Raised twice and lowered once. The Phase 6 raise was honest -- the growth was
    a feature and the *rules* went to a service -- but a threshold that only ever
    goes up stops being a guard, which is why Phase 7 was the next phase. Now
    that no route lives here, app.py should only change when the wiring changes,
    so the ceiling stays tight on purpose: a route added to this file rather
    than to a blueprint will hit it.

    The 10.6 raise is the smallest kind there is and is still worth justifying,
    because "it was only two lines" is how the Phase 3 number got to 3,161.
    Backups needed starting from the factory, the file was two lines under the
    ceiling, and the alternative was to shrink the addition until it fit -- which
    means writing worse wiring to satisfy a proxy for wiring being large. The
    scheduler's own reasoning lives in `dough/services/backup.py`; what landed
    here is a call and an import, which is what this file is *for*.

    Ten lines of headroom rather than the two actually needed, deliberately: a
    ceiling set to exactly the current size fails on the next honest line and
    teaches whoever hits it to raise it without reading any of this.
    """
    with open(os.path.join(REPO_ROOT, 'app.py'), encoding='utf-8') as handle:
        total = sum(1 for _ in handle)
    assert total < 810, (
        f'app.py is {total} lines. Since Phase 7 it holds the factory and the '
        'request hooks only -- a route belongs in dough/blueprints/.')


# ---------------------------------------------------------------------------
# dough/blueprints/ — the Phase 7 extraction
# ---------------------------------------------------------------------------

def _blueprint_modules():
    return sorted(name[:-3] for name in os.listdir(BLUEPRINTS_DIR)
                  if name.endswith('.py') and name != '__init__.py')


@pytest.mark.parametrize('module', _blueprint_modules())
def test_blueprint_does_not_import_app(module):
    """The cycle Phase 7 must not reintroduce.

    `app` imports `dough.blueprints`, so any blueprint importing `app` back is a
    circular import. It would not necessarily fail loudly -- Python tolerates
    plenty of cycles -- which is exactly why it is worth an assertion rather
    than a convention. Anything a route needs from the application is on
    `current_app`.
    """
    path = os.path.join(BLUEPRINTS_DIR, module + '.py')
    offenders = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.split('.')[0] == 'app']
        elif isinstance(node, ast.ImportFrom):
            if (node.module or '').split('.')[0] == 'app':
                offenders.append(node.module)
    assert offenders == [], f'{module}.py imports app: {offenders}'


@pytest.mark.parametrize('module', _blueprint_modules())
def test_blueprint_declares_exactly_one_blueprint_named_bp(module):
    """One module, one blueprint, always called `bp`.

    `dough/blueprints/__init__.py` registers them by that name. A module that
    named its blueprint something else would import cleanly, register nothing,
    and serve 404s for every route it defines -- with no error anywhere.
    """
    tree = _parse(os.path.join(BLUEPRINTS_DIR, module + '.py'))
    names = [t.id for node in tree.body if isinstance(node, ast.Assign)
             for t in node.targets if isinstance(t, ast.Name)
             and isinstance(node.value, ast.Call)
             and getattr(node.value.func, 'id', None) == 'Blueprint']
    assert names == ['bp'], f'{module}.py declares {names}, expected exactly [bp]'


def test_every_blueprint_module_is_registered():
    """A blueprint file nobody registers is a set of routes nobody serves.

    Reads the registration from a real application rather than from
    `blueprints.ALWAYS`, so a module added to the package and forgotten in the
    registrar is caught -- which is the mistake this is actually guarding.
    """
    from app import create_app
    app = create_app({'TESTING': True, 'AUTH_ENABLED': True,
                      'SQLALCHEMY_DATABASE_URI': 'sqlite://'})
    # `finance_sync` is registered by app.py but lives in its own package -- it
    # predates this one and owns the adapter pipeline as well as its routes.
    #
    # Phase 10 adds `dough/api/v1/`, held to the same rule and for a stronger
    # reason: a v1 module nobody registers is not merely dead code, it is a
    # documented public endpoint that answers 404. The naming convention
    # (`api_v1_<module>`) is asserted here too -- it is what keeps these from
    # colliding with the HTML blueprints, several of which also have an `index`.
    expected = (_blueprint_modules()
                + ['finance_sync']
                + [f'api_v1_{name}' for name in _api_v1_modules()])
    assert sorted(app.blueprints) == sorted(expected)


def _api_v1_modules():
    return sorted(name[:-3] for name in os.listdir(API_V1_DIR)
                  if name.endswith('.py') and name != '__init__.py')


@pytest.mark.parametrize('module', _api_v1_modules())
def test_api_resource_does_not_import_app_or_another_blueprint(module):
    """The same cycle rule the HTML blueprints follow, for the same reason.

    Extended here to cover `dough.blueprints` as well as `app`: an API resource
    importing a web blueprint would couple the two surfaces at exactly the seam
    this phase exists to keep clean -- and it is the tempting shortcut, since
    the logic a resource wants is often visible in the blueprint that used to
    hold it. It belongs in `dough/services/`, which is where both may reach.
    """
    path = os.path.join(API_V1_DIR, module + '.py')
    offenders = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.split('.')[0] == 'app'
                          or a.name.startswith('dough.blueprints')]
        elif isinstance(node, ast.ImportFrom):
            imported = node.module or ''
            if (imported.split('.')[0] == 'app'
                    or imported.startswith('dough.blueprints')):
                offenders.append(imported)
    assert offenders == [], f'{module}.py imports {offenders}'


@pytest.mark.parametrize('module', _api_v1_modules())
def test_api_resource_holds_no_business_logic(module):
    """The architectural claim of Phase 10, checked mechanically.

    A resource module reads the request, calls a service, and shapes a response.
    The specific thing forbidden here is a *write* issued from the route: any
    `db.session.add/delete/commit` means the module is doing domain work that
    its counterpart on the web side does not share, and the two surfaces have
    started to diverge.

    Reads are not forbidden. Several resources legitimately run a query to
    serialize a list, and a rule against that would push trivial passthroughs
    into `dough/services/` without making anything safer.

    `chat.py` is the one exception and it is named rather than pattern-matched:
    persisting a message mid-stream has to happen inside the generator, after
    `teardown_request` has released the request's tenant scope, so it cannot be
    delegated to a service that would be called in the wrong frame. See its
    module docstring.
    """
    if module == 'chat':
        pytest.skip('documented exception -- see dough/api/v1/chat.py')

    tree = _parse(os.path.join(API_V1_DIR, module + '.py'))
    writes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in (
                'add', 'delete', 'commit'):
            continue
        value = func.value
        if isinstance(value, ast.Attribute) and value.attr == 'session':
            writes.append(f'db.session.{func.attr}')
    assert writes == [], (
        f'{module}.py writes to the database directly: {writes}. '
        f'Move it into dough/services/ so the web UI shares it.')


def test_no_route_is_defined_in_app_py():
    """Every route lives in a blueprint. app.py wires, it does not serve.

    `sync_bp` is registered here, not defined here, so it is unaffected.
    """
    tree = _parse(os.path.join(REPO_ROOT, 'app.py'))
    decorators = [ast.unparse(d) for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) for d in node.decorator_list]
    routes = [d for d in decorators if d.startswith('app.route')]
    assert routes == [], f'app.py still defines routes: {routes}'


# ---------------------------------------------------------------------------
# The dependency rules in README.md hold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('module', _service_modules())
def test_service_imports_nothing_forbidden(module):
    """No service may import `app`, `anthropic`, or Flask's response helpers."""
    offenders = []
    for node in ast.walk(_parse(_module_path(module))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] in FORBIDDEN_MODULES:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or '').split('.')[0]
            if root in FORBIDDEN_MODULES:
                offenders.append(node.module)
            if root == 'flask':
                offenders += [a.name for a in node.names
                              if a.name in FORBIDDEN_FROM_FLASK]
    assert offenders == [], f'{module}.py imports {offenders}'


@pytest.mark.parametrize('module', _service_modules())
def test_service_declares_its_dependency_rules(module):
    """Every service docstring carries the Allowed/Must not block.

    Cheap to satisfy and easy to forget, which is exactly why it is asserted:
    the block is the decision record for what the module is allowed to touch.
    """
    doc = ast.get_docstring(_parse(_module_path(module))) or ''
    assert 'Allowed:' in doc and 'Must not:' in doc, (
        f'{module}.py must document its allowed and forbidden dependencies')


def test_services_import_without_a_flask_app():
    """The package must be importable in a bare interpreter.

    Run in a subprocess so it cannot be satisfied by conftest having already
    built an app. This is what makes the services reachable from a CLI command
    or a migration, and it fails loudly if anything gains an import-time
    dependency on `current_app`.
    """
    code = (
        'import importlib\n'
        'for m in %r:\n'
        '    importlib.import_module("dough.services." + m)\n'
        'print("ok")\n' % (_service_modules(),)
    )
    result = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'ok' in result.stdout


# ---------------------------------------------------------------------------
# The deliberate duplication survives
# ---------------------------------------------------------------------------

# [Phase 11A.1] The three tests below replace three that asserted the *old*
# arrangement, in which `dough/services/categorization.py` held one
# `CategoryRules` per process backed by a shared `category_rules.json`.
#
# They were:
#   test_sync_repository_builds_its_own_category_rules   (asserted the import)
#   test_get_category_rules_is_cached_and_resettable     (asserted the cache)
#   test_sync_repository_instance_is_not_the_cached_engine
#
# Rewriting a passing test to accommodate new code is normally the wrong move,
# and AGENTS.md says so. This is the exception it names, and the reason is that
# the invariant those tests protected turned out to be a security bug: a
# process-wide rule cache over a process-wide rule *file* meant the second
# household in an installation read and overwrote the first household's rules,
# which name the merchants that household actually pays.
#
# The *intent* of the originals is preserved in full -- the sync must not pin
# categorization to whatever was loaded at boot -- and each is now the stronger
# statement of it, asserted behaviourally rather than by reading an import.

def test_sync_repository_does_not_pin_the_rules_it_categorizes_with():
    """A rule edited in the Rules page must reach the very next sync.

    The original reason `SyncRepository` built its own engine, and still true.
    Asserted by editing a rule and re-categorizing rather than by checking which
    name the module imports, so any implementation that resolves rules per call
    satisfies it.
    """
    from dough.services import rules_service
    from finance_sync.repository import SyncRepository

    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False})
    from dough.tenancy import tenant_scope
    from models import db

    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            repo = SyncRepository()
            assert repo._categorize('ACME WIDGETS') == 'Uncategorized'

            rules_service.add_rule('Widgets', 'ACME WIDGETS')
            # Same repository instance, no restart: it must see the new rule.
            assert repo._categorize('ACME WIDGETS') == 'Widgets'
        db.session.remove()
    scheduler_module._scheduler = None


def test_category_rules_are_not_cached_across_households(app):
    """The successor to `test_get_category_rules_is_cached_and_resettable`.

    That test asserted `get_category_rules()` returned the *same object* twice.
    It must now return a fresh engine each time, because a cached engine is a
    cached household -- see `dough/services/categorization.py`.
    """
    from dough.services import categorization

    first = categorization.get_category_rules()
    assert categorization.get_category_rules() is not first


def test_the_rules_accessor_reads_the_current_household(tmp_path):
    """The bug this whole change exists to close, asserted directly.

    Two households, different rules, one process. Before Phase 11A.1 both read
    the same `category_rules.json` and this could not have passed.
    """
    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'rules.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False})

    from dough.services import rules_service
    from dough.services.categorization import get_category_rules
    from dough.tenancy import tenant_scope, unscoped
    from models import Household, db

    with application.app_context():
        with unscoped():
            a = Household(name='A', plaid_user_id='rules-a')
            b = Household(name='B', plaid_user_id='rules-b')
            db.session.add_all([a, b])
            db.session.commit()
            a_id, b_id = a.id, b.id

        with tenant_scope(a_id):
            rules_service.replace_all({'Gym': ['PLANET FITNESS']})
        with tenant_scope(b_id):
            rules_service.replace_all({'Coffee': ['STARBUCKS']})

        with tenant_scope(a_id):
            engine = get_category_rules()
            assert engine.get_category('PLANET FITNESS 123') == 'Gym'
            assert engine.get_category('STARBUCKS 8891') == 'Uncategorized'

        with tenant_scope(b_id):
            engine = get_category_rules()
            assert engine.get_category('STARBUCKS 8891') == 'Coffee'
            assert engine.get_category('PLANET FITNESS 123') == 'Uncategorized'

        db.session.remove()
    scheduler_module._scheduler = None


# ---------------------------------------------------------------------------
# The app-level attachments the tests depend on still point somewhere real
# ---------------------------------------------------------------------------

def test_app_exposes_the_service_functions_it_used_to_close_over(app):
    """`app.build_finance_context` / `app.wealth_snapshot` are the test seams.

    tests/test_routes.py and tests/test_investments_page.py call these. After
    the extraction they must resolve to the service functions themselves, not
    to a surviving closure.
    """
    from dough.services.finance_context import build_finance_context
    from dough.services.networth import wealth_snapshot

    assert app.build_finance_context is build_finance_context
    assert app.wealth_snapshot is wealth_snapshot
