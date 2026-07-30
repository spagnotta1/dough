"""Render a model's markdown reply as HTML, with no external dependency.

The assistant is asked for markdown because that is what it is good at, and the
browser needs HTML. A full markdown library would be a large dependency for a
grammar this small, and -- more to the point -- an unbounded one: the output
here is styled inline to match the app, and every tag it can emit is listed in
this file. That is a property worth keeping for text a model wrote.

Escaping happens before formatting, so a reply containing `<script>` renders as
characters rather than as a tag.
"""

import re


def md_to_html(text):
    """Convert Claude's markdown to HTML with no external dependencies."""
    if not text:
        return ''

    def esc(s):
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def fmt(s):
        s = esc(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        s = re.sub(r'`(.+?)`', r'<code style="background:#f3f4f6;padding:.1em .3em;border-radius:3px;font-size:.85em">\1</code>', s)
        return s

    lines = text.split('\n')
    parts = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if re.match(r'^-{3,}$', s) or re.match(r'^\*{3,}$', s):
            parts.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:.6em 0">')
            i += 1; continue
        if s.startswith('### '):
            parts.append(f'<h3 style="font-weight:600;font-size:.9rem;margin:.75em 0 .2em;color:#1f2937">{fmt(s[4:])}</h3>')
            i += 1; continue
        if s.startswith('## '):
            parts.append(f'<h2 style="font-weight:700;font-size:1rem;margin:.85em 0 .25em;color:#111827">{fmt(s[3:])}</h2>')
            i += 1; continue
        if s.startswith('# '):
            parts.append(f'<h1 style="font-weight:700;font-size:1.1rem;margin:1em 0 .3em;color:#111827">{fmt(s[2:])}</h1>')
            i += 1; continue
        if s.startswith('|'):
            tbl, thead, tbody_rows, hdr_done = [], '', [], False
            while i < len(lines) and lines[i].strip().startswith('|'):
                tbl.append(lines[i].strip()); i += 1
            for row in tbl:
                if re.match(r'^\|[\s\-\:|]+\|$', row):
                    hdr_done = True; continue
                cells = [c.strip() for c in row.split('|')[1:-1]]
                if not hdr_done:
                    thead = '<thead><tr>' + ''.join(
                        f'<th style="font-weight:600;text-align:left;padding:.35em .6em;border:1px solid #ddd6fe;background:#f3f0ff;color:#5b21b6;font-size:.78rem">{fmt(c)}</th>'
                        for c in cells) + '</tr></thead>'
                else:
                    tbody_rows.append('<tr>' + ''.join(
                        f'<td style="padding:.3em .6em;border:1px solid #e5e7eb;font-size:.8rem;vertical-align:top">{fmt(c)}</td>'
                        for c in cells) + '</tr>')
            parts.append(f'<div style="overflow-x:auto;margin:.5em 0"><table style="width:100%;border-collapse:collapse">'
                         f'{thead}<tbody>{"".join(tbody_rows)}</tbody></table></div>')
            continue
        if re.match(r'^[-*] ', s):
            items = []
            while i < len(lines) and re.match(r'^[-*] ', lines[i].strip()):
                items.append(f'<li style="margin:.2em 0">{fmt(lines[i].strip()[2:])}</li>'); i += 1
            parts.append(f'<ul style="margin:.35em 0;padding-left:1.4em;list-style:disc">{"".join(items)}</ul>')
            continue
        if re.match(r'^\d+\. ', s):
            items = []
            while i < len(lines) and re.match(r'^\d+\. ', lines[i].strip()):
                item_text = re.sub(r'^\d+\. ', '', lines[i].strip())
                items.append(f'<li style="margin:.2em 0">{fmt(item_text)}</li>'); i += 1
            parts.append(f'<ol style="margin:.35em 0;padding-left:1.4em;list-style:decimal">{"".join(items)}</ol>')
            continue
        parts.append(f'<p style="margin:.3em 0;line-height:1.6">{fmt(s)}</p>')
        i += 1
    return ''.join(parts)
