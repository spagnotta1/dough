"""Dump and compare SQLite schemas, normalised for diffing.

Two databases that should be identical -- one reached by running the migration
chain against an existing database, one reached by `create_all` on an empty file
-- will differ in ways that do not matter (declaration order, autoindex names,
whitespace inside a CREATE TABLE). Comparing `.schema` output directly drowns
the real differences in those.

This reports structure only: tables, columns with type/nullability/default,
indexes with their columns and uniqueness, and foreign keys. Everything is
sorted, so a diff shows semantic drift and nothing else.

Usage:
    python tools/schema_report.py a.db                 # print a report
    python tools/schema_report.py a.db b.db            # diff two databases
    python tools/schema_report.py a.db b.db --json     # machine-readable diff
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

# SQLite renders the same declared type in several ways depending on how the
# table was created (ALTER TABLE ADD COLUMN preserves the literal text, while
# CREATE TABLE from SQLAlchemy normalises it). None of these differences affect
# behaviour, because SQLite applies type affinity rather than strict typing.
_TYPE_ALIASES = {
    'INT': 'INTEGER',
    'BOOL': 'BOOLEAN',
    'DATETIME': 'DATETIME',
    'TEXT': 'TEXT',
    'VARCHAR': 'VARCHAR',
}


def _norm_type(declared: str) -> str:
    t = (declared or '').strip().upper().replace(' ', '')
    return _TYPE_ALIASES.get(t, t)


def _norm_default(value):
    """Normalise a column default to a comparable form.

    SQLite records the literal text of the DEFAULT clause, so `'0'`, `0` and
    `"0"` are all the same default written three ways.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1]
    return text


def read_schema(path: str) -> dict:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

        out = {}
        for table in tables:
            columns = {}
            for row in conn.execute(f'PRAGMA table_info("{table}")'):
                _cid, name, decl_type, notnull, default, pk = row
                columns[name] = {
                    'type': _norm_type(decl_type),
                    'nullable': not notnull,
                    'default': _norm_default(default),
                    'primary_key': bool(pk),
                }

            indexes = {}
            for row in conn.execute(f'PRAGMA index_list("{table}")'):
                name, unique, origin = row[1], bool(row[2]), row[3]
                cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
                indexes[name] = {
                    'unique': unique,
                    'columns': cols,
                    # 'c' = CREATE INDEX, 'u' = UNIQUE constraint, 'pk' = primary key.
                    # The distinction matters: an 'u' autoindex lives inside the
                    # CREATE TABLE and cannot be dropped without rebuilding.
                    'origin': origin,
                }

            fks = []
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                fks.append({'column': row[3], 'references': f'{row[2]}.{row[4]}'})

            out[table] = {
                'columns': columns,
                'indexes': indexes,
                'foreign_keys': sorted(fks, key=lambda f: (f['column'], f['references'])),
            }
        return out
    finally:
        conn.close()


def _unique_index_signatures(table_schema: dict) -> set:
    """Uniqueness constraints as (sorted columns), ignoring index NAMES.

    A UNIQUE column constraint produces `sqlite_autoindex_market_prices_1`
    while an explicit Index() produces `uq_market_price`. Same guarantee,
    different name -- so compare by what they enforce.
    """
    return {
        tuple(sorted(idx['columns']))
        for idx in table_schema['indexes'].values()
        if idx['unique']
    }


def diff(left: dict, right: dict, left_name='left', right_name='right') -> list[str]:
    problems = []

    for table in sorted(set(left) - set(right)):
        problems.append(f'table {table}: only in {left_name}')
    for table in sorted(set(right) - set(left)):
        problems.append(f'table {table}: only in {right_name}')

    for table in sorted(set(left) & set(right)):
        lt, rt = left[table], right[table]

        for col in sorted(set(lt['columns']) - set(rt['columns'])):
            problems.append(f'{table}.{col}: column only in {left_name}')
        for col in sorted(set(rt['columns']) - set(lt['columns'])):
            problems.append(f'{table}.{col}: column only in {right_name}')

        for col in sorted(set(lt['columns']) & set(rt['columns'])):
            lc, rc = lt['columns'][col], rt['columns'][col]
            for attr in ('type', 'nullable', 'default', 'primary_key'):
                if lc[attr] != rc[attr]:
                    problems.append(
                        f'{table}.{col}.{attr}: {left_name}={lc[attr]!r} '
                        f'{right_name}={rc[attr]!r}')

        # Compare uniqueness by what it enforces, then named indexes by name.
        lu, ru = _unique_index_signatures(lt), _unique_index_signatures(rt)
        for sig in sorted(lu - ru):
            problems.append(f'{table}: UNIQUE{list(sig)} only in {left_name}')
        for sig in sorted(ru - lu):
            problems.append(f'{table}: UNIQUE{list(sig)} only in {right_name}')

        l_named = {n: i for n, i in lt['indexes'].items() if i['origin'] == 'c'}
        r_named = {n: i for n, i in rt['indexes'].items() if i['origin'] == 'c'}
        for name in sorted(set(l_named) - set(r_named)):
            problems.append(f'{table}: index {name} only in {left_name}')
        for name in sorted(set(r_named) - set(l_named)):
            problems.append(f'{table}: index {name} only in {right_name}')
        for name in sorted(set(l_named) & set(r_named)):
            if l_named[name]['columns'] != r_named[name]['columns']:
                problems.append(
                    f'{table}: index {name} columns '
                    f'{left_name}={l_named[name]["columns"]} '
                    f'{right_name}={r_named[name]["columns"]}')

        lf = {(f['column'], f['references']) for f in lt['foreign_keys']}
        rf = {(f['column'], f['references']) for f in rt['foreign_keys']}
        for col, ref in sorted(lf - rf):
            problems.append(f'{table}.{col}: FK -> {ref} only in {left_name}')
        for col, ref in sorted(rf - lf):
            problems.append(f'{table}.{col}: FK -> {ref} only in {right_name}')

    return problems


def render(schema: dict) -> str:
    lines = []
    for table in sorted(schema):
        t = schema[table]
        lines.append(f'{table}')
        for col in sorted(t['columns']):
            c = t['columns'][col]
            bits = [c['type']]
            if c['primary_key']:
                bits.append('PK')
            bits.append('NULL' if c['nullable'] else 'NOT NULL')
            if c['default'] is not None:
                bits.append(f'DEFAULT {c["default"]}')
            lines.append(f'    {col:<24} {" ".join(bits)}')
        for name in sorted(t['indexes']):
            i = t['indexes'][name]
            kind = 'UNIQUE' if i['unique'] else 'INDEX '
            lines.append(f'    [{kind}] {name} ({", ".join(i["columns"])})')
        for fk in t['foreign_keys']:
            lines.append(f'    [FK] {fk["column"]} -> {fk["references"]}')
        lines.append('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('left')
    parser.add_argument('right', nargs='?')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    left = read_schema(args.left)
    if args.right is None:
        print(json.dumps(left, indent=1) if args.json else render(left))
        return 0

    right = read_schema(args.right)
    problems = diff(left, right, args.left, args.right)
    if args.json:
        print(json.dumps(problems, indent=1))
    elif problems:
        print(f'{len(problems)} difference(s):')
        for p in problems:
            print(f'  {p}')
    else:
        print('Schemas are equivalent.')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
