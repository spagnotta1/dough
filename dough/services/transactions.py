"""Transaction querying, filtering and anomaly scoring.

Moved verbatim out of `create_app()`'s closures in Phase 3. See
`dough/services/README.md` for why, and for the dependency rules below.

Allowed:   models, SQLAlchemy, pandas, numpy, scikit-learn, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators
Exception: `sticky_filter` is REQUEST-BOUND -- it exists to read `request.args`
           and the Flask `session`, and is kept below its own separator so the
           rest of this module stays callable from the sync scheduler thread,
           where no request exists.
"""

from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sqlalchemy import and_, or_

# Only the request-bound section below uses these two. They are proxies, so
# importing them costs nothing and binds to whatever context is active at call
# time -- but see the separator: nothing above it may touch them.
from flask import request, session

from models import Transaction, db


def compute_anomaly_scores():
    """Recompute Isolation Forest anomaly scores for all transactions and persist."""
    transactions = Transaction.query.all()
    if len(transactions) < 10:
        return
    df = pd.DataFrame([{
        'id': t.id,
        'abs_amount': abs(float(t.amount)),
        'day_of_week': pd.to_datetime(t.date).dayofweek,
        'day_of_month': pd.to_datetime(t.date).day,
    } for t in transactions])
    X = df[['abs_amount', 'day_of_week', 'day_of_month']].replace([np.inf, -np.inf], np.nan).dropna()
    model = IsolationForest(contamination='auto', random_state=42)
    scores = model.fit_predict(X)
    for tid, score in zip(df['id'], scores):
        t = Transaction.query.get(tid)
        if t:
            t.anomaly_score = float(score)
    db.session.commit()


def build_transaction_query(account_filter, category_filter, start_date_str,
                            end_date_str, direction_filter, search_query):
    filters = []
    if account_filter and account_filter != 'both':
        filters.append(Transaction.account_name == account_filter)
    if category_filter:
        filters.append(Transaction.category == category_filter)
    if start_date_str:
        filters.append(Transaction.date >= datetime.strptime(start_date_str, '%Y-%m-%d'))
    if end_date_str:
        filters.append(Transaction.date <= datetime.strptime(end_date_str, '%Y-%m-%d'))
    if search_query:
        terms = []
        try:
            terms.append(Transaction.id == int(search_query))
        except ValueError:
            pass
        terms.append(Transaction.description.ilike(f'%{search_query}%'))
        filters.append(or_(*terms))
    if direction_filter == 'inbound':
        filters.append(Transaction.amount > 0)
    elif direction_filter == 'outgo':
        filters.append(Transaction.amount < 0)
    return Transaction.query.filter(and_(*filters)) if filters else Transaction.query


# ---------------------------------------------------------------------------
# REQUEST-BOUND
#
# Below this line the request context is a hard requirement. Nothing above it
# may call anything below it.
# ---------------------------------------------------------------------------

def sticky_filter(session_key, *arg_keys, default=None):
    """Resolve a filter value from the query string with session fallback.

    A key that is present but empty means the user explicitly cleared the
    filter (e.g. picked "All" in the form), so it must not fall back to
    the stale session value; only a fully absent key does.
    """
    for key in arg_keys or (session_key,):
        if key in request.args:
            return request.args.get(key) or None
    return session.get(session_key, default)
