"""Preferences: one small JSON file under XDG_CONFIG_HOME.

Nothing here talks to Twingate. This module is import-safe with no toolkit and
no subprocess, so the CLI, the tray and the window all share one definition of
what a setting is.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "twintray"


def prefs_path() -> Path:
    return config_dir() / "prefs.json"


@dataclass
class Prefs:
    """User-facing behaviour.

    Defaults are deliberately *few*. Anything the app can detect at runtime is
    detected instead of stored, so this stays a short list of real choices.
    """

    # Resource pattern handed to `twingate auth` after the service comes up.
    # Empty string means "do not run auth"; the empty default keeps the app
    # usable on a network with no wildcard resource.
    auth_target: str = ""

    # tg-start/tg-stop drop and restore Tailscale because the two clients fight
    # over 100.96.12.0/12. `None` means "decide at runtime": couple them when a
    # `tailscale` binary is on PATH.
    manage_tailscale: bool | None = None

    poll_seconds: int = 5
    notify: bool = True
    start_at_login: bool = False

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls) -> "Prefs":
        path = prefs_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()

        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known}
        prefs = cls()
        for key, value in kwargs.items():
            expected = type(getattr(prefs, key))
            # `bool | None` fields report `object` as their type; accept
            # anything json gives us there and let the caller coerce.
            if key == "manage_tailscale":
                if value is None or isinstance(value, bool):
                    setattr(prefs, key, value)
                continue
            if isinstance(value, expected) or (
                expected is int and isinstance(value, bool) is False
            ):
                setattr(prefs, key, value)
        prefs.poll_seconds = max(2, min(int(prefs.poll_seconds or 5), 300))
        return prefs

    # ---------------------------------------------------------------- saving

    def save(self) -> None:
        """Write atomically so a crash mid-write cannot truncate the file."""
        path = prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prefs-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --------------------------------------------------------------- helpers

    def tailscale_coupled(self) -> bool:
        """Resolve the tri-state into a real answer."""
        if self.manage_tailscale is None:
            from .core import tailscale_available

            return tailscale_available()
        return bool(self.manage_tailscale)
