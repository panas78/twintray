"""Everything the app draws, in cairo, with no asset files.

Two renderings, two jobs:

* the **tray** gets the bare TwinTray mark on a transparent background plus a
  small ringed state dot, because a bar full of strangers needs "which app is
  this?" answered before "is it connected?";
* the **launcher icon** gets the same mark on a rounded mint plate, because a
  transparent glyph is invisible in a menu or a dock.

Both come from one function, so the mark cannot drift between them.
"""

from __future__ import annotations

import math

import cairo

# --------------------------------------------------------------------- palette

ONLINE = "online"
AUTHENTICATING = "authenticating"
OFFLINE = "offline"
DISCONNECTED = "disconnected"
ERROR = "error"
STOPPED = "not-running"

# Every state needs a colour of its own: two states sharing one means the icon
# stops conveying the case that most needs attention.
STATE_COLORS = {
    ONLINE: (0.20, 0.83, 0.60),
    AUTHENTICATING: (0.98, 0.75, 0.25),
    OFFLINE: (0.65, 0.68, 0.75),
    DISCONNECTED: (0.45, 0.52, 0.66),
    ERROR: (0.90, 0.35, 0.35),
    STOPPED: (0.55, 0.58, 0.64),
}

# A state that needs the user to act. `online` is deliberately absent — a
# perfectly healthy connection gets the untouched mark and no annotation.
DOT_STATES = (AUTHENTICATING, OFFLINE, DISCONNECTED, ERROR, STOPPED)

GLYPH_COLOR = (0.00, 0.79, 0.67)      # TwinTray mint, on transparent
PLATE_COLOR = (0.00, 0.72, 0.62)      # launcher plate
PLATE_GLYPH = (0.04, 0.13, 0.16)      # near-black on the plate
RING_COLOR = (0.10, 0.12, 0.16)       # separates the dot from the mark

DOT_OUTER = 0.18                      # fractions of the icon size
DOT_INNER = 0.125


def _mark(cr, size, color, stroke_ratio=0.125, scale=1.0):
    """Draw the TwinTray mark: two nodes joined by a bar.

    A `>  <` pair was the first design and it does not survive: at 22 px the two
    apexes are ~2 px apart and the whole thing reads as an **X**, i.e. the
    universal glyph for "close/cancel" — the worst possible message on a VPN
    toggle. Two nodes and a bar stay legible at bar size and cannot be misread.
    """
    mid = size / 2.0
    radius = size * 0.135 * scale
    offset = size * 0.265 * scale
    bar = max(1.0, size * stroke_ratio * scale)

    cr.set_source_rgb(*color)

    # Bar first, so the node circles sit on top of its rounded ends.
    cr.set_line_width(bar)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(mid - offset, mid)
    cr.line_to(mid + offset, mid)
    cr.stroke()

    for direction in (-1, 1):
        cr.arc(mid + direction * offset, mid, radius, 0, 2 * math.pi)
        cr.fill()


def _draw_state_dot(cr, state, size):
    """A ringed corner dot. The ring is load-bearing, not decoration."""
    outer = size * DOT_OUTER
    centre = size * (1 - DOT_OUTER - 0.05)
    cr.arc(centre, centre, outer, 0, 2 * math.pi)
    cr.set_source_rgb(*RING_COLOR)
    cr.fill()
    cr.arc(centre, centre, size * DOT_INNER, 0, 2 * math.pi)
    cr.set_source_rgb(*STATE_COLORS[state])
    cr.fill()


def _surface(size):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    return surface, cairo.Context(surface)


def canonical(state: str) -> str:
    """Resolve an unknown state once, then read everything from that one key."""
    return state if state in STATE_COLORS else STOPPED


def tray_surface(state: str, size: int = 22) -> cairo.ImageSurface:
    """The bar icon: mint mark on transparent, plus a dot if action is needed."""
    state = canonical(state)
    surface, cr = _surface(size)
    _mark(cr, size, GLYPH_COLOR)
    if state in DOT_STATES:
        _draw_state_dot(cr, state, size)
    surface.flush()
    return surface


def plate_surface(state: str, size: int = 128) -> cairo.ImageSurface:
    """The launcher icon: the same mark, on a rounded plate."""
    state = canonical(state)
    surface, cr = _surface(size)
    radius = size * 0.22
    x = y = size * 0.04
    w = h = size - 2 * x
    cr.new_sub_path()
    cr.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgb(*PLATE_COLOR)
    cr.fill()

    # Inset the mark so it keeps breathing room inside the plate, but not so
    # much that it disappears at 22 px — 0.18 of the size made it a smudge.
    inset = size * 0.14
    cr.save()
    cr.translate(inset, inset)
    _mark(cr, size - 2 * inset, PLATE_GLYPH, stroke_ratio=0.145, scale=1.02)
    cr.restore()

    if state in DOT_STATES:
        _draw_state_dot(cr, state, size)
    surface.flush()
    return surface


def surface_to_pixmap(surface: cairo.ImageSurface) -> tuple[int, int, bytes]:
    """ARGB32 → SNI's `a(iiay)` payload.

    Two conversions happen here and both are mandatory:

    * cairo is native-endian **premultiplied** ARGB, D-Bus wants A,R,G,B — on
      x86 that is a byte reorder per pixel;
    * cairo pads the row stride (a 22 px surface has a 96-byte stride, not 88),
      so the raw buffer cannot be sent: every row would shift.
    """
    width, height = surface.get_width(), surface.get_height()
    data = surface.get_data()
    stride = surface.get_stride()
    out = bytearray(width * height * 4)
    for row in range(height):
        src = row * stride
        dst = row * width * 4
        for col in range(width):
            b, g, r, a = data[src + col * 4 : src + col * 4 + 4]
            out[dst + col * 4] = a
            out[dst + col * 4 + 1] = r
            out[dst + col * 4 + 2] = g
            out[dst + col * 4 + 3] = b
    return width, height, bytes(out)


def tray_pixmap(state: str, size: int = 22) -> list[tuple[int, int, bytes]]:
    return [surface_to_pixmap(tray_surface(state, size))]


def write_png(surface: cairo.ImageSurface, path) -> None:
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(path))
