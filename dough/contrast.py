"""Readable ink for a background colour, server-side.

The browser has this already: ``CheckScheme.onColor`` in base.html picks the
ink for the theme's accent. That routine cannot help with colours that arrive
as *data* — an institution's brand colour is chosen by the institution, lives
on the adapter class, and is written into the markup as an inline background.
Deriving its ink in JavaScript means the label is unreadable until a script
runs, and permanently unreadable if one does not; Plaid ships ``#000000``,
so on a light theme the fallback ink was black text on a black button.

Same formulas as the browser copy (WCAG 2.x relative luminance and contrast
ratio) and the same two candidate inks, so the two agree on every input.
"""

from __future__ import annotations

#: The two inks the product paints on coloured surfaces: white, and the
#: near-black used for ink throughout the design system.
_WHITE = (255, 255, 255)
_NEAR_BLACK = (17, 17, 19)


def parse_hex(color: str) -> tuple[int, int, int]:
    """``#rgb`` / ``#rrggbb`` → an (r, g, b) triple; black if unparseable.

    Unparseable input falls back to black rather than raising: this runs while
    rendering a page, and a malformed accent colour on one adapter must not
    take the Connections page down with it.
    """
    text = (color or '').strip().lstrip('#')
    if len(text) == 3:
        text = ''.join(c * 2 for c in text)
    if len(text) != 6:
        return (0, 0, 0)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for raw in rgb:
        v = raw / 255
        channels.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two colours, 1.0 (same) to 21.0."""
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def on_color(background: str) -> str:
    """The more readable of white / near-black for text laid on ``background``."""
    bg = parse_hex(background)
    return '#ffffff' if contrast(bg, _WHITE) >= contrast(bg, _NEAR_BLACK) else '#111113'
