"""Everything the app draws, with no icon-theme dependency.

The mark is the real Twingate logo, shipped as an alpha mask
(`data/twingate.png`) so it can be painted in any colour. Two renderings, two
jobs:

* the **tray** gets the bare glyph, tinted by state, on a transparent
  background. There is no state dot: colour alone carries the state, which is
  one thing to read instead of two;
* the **launcher icon** gets the glyph on a rounded near-black plate, matching
  the source artwork — a transparent white glyph is invisible in a menu or dock.

Both come from the same mask, so the mark cannot drift between them.
"""

from __future__ import annotations

import math
from pathlib import Path

import cairo

# --------------------------------------------------------------------- palette

ONLINE = "online"
AUTHENTICATING = "authenticating"
OFFLINE = "offline"
DISCONNECTED = "disconnected"
ERROR = "error"
STOPPED = "not-running"

WHITE = (1.00, 1.00, 1.00)
GREEN = (0.20, 0.83, 0.60)
AMBER = (0.98, 0.75, 0.25)
RED = (0.90, 0.35, 0.35)

# Colour alone carries the state now, so the mapping is the whole UI contract.
# The three not-connected states deliberately share white — to a user glancing
# at the bar they are one situation, "off". Only states that need attention get
# a colour of their own, because only those should interrupt.
STATE_COLORS = {
    ONLINE: GREEN,
    AUTHENTICATING: AMBER,
    OFFLINE: WHITE,
    DISCONNECTED: WHITE,
    ERROR: RED,
    STOPPED: WHITE,
}

PLATE_BG = (0.071, 0.075, 0.082)       # near-black, as in the source artwork
PLATE_GLYPH = WHITE


# ----------------------------------------------------------------------- mask

MASK_PATH = Path(__file__).with_name("data") / "twingate.png"

_mask_cache: cairo.ImageSurface | None = None
_scaled_cache: dict[int, cairo.ImageSurface] = {}


def mask_surface() -> cairo.ImageSurface:
    """The stored alpha mask, loaded once. RGB is irrelevant; alpha is coverage."""
    global _mask_cache
    if _mask_cache is None:
        if not MASK_PATH.exists():
            raise RuntimeError(f"logo mask missing: {MASK_PATH}")
        _mask_cache = cairo.ImageSurface.create_from_png(str(MASK_PATH))
    return _mask_cache


def _scaled_mask(size: int) -> cairo.ImageSurface:
    """The mask fitted into a `size` square, **preserving aspect**, cached.

    The logo is portrait (307x512, aspect 0.6). Scaling it to fill a square
    stretches it and the glyph turns into a smear — which is exactly what went
    wrong before. It is scaled by *height* to fit and centred horizontally, so
    the shape is always the shape.

    No alpha boost: the source is a clean 512px-tall RGBA with proper
    antialiasing, so downsampling to bar size keeps honest coverage. The gamma
    hack that lived here was compensating for a low-resolution JPEG source.
    """
    cached = _scaled_cache.get(size)
    if cached is None:
        source = mask_surface()
        sw, sh = source.get_width(), source.get_height()
        scale = size / max(sw, sh)
        w, h = max(1, round(sw * scale)), max(1, round(sh * scale))

        cached = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(cached)
        cr.translate((size - w) / 2.0, (size - h) / 2.0)
        cr.scale(w / sw, h / sh)
        cr.set_source_surface(source, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BEST)
        cr.paint()
        cached.flush()
        _scaled_cache[size] = cached
    return cached


def _paint_glyph(cr, size: int, color) -> None:
    cr.set_source_rgb(*color)
    cr.mask_surface(_scaled_mask(size), 0, 0)


def _surface(size: int):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    return surface, cairo.Context(surface)


def canonical(state: str) -> str:
    """Resolve an unknown state once, then read everything from that one key."""
    return state if state in STATE_COLORS else STOPPED


def state_color(state: str):
    return STATE_COLORS[canonical(state)]


def tray_surface(state: str, size: int = 22) -> cairo.ImageSurface:
    """The bar icon: the glyph alone, tinted by state, no dot."""
    surface, cr = _surface(size)
    _paint_glyph(cr, size, state_color(state))
    surface.flush()
    return surface


def plate_surface(state: str = ONLINE, size: int = 128) -> cairo.ImageSurface:
    """The launcher icon: glyph on a rounded near-black plate.

    Deliberately not state-coloured — an app-menu icon is an identity, and it
    must not look like a different application depending on connection state.
    """
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
    cr.set_source_rgb(*PLATE_BG)
    cr.fill()

    # Inset so the glyph keeps breathing room, but not so much that it becomes
    # a smudge at 22 px.
    inner = int(round(size * 0.82))
    inset = (size - inner) / 2.0
    cr.save()
    cr.translate(inset, inset)
    _paint_glyph(cr, inner, PLATE_GLYPH)
    cr.restore()

    surface.flush()
    return surface


def surface_to_pixmap(surface: cairo.ImageSurface) -> tuple[int, int, bytes]:
    """ARGB32 → SNI's `a(iiay)` payload.

    Two conversions happen here:

    * cairo is native-endian **premultiplied** ARGB and D-Bus wants A,R,G,B, so
      the bytes are reordered per pixel;
    * the output is rebuilt tightly as `width * height * 4`, which is the buffer
      length D-Bus requires. (Cairo's own stride is already `width * 4` for
      ARGB32 — 4 bytes per pixel is inherently aligned — so this is a contract
      guarantee, not a de-padding step.)
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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(path))
