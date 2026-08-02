"""Re-apply the current rules to every transaction, one household at a time.

A maintenance script, run by hand after a rule change that should reach rows
that were imported before it existed.

[Phase 11A.1] Rules are per household now, so this iterates households and
enters each one's scope rather than building a single global engine. The old
version built one `CategoryRules()` from the shared JSON file and applied it to
`Transaction.query.all()` — which, once tenancy existed, meant recategorising
every household's transactions with whichever rule set happened to be on disk.
"""

from app import create_app
from dough.services.rules_service import as_engine
from dough.tenancy import tenant_scope, unscoped
from models import Household, Transaction, db

app = create_app()

with app.app_context():
    # Reading the tenant list is one of the sanctioned uses of `unscoped()`.
    with unscoped():
        household_ids = [h.id for h in Household.query.order_by(Household.id)]

    total = 0
    for household_id in household_ids:
        with tenant_scope(household_id):
            rules = as_engine()
            updated = 0
            for transaction in Transaction.query.all():
                category = rules.get_category(transaction.description)
                if transaction.category != category:
                    print(f'  [{household_id}] {transaction.id}: '
                          f'{transaction.description!r} '
                          f'{transaction.category!r} -> {category!r}')
                    transaction.category = category
                    updated += 1
            db.session.commit()
            print(f'Household {household_id}: {updated} transaction(s) updated.')
            total += updated

    print(f'Re-categorization complete. {total} transaction(s) updated '
          f'across {len(household_ids)} household(s).')
