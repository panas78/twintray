#!/usr/bin/python3
"""Regenerate the committed hicolor PNG set from twintray/icons.py.

The tray icon is drawn at runtime, so it needs no files. These PNGs exist only
so the desktop's own icon lookup (`Icon=twintray` in the .desktop entry) finds
something to show in a menu, a dock or a task switcher.

Run from the repo root:

    /usr/bin/python3 tools/make-icons.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twintray import icons  # noqa: E402

SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def main() -> int:
    out = ROOT / "data" / "icons" / "hicolor"
    for size in SIZES:
        target = out / f"{size}x{size}" / "apps" / "twintray.png"
        icons.write_png(icons.plate_surface(icons.ONLINE, size), target)
        print(f"  {target.relative_to(ROOT)}")
    print(f"Wrote {len(SIZES)} sizes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
