"""Start-at-login as a user-facing setting, not an install flag.

One writer with three callers — the window switch, the CLI and `install.sh` —
so there is exactly one definition of what the entry contains. `install.sh`
calls `twintray autostart on` rather than writing its own copy.

Disabling writes `Hidden=true` instead of deleting the file: a deleted entry
re-exposes any system-wide one underneath it, while a hidden one masks it,
which is what "off" is supposed to mean.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_ID, APP_NAME

_TEMPLATE = """[Desktop Entry]
Type=Application
Version=1.0
Name={name} (tray)
Comment=Twingate status and connection in the system tray
Exec={exec_path} tray
Icon={icon}
Terminal=false
NoDisplay=true
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""


def autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "autostart"


def entry_path() -> Path:
    return autostart_dir() / f"{APP_ID}.desktop"


def resolve_executable() -> str:
    """Find the launcher this install owns.

    Walk *up* looking for `bin/twintray` instead of counting parents: the
    install layout is `PREFIX/share/twintray/twintray/`, and a wrong depth does
    not raise — it silently falls through to PATH and returns a different copy,
    so a custom-PREFIX install writes an entry pointing at `~/.local/bin`.
    """
    from shutil import which

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "bin" / "twintray"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return which("twintray") or "twintray"


def render(exec_path: str | None = None) -> str:
    from . import ICON_NAME

    return _TEMPLATE.format(
        name=APP_NAME,
        exec_path=exec_path or resolve_executable(),
        icon=ICON_NAME,
    )


def is_enabled() -> bool:
    """Read the flags, not the file's existence — a hidden entry exists."""
    path = entry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if line.strip().lower() == "hidden=true":
            return False
        if line.strip().lower() == "x-gnome-autostart-enabled=false":
            return False
    return True


def enable(exec_path: str | None = None) -> Path:
    path = entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(exec_path), encoding="utf-8")
    return path


def disable() -> None:
    path = entry_path()
    if not path.exists():
        return
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("hidden=") or lowered.startswith(
            "x-gnome-autostart-enabled="
        ):
            continue
        lines.append(line)
    lines.append("Hidden=true")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_enabled(enabled: bool) -> None:
    enable() if enabled else disable()
