"""Tray menu and icon-state logic, without needing a bus or a running bar.

The D-Bus surface itself is exercised live; these cover the decisions that were
getting them wrong: when Authenticate appears, and what colour the icon is when
the tunnel is up but the resource behind it is not.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from twintray import core, icons, tray  # noqa: E402


def make_tray(**kwargs) -> tray.Tray:
    return tray.Tray(
        on_toggle=kwargs.pop("on_toggle", lambda: None),
        on_open=kwargs.pop("on_open", lambda: None),
        on_quit=kwargs.pop("on_quit", lambda: None),
        **kwargs,
    )


def labels(t) -> list[str]:
    return [c.label for c in t.menu_root().children if c.kind != "separator"]


def test_menu_has_no_authenticate_item_when_nothing_is_locked():
    t = make_tray()
    t.set_status(core.Status(state=core.ONLINE))
    assert labels(t) == ["Disconnect", "Open TwinTray", "Quit"]


def test_menu_offers_authenticate_for_one_locked_resource():
    t = make_tray()
    t.set_status(core.Status(state=core.ONLINE))
    t.set_pending(["*.diastasys.gr"])
    assert "Authenticate *.diastasys.gr…" in labels(t)


def test_menu_counts_several_locked_resources():
    t = make_tray()
    t.set_status(core.Status(state=core.ONLINE))
    t.set_pending(["a", "b", "c"])
    assert "Authenticate 3 resources…" in labels(t)


def test_authenticate_item_disappears_once_nothing_is_pending():
    t = make_tray()
    t.set_status(core.Status(state=core.ONLINE))
    t.set_pending(["a"])
    assert any(lbl.startswith("Authenticate") for lbl in labels(t))
    t.set_pending([])
    assert not any(lbl.startswith("Authenticate") for lbl in labels(t))


def test_connected_but_locked_is_amber_not_green():
    """A green icon while traffic is held is the lie that causes the confusion."""
    t = make_tray()
    t.set_status(core.Status(state=core.ONLINE))
    t.set_pending(["*.diastasys.gr"])
    assert t.icon_state == core.AUTHENTICATING
    assert icons.STATE_COLORS[t.icon_state] == icons.AMBER

    t.set_pending([])
    assert t.icon_state == core.ONLINE
    assert icons.STATE_COLORS[t.icon_state] == icons.GREEN


def test_pending_while_disconnected_does_not_paint_the_icon_amber():
    # Stale pending data from the last session must not survive a disconnect.
    t = make_tray()
    t.set_status(core.Status(state=core.STOPPED))
    t.set_pending(["*.diastasys.gr"])
    assert t.icon_state == core.STOPPED


def test_authenticate_dispatch_calls_the_handler():
    called = []
    t = make_tray(on_auth=lambda: called.append(True))
    t.set_pending(["x"])
    t._dispatch(tray.ID_AUTH)
    assert called == [True]


def test_authenticate_dispatch_is_safe_without_a_handler():
    t = make_tray()
    t.set_pending(["x"])
    t._dispatch(tray.ID_AUTH)  # must not raise
