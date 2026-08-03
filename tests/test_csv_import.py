"""Reading a CSV from any bank.

The importer used to hardcode Capital One's four column names —
``Transaction Date``, ``Transaction Description``, ``Transaction Amount``,
``Balance`` — and index them directly. Every other bank's export therefore
raised ``KeyError`` inside a per-row loop, which reached the user as a 500 page
with a trace id and nothing saying the file was the problem. The upload page
meanwhile invited them to "drop in your Capital One CSV exports", so the copy
was honest and the product was narrow.

Two shapes have to work, and the difference between them is not cosmetic:

  **One amount column.** The magnitude may be unsigned, so the direction is
  inferred from the description with the balance delta as a fallback. This is
  Capital One's checking export, and its behaviour must not move.

  **Debit and credit columns.** The direction is *stated*. Inferring it here
  would let a row sitting under Credit and described as "PAYMENT" be booked as
  money out, because that is what the vocabulary rules say about the word.

The rest is presentation the banks insist on putting in numeric cells: currency
symbols, thousands separators, parentheses for negatives, European decimal
commas, and empty cells meaning "not this column".
"""

import pytest

from dough.services.ledger import (CsvFormatError, _money, detect_columns,
                                   infer_signed_amount)


# ── Header mapping ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('headers, expected', [
    # Capital One checking — the shape that used to be the only one.
    (['Transaction Date', 'Transaction Description', 'Transaction Amount',
      'Balance'],
     {'date': 'Transaction Date', 'description': 'Transaction Description',
      'amount': 'Transaction Amount', 'balance': 'Balance'}),
    # Capital One credit card — debit/credit pair, no signed column.
    (['Transaction Date', 'Posted Date', 'Description', 'Debit', 'Credit'],
     {'date': 'Transaction Date', 'description': 'Description',
      'debit': 'Debit', 'credit': 'Credit'}),
    # Chase
    (['Posting Date', 'Description', 'Amount', 'Balance'],
     {'date': 'Posting Date', 'description': 'Description',
      'amount': 'Amount', 'balance': 'Balance'}),
    # Amex
    (['Date', 'Description', 'Amount'],
     {'date': 'Date', 'description': 'Description', 'amount': 'Amount'}),
    # A UK bank: money in / money out, and a payee rather than a description.
    (['Date', 'Payee', 'Money Out', 'Money In', 'Balance'],
     {'date': 'Date', 'description': 'Payee', 'debit': 'Money Out',
      'credit': 'Money In', 'balance': 'Balance'}),
    # Punctuation and case are not a different format.
    (['posted_date', 'MEMO', 'amount'],
     {'date': 'posted_date', 'description': 'MEMO', 'amount': 'amount'}),
])
def test_headers_map_onto_roles(headers, expected):
    cols = detect_columns(headers)
    for role, header in expected.items():
        assert cols[role] == header, f'{role} resolved to {cols[role]!r}'


def test_the_transaction_date_wins_over_the_posted_date():
    """An export with both should book on the date the user recognises from
    their statement, not the one the bank settled it on."""
    cols = detect_columns(['Posted Date', 'Transaction Date', 'Description',
                           'Amount'])
    assert cols['date'] == 'Transaction Date'


def test_a_signed_amount_column_suppresses_debit_and_credit():
    """A file carrying both shapes is not ambiguous — the signed column is the
    whole truth, and reading both would double every row."""
    cols = detect_columns(['Date', 'Description', 'Amount', 'Debit', 'Credit'])
    assert cols['amount'] == 'Amount'
    assert cols['debit'] is None and cols['credit'] is None


def test_balance_is_optional():
    """Most card exports have no running balance. Requiring one is what made
    `Balance` a KeyError for every bank that is not Capital One."""
    cols = detect_columns(['Date', 'Description', 'Amount'])
    assert cols['balance'] is None


@pytest.mark.parametrize('headers, missing_phrase', [
    (['Description', 'Amount'], 'date'),
    (['Date', 'Amount'], 'description'),
    (['Date', 'Description'], 'amount'),
])
def test_an_unreadable_file_says_what_is_missing_and_what_it_saw(
        headers, missing_phrase):
    with pytest.raises(CsvFormatError) as caught:
        detect_columns(headers)
    assert missing_phrase in str(caught.value)
    # The headers it did read are the actionable half of the message.
    for header in headers:
        assert header in str(caught.value)


# ── Numbers as banks actually write them ────────────────────────────────────

@pytest.mark.parametrize('cell, expected', [
    (42, 42.0),
    (42.5, 42.5),
    ('42.50', 42.5),
    ('$1,234.56', 1234.56),
    ('-$1,234.56', -1234.56),
    ('(45.00)', -45.0),          # accountants' negative
    ('1.234,56', 1234.56),       # European: dot thousands, comma decimal
    ('1234,56', 1234.56),        # European decimal comma, no thousands
    ('1,234', 1234.0),           # thousands comma, no decimals
    ('', 0.0),                   # empty debit cell on a credit row
    ('   ', 0.0),
    ('-', 0.0),
    (None, 0.0),
])
def test_money_parses_what_the_bank_wrote(cell, expected):
    assert _money(cell) == pytest.approx(expected)


def test_money_never_raises_on_a_junk_cell():
    """A per-row loop that raises aborts an import most of the way through,
    leaving a half-loaded ledger the user has to unpick."""
    assert _money('n/a') == 0.0


# ── Direction ───────────────────────────────────────────────────────────────

def test_the_signed_column_path_still_infers_direction():
    """Unchanged behaviour for the export this importer was written for."""
    assert infer_signed_amount('PURCHASE AT SHOP', 25.0, None, None) == -25.0
    assert infer_signed_amount('Deposit', 25.0, None, None) == 25.0


def test_credit_card_is_tested_before_credit():
    """Order in the branch chain is load-bearing: a credit-card payment is an
    outgo, not income caught by the generic 'credit' branch."""
    assert infer_signed_amount('CREDIT CARD PMT', 100.0, None, None) == -100.0


# ── End to end ──────────────────────────────────────────────────────────────
#
# The mapper being right does not mean the loop reads through it: the loop used
# to index the frame by literal name, and a mapper it ignored would pass every
# test above while importing nothing.

def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text.strip() + '\n', encoding='utf-8')
    return str(path)


class _Rules:
    """The categorizer, stubbed. `import_csv` takes it as a parameter rather
    than reaching for configuration — see dough/services/README.md rule 2."""

    def get_category(self, description):
        return 'Groceries' if 'MARKET' in description.upper() else 'Other'


def test_a_capital_one_export_imports_unchanged(app, tmp_path):
    from models import Transaction
    from dough.services import ledger

    path = _write(tmp_path, 'capitalone.csv', """
Transaction Date,Transaction Description,Transaction Amount,Balance
2026-03-02,PURCHASE AT MARKET,84.21,915.79
2026-03-03,Deposit from payroll,1200.00,2115.79
""")
    result = ledger.import_csv([path], 'Checking', batch_id='b1',
                               rules_engine=_Rules())
    assert result.added == 2

    # float() on the way out: the column is Numeric, so these come back as
    # Decimal and `Decimal('-84.21') == -84.21` is False.
    rows = {t.description: t for t in Transaction.query.all()}
    assert float(rows['PURCHASE AT MARKET'].amount) == -84.21, 'an outgo'
    assert float(rows['Deposit from payroll'].amount) == 1200.00
    assert rows['PURCHASE AT MARKET'].category == 'Groceries'


def test_a_debit_credit_export_takes_the_direction_it_is_given(app, tmp_path):
    """The row described as PAYMENT sits under Credit, so it is money in — the
    vocabulary rules would have called it an outgo."""
    from models import Transaction
    from dough.services import ledger

    path = _write(tmp_path, 'card.csv', """
Transaction Date,Posted Date,Description,Debit,Credit
2026-03-04,2026-03-05,COFFEE SHOP,6.45,
2026-03-06,2026-03-07,PAYMENT THANK YOU,,250.00
""")
    result = ledger.import_csv([path], 'Visa', batch_id='b2',
                               rules_engine=_Rules())
    assert result.added == 2

    rows = {t.description: float(t.amount) for t in Transaction.query.all()}
    assert rows['COFFEE SHOP'] == -6.45
    assert rows['PAYMENT THANK YOU'] == 250.00


def test_a_uk_style_export_with_presentation_in_the_cells(app, tmp_path):
    from models import Transaction
    from dough.services import ledger

    path = _write(tmp_path, 'uk.csv', """
Date,Payee,Money Out,Money In,Balance
2026-03-08,TESCO MARKET,"1,234.56",,"2,000.00"
2026-03-09,SALARY,,"£2,500.00","4,500.00"
""")
    result = ledger.import_csv([path], 'Current', batch_id='b3',
                               rules_engine=_Rules())
    assert result.added == 2

    rows = {t.description: float(t.amount) for t in Transaction.query.all()}
    assert rows['TESCO MARKET'] == -1234.56
    assert rows['SALARY'] == 2500.00


def test_an_unreadable_file_names_itself(app, tmp_path):
    """The route turns this into a sentence, so it has to carry the filename —
    a user who dropped in four files needs to know which one to replace."""
    from dough.services import ledger

    path = _write(tmp_path, 'holdings.csv', """
Ticker,Shares,Price
VTI,10,250.00
""")
    with pytest.raises(CsvFormatError) as caught:
        ledger.import_csv([path], 'Checking', batch_id='b4',
                          rules_engine=_Rules())
    assert caught.value.filename == 'holdings.csv'
    assert 'Ticker' in str(caught.value)


# ── Through the upload route ────────────────────────────────────────────────

def _upload(client, name, body):
    import io
    return client.post(
        '/upload',
        data={'account_name': 'Checking',
              'files[]': (io.BytesIO(body.strip().encode()), name)},
        content_type='multipart/form-data', follow_redirects=True)


def test_uploading_another_banks_export_works(client):
    """The whole point, exercised at the surface a user touches."""
    from models import Transaction

    response = _upload(client, 'chase.csv', """
Posting Date,Description,Amount,Balance
2026-03-02,STARBUCKS 123,-6.45,1000.00
2026-03-03,PAYROLL DEPOSIT,2500.00,3500.00
""")
    assert response.status_code == 200
    assert b'I added 2 new transactions' in response.data

    assert {t.description for t in Transaction.query.all()} == {
        'STARBUCKS 123', 'PAYROLL DEPOSIT'}


def test_an_unreadable_upload_explains_itself_instead_of_500ing(client):
    """The regression this replaces: a KeyError inside the row loop, rendered
    as a 500 and a trace id, with nothing saying the file was the problem."""
    response = _upload(client, 'holdings.csv', """
Ticker,Shares,Price
VTI,10,250.00
""")
    assert response.status_code == 200, 'a wrong-format CSV must not 500'
    body = response.data.decode()
    assert 'holdings.csv' in body, 'the message must name the file'
    assert 'Ticker' in body, 'and list the headers it actually found'
