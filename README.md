# TwinTray

A system tray front-end for the [Twingate](https://www.twingate.com) client on
Linux — the equivalent of what [Trayscale](https://github.com/DeedleFake/trayscale)
does for Tailscale.

Twingate ships a CLI and a desktop notifier, but no UI: checking whether the
tunnel is up means running `twingate status` in a terminal, and starting it means
`systemctl start twingate` plus an SSO round-trip. TwinTray puts that behind a
tray icon.

![icon](data/icons/hicolor/128x128/apps/twintray.png)

## What it does

- **State at a glance** — the tray icon is the Twingate glyph, tinted by state:
  **white when not connected**, **green when connected**, amber while
  authenticating, red on error. No corner dot: colour alone carries the state,
  so there is one thing to read instead of two.
- **One-click connect/disconnect** — starts and stops `twingate.service`
  passwordless (via a polkit rule), then kicks the SSO flow.
- **Tailscale coupling** — Twingate and Tailscale both claim `100.96.12.0/12`,
  so the app can stand Tailscale down while Twingate is up and restore it after.
  On by default when `tailscale` is on `PATH`.
- **Resources tab** — the resources the client is authorized for.
- **Scriptable** — every operation is also a CLI subcommand with `--json`.

## Install

```bash
./install.sh                 # install + start at login
./install.sh --no-autostart  # install without the autostart entry
./install.sh --uninstall     # remove everything it installed
PREFIX=/tmp/x ./install.sh   # install elsewhere (used for round-trip tests)
```

No root needed: the app lives in `~/.local` and reaches the system only through
`systemctl start/stop twingate`.

**Dependencies come from the distro, never pip** — PyGObject binds to system
shared libraries and a venv copy cannot see the typelibs:

```bash
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita python3-cairo desktop-file-utils
# Debian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-cairo desktop-file-utils
```

### Passwordless start/stop

`systemctl start/stop twingate` needs polkit authorization. A bare wlroots
session (niri/Umbriel, labwc) ships no polkit agent, so the prompt never appears
and the click silently fails. The rule that fixes it lives in the sibling
[MySettings](https://github.com/panas78/MySettings) repo:

```bash
sudo ~/dev/MySettings/Linux/install-twingate-polkit.sh
```

`twintray doctor` tells you whether it is working.

## CLI

```bash
twintray status [--json] [--brief]
twintray resources [--json] [--all]
twintray connect [--target '*.example.gr'] [--no-couple-tailscale]
twintray disconnect [--no-couple-tailscale]
twintray doctor
twintray autostart on|off|status
twintray window        # open the window (also: gui, app)
twintray tray          # tray only, window on demand
twintray icon-probe    # render every state icon to PNG
```

`status` and `doctor` never import GTK, so they work over SSH and in cron.

## Layout

```
twintray/core.py        all logic — no GTK import, so it is testable headless
twintray/cli.py         argparse front-end over core
twintray/window.py      GTK4/libadwaita window, three tabs
twintray/tray.py        StatusNotifierItem + DBusMenu, written against Gio.DBus
twintray/icons.py       every pixel the app draws, in cairo
twintray/data/twingate.png  the logo as an alpha mask, tinted at draw time
twintray/prefs.py       one JSON file under XDG_CONFIG_HOME
twintray/autostart.py   the XDG autostart entry, one writer, three callers
```

`core.py` must never import `gi`. The GUI and the tray are consumers, never the
other way round.

## Tests

```bash
uv venv --python /usr/bin/python3 --system-site-packages /tmp/t
VIRTUAL_ENV=/tmp/t uv pip install -q pytest
PYTHONPATH=$PWD /tmp/t/bin/python -m pytest tests/
```

60 tests: core parsing with the subprocess stubbed, CLI shapes and exit codes,
and widget-level tests that build the real window. The widget tests skip
themselves with no display; run them on a desktop or they prove nothing.

## Notes

- The tray publishes `IconPixmap` with **an empty `IconName`**. Any theme name
  here makes the host draw that glyph and ignore the pixmap entirely.
- `Status` is always `Active`. Noctalia hides `Passive` items, and "disconnected"
  is exactly when a VPN icon must stay clickable.
- The logo ships as a transparent RGBA PNG and is used as an alpha mask, tinted
  in cairo at draw time, so one asset covers every state. It is portrait
  (307x512), so it is **fitted by height and centred**, never stretched to fill
  the square — stretching turns the glyph into a smear.
- White on a **light** bar is invisible. The default assumes a dark bar; on a
  light theme, drop `STATE_COLORS[STOPPED]` to a mid grey.
- Closing the window hides it. Quit is an explicit menu item.
