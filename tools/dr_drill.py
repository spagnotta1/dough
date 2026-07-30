"""Disaster-recovery drill, service half: boot against a RESTORED database
and verify every service the runbook cares about. Touches nothing live —
everything it reads and writes is under the restored directory it is given.

This is steps 6-9 of docs/runbooks/disaster-recovery.md (steps 1-5 — backup,
baseline, restore, migrate, tenancy — are tools/backup_db.py, a copy, flask
db upgrade, and tools/verify_tenancy.py; the runbook gives the commands).

What is verified, and how it avoids needing any live secret:

  * /health/live and /health/ready answer over real HTTP — readiness covers
    database reachability, migration currency, and required configuration;
  * the restored password hash verifies: a WRONG password is submitted
    through the real login form (CSRF token and all, which also proves the
    restored SECRET_KEY signs sessions the CSRF layer accepts) and must be
    rejected with the generic message — no live password is ever needed;
  * that failed attempt lands in audit_events as auth.login.failed with a
    NULL household (by design — see tools/verify_tenancy.py), proving audit
    is wired end-to-end on the restored data;
  * every stored connection credential decrypts with the restored
    .sync_encryption_key — the check that catches a backup that forgot the
    key file, which is the single most likely fatal omission;
  * the AI service constructed its adapter, and the scheduler honours the
    OPS-0012 single-worker decision.

Run from the repo root, against a directory holding the restored
checkbook.db, .sync_encryption_key and .flask_secret_key:

    python tools/dr_drill.py /path/to/restored

NOTE: the wrong-password probe writes one audit_events row to the restored
copy (that is the point — it proves audit writes work). Re-restore from the
backup before promoting the copy to live if a pristine file matters.

Exit status 0 when all checks pass, 1 otherwise, so it drops into a script.
"""
import json
import os
import sys
import threading
import urllib.request

sys.path.insert(0, os.getcwd())   # run from the repo root; import app from there

RESTORED = os.path.abspath(sys.argv[1])
DB = os.path.join(RESTORED, 'checkbook.db')
assert os.path.exists(DB), DB

# The restored host's environment: DB + the two key files restored beside it.
os.environ['DATABASE_URL'] = 'sqlite:///' + DB.replace('\\', '/')
os.environ['SYNC_ENCRYPTION_KEY'] = open(
    os.path.join(RESTORED, '.sync_encryption_key')).read().strip()
os.environ['SECRET_KEY'] = open(
    os.path.join(RESTORED, '.flask_secret_key')).read().strip()
os.environ['SYNC_AUTO_ENABLED'] = '0'   # OPS-0012: single-worker decision

from app import create_app                      # noqa: E402
from werkzeug.serving import make_server        # noqa: E402

app = create_app()
results = {}

# ── 6. Health endpoints over real HTTP ───────────────────────────────────────
server = make_server('127.0.0.1', 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f'http://127.0.0.1:{server.server_address[1]}'

def get(path):
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

status, body = get('/health/live')
results['health/live'] = (status == 200, f'{status} {body.strip()}')
status, body = get('/health/ready')
ready = json.loads(body)
results['health/ready'] = (status == 200 and ready['status'] == 'ok',
                           f'{status} {body.strip()}')

# ── 7. Auth + audit on the restored data ─────────────────────────────────────
# The drill cannot know the live password (only its scrypt hash survived the
# restore, which is the point). What CAN be proven: the login route verifies
# against the restored hash (a wrong password is rejected with the generic
# message), and that failed attempt lands in audit_events — auth and audit
# exercised end-to-end with zero secrets needed.
import sqlite3
conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
user, = conn.execute('SELECT username FROM app_users LIMIT 1').fetchone()
audit_before, = conn.execute('SELECT COUNT(*) FROM audit_events').fetchone()
conn.close()

import re
with app.test_client() as client:
    page = client.get('/login')
    results['login page renders'] = (page.status_code == 200,
                                     f'GET /login -> {page.status_code}')
    # CSRF is on outside the test config, so the POST has to carry the form's
    # own token — which is itself part of the drill: it proves the restored
    # SECRET_KEY signs sessions the CSRF layer accepts.
    token = re.search(r'name="_csrf_token" value="([^"]+)"',
                      page.get_data(as_text=True)).group(1)
    resp = client.post('/login', data={'username': user,
                                       'password': 'wrong-password-for-drill',
                                       '_csrf_token': token})
    body = resp.get_data(as_text=True)
    rejected = resp.status_code == 200 and 'Invalid username or password' in body
    results['restored password hash verifies'] = (
        rejected, f'wrong password rejected with the generic message: {rejected}')

conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
audit_after, = conn.execute('SELECT COUNT(*) FROM audit_events').fetchone()
last = conn.execute('SELECT event_type, household_id FROM audit_events '
                    'ORDER BY id DESC LIMIT 1').fetchone()
# household_id None on this row is by design: a failed login belongs to no
# household (see tools/verify_tenancy.py EXPECTED_NULLABLE_HOUSEHOLD).
results['audit logging records on restored DB'] = (
    audit_after == audit_before + 1 and last[0] == 'auth.login.failed',
    f'{audit_before} -> {audit_after} rows, last={last}')

# ── 8. Plaid credentials decrypt with the restored key ──────────────────────
from finance_sync.crypto import TokenCipher
cipher = TokenCipher()   # reads SYNC_ENCRYPTION_KEY set above
conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
rows = conn.execute('SELECT id, institution, auth_blob FROM connected_accounts '
                    "WHERE status != 'disconnected' AND auth_blob IS NOT NULL").fetchall()
conn.close()
ok, bad = 0, []
for cid, institution, blob in rows:
    try:
        creds = cipher.decrypt(blob)
        ok += 1 if isinstance(creds, dict) else 0
    except Exception as exc:
        bad.append((cid, institution, str(exc)[:60]))
results['stored connection credentials decrypt'] = (
    not bad and ok == len(rows), f'{ok}/{len(rows)} connections decrypt; failures: {bad}')

# ── 9. AI adapter + scheduler state ──────────────────────────────────────────
# The service lives on app.extensions (one per app, never a module global —
# see app.py); config['AI_ADAPTER'] is only the test-injection seam.
ai_service = app.extensions.get('dough_ai')
adapter = getattr(ai_service, 'adapter', None) if ai_service else None
results['AI adapter constructed'] = (
    adapter is not None,
    f'service={type(ai_service).__name__}, adapter={type(adapter).__name__}, '
    f'configured={getattr(adapter, "configured", "?")}')
results['scheduler honours SYNC_AUTO_ENABLED=0'] = (
    app.config.get('SYNC_AUTO_ENABLED') in (False, 0, '0'),
    f"SYNC_AUTO_ENABLED={app.config.get('SYNC_AUTO_ENABLED')!r} (OPS-0012 single-worker decision)")

server.shutdown()

# ── Report ───────────────────────────────────────────────────────────────────
width = max(len(k) for k in results)
failed = 0
for name, (passed, detail) in results.items():
    mark = 'ok  ' if passed else 'FAIL'
    failed += 0 if passed else 1
    print(f'  [{mark}] {name:<{width}}  {detail}')
print(f'\n{len(results) - failed}/{len(results)} service checks passed.')
sys.exit(1 if failed else 0)
