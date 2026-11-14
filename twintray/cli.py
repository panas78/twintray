"""Command-line front-end. Works over SSH, in cron, and in the test suite.

The toolkit is imported lazily inside the GUI entry points only, so
`twintray status --json` never drags in GTK and runs with no display.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import APP_NAME, VERSION, core
from . import autostart as autostart_mod
from . import prefs as prefs_mod


def _print_checks(checks) -> None:
    for name, ok, detail in checks:
        flag = "ok  " if ok else ("??  " if ok is None else "FAIL")
        print(f"[{flag}] {name:<20} {detail}")


def cmd_status(args) -> int:
    current = core.status(verbose=not args.brief)
    # Locked resources are only meaningful while the tunnel is up, but the key
    # is always present: a JSON contract with conditionally-absent fields makes
    # every consumer write the same `if` and one of them forgets.
    listing = core.resources() if current.state in core.LIVE_STATES else None
    pending = listing.pending if listing is not None else []

    if args.json:
        payload = current.to_dict()
        payload["pending_auth"] = [r.name for r in pending]
        print(json.dumps(payload, indent=2))
        return 0 if current.state != core.UNKNOWN else 1

    print(current.label)
    if current.network:
        print(f"  network: {current.network}")
    if current.auth_expires:
        print(f"  auth:    {current.auth_expires}")
    # The trap this line exists for: online, tunnel up, and the resource you
    # actually want is still locked behind its own SSO round-trip.
    if pending:
        waiting = ", ".join(f"{r.name} ({r.auth_status})" for r in pending)
        print(f"  pending: {waiting}")
        print("  run 'twintray auth' to authenticate")
    if current.error:
        print(f"  error:   {current.error}", file=sys.stderr)
    return 0 if current.state != core.UNKNOWN else 1


def cmd_resources(args) -> int:
    listing = core.resources(include_hidden=args.all)
    if args.json:
        print(json.dumps(listing.to_dict(), indent=2))
        return 0 if not listing.error else 1
    if listing.error:
        print(listing.error, file=sys.stderr)
        return 1
    if not listing.items:
        print("No resources.")
        return 0
    width = max(len(item.name) for item in listing.items)
    for item in listing.items:
        line = f"{item.name:<{width}}  {item.address}" if item.address else item.name
        if item.needs_auth:
            line += f"  [{item.auth_status}]"
        print(line)
    return 0


def cmd_auth(args) -> int:
    result = core.authenticate(args.target or "")
    for step in result.steps:
        print(f"  {step}")
    print(result.message)
    return 0 if result.ok else 1


def cmd_connect(args) -> int:
    current_prefs = prefs_mod.Prefs.load()
    target = args.target if args.target is not None else current_prefs.auth_target
    coupled = (
        args.couple_tailscale
        if args.couple_tailscale is not None
        else current_prefs.tailscale_coupled()
    )
    result = core.connect(auth_target=target, couple_tailscale=coupled)
    for step in result.steps:
        print(f"  {step}")
    print(result.message)
    return 0 if result.ok else 1


def cmd_disconnect(args) -> int:
    current_prefs = prefs_mod.Prefs.load()
    coupled = (
        args.couple_tailscale
        if args.couple_tailscale is not None
        else current_prefs.tailscale_coupled()
    )
    result = core.disconnect(couple_tailscale=coupled)
    for step in result.steps:
        print(f"  {step}")
    print(result.message)
    return 0 if result.ok else 1


def cmd_doctor(args) -> int:
    checks = core.doctor()
    _print_checks(checks)
    return 0 if core.summarise(checks) else 1


def cmd_autostart(args) -> int:
    if args.state == "status":
        print("enabled" if autostart_mod.is_enabled() else "disabled")
        return 0
    if args.state == "on":
        path = autostart_mod.enable()
        print(f"Installed {path}")
        return 0
    autostart_mod.disable()
    print("Disabled (entry kept, Hidden=true)")
    return 0


def cmd_icon_probe(args) -> int:
    """Render every state to PNG so the icon design can be judged, not guessed."""
    from pathlib import Path

    from . import icons

    out = Path(args.outdir)
    states = [
        core.ONLINE,
        core.AUTHENTICATING,
        core.OFFLINE,
        core.DISCONNECTED,
        core.ERROR,
        core.STOPPED,
    ]
    for state in states:
        icons.write_png(icons.tray_surface(state, 22), out / f"tray-{state}.png")
    # One plate, not one per state: it carries identity, not status.
    icons.write_png(icons.plate_surface(128), out / "plate.png")
    print(f"Wrote {len(states) + 1} PNGs to {out}")
    return 0


def cmd_window(args) -> int:
    from .app import main as app_main

    return app_main(show_window=True)


def cmd_tray(args) -> int:
    from .app import main as app_main

    return app_main(show_window=False)


def cmd_about(args) -> int:
    print(f"{APP_NAME} {VERSION}")
    print("A Twingate client front-end for the Linux system tray.")
    print(f"Prefs: {prefs_mod.prefs_path()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="twintray",
        description=f"{APP_NAME} — Twingate status and connection in the system tray.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="Show connection state")
    status.add_argument("--json", action="store_true", help="Machine-readable output")
    # Both spellings: `-v/--verbose` is what `twingate status` uses, so it is
    # what fingers type; `--brief` is the same axis read the other way.
    status.add_argument(
        "-v", "--verbose", dest="brief", action="store_false",
        help="Include the detail lines (the default)",
    )
    status.add_argument(
        "-b", "--brief", dest="brief", action="store_true",
        help="Print the state only",
    )
    status.set_defaults(func=cmd_status, brief=False)

    resources = sub.add_parser("resources", help="List authorized resources")
    resources.add_argument("--json", action="store_true")
    resources.add_argument("--all", action="store_true", help="Include hidden resources")
    resources.set_defaults(func=cmd_resources)

    auth = sub.add_parser(
        "auth", help="Authenticate a locked resource (opens your browser)"
    )
    auth.add_argument(
        "target",
        nargs="?",
        default="",
        help="Resource name; omit to authenticate everything that is pending",
    )
    auth.set_defaults(func=cmd_auth)

    connect = sub.add_parser("connect", help="Start the tunnel (mirrors tg-start)")
    connect.add_argument("--target", default=None, help="Resource pattern for twingate auth")
    connect.add_argument(
        "--couple-tailscale",
        dest="couple_tailscale",
        action="store_true",
        default=None,
        help="Force dropping Tailscale while connected",
    )
    connect.add_argument(
        "--no-couple-tailscale",
        dest="couple_tailscale",
        action="store_false",
        help="Leave Tailscale alone",
    )
    connect.set_defaults(func=cmd_connect)

    disconnect = sub.add_parser("disconnect", help="Stop the tunnel (mirrors tg-stop)")
    disconnect.add_argument(
        "--couple-tailscale",
        dest="couple_tailscale",
        action="store_true",
        default=None,
        help="Force bringing Tailscale back up",
    )
    disconnect.add_argument(
        "--no-couple-tailscale",
        dest="couple_tailscale",
        action="store_false",
        help="Leave Tailscale alone",
    )
    disconnect.set_defaults(func=cmd_disconnect)

    doctor = sub.add_parser("doctor", help="Check everything the app needs")
    doctor.set_defaults(func=cmd_doctor)

    autostart = sub.add_parser("autostart", help="Start-at-login entry")
    autostart.add_argument("state", choices=["on", "off", "status"])
    autostart.set_defaults(func=cmd_autostart)

    window = sub.add_parser("window", help="Open the window", aliases=["gui", "app"])
    window.set_defaults(func=cmd_window)

    tray = sub.add_parser("tray", help="Run tray-only (no window until opened)")
    tray.set_defaults(func=cmd_tray)

    probe = sub.add_parser("icon-probe", help="Render every state icon to PNG")
    probe.add_argument("--outdir", default="/tmp/twintray-icons")
    probe.set_defaults(func=cmd_icon_probe)

    about = sub.add_parser("about", help="Print version and paths")
    about.set_defaults(func=cmd_about)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except core.TwingateMissing as exc:
        print(f"Twingate CLI missing: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
