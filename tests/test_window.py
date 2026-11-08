"""Widget-level tests.

Compile + lint + unit tests passing tells you nothing about whether the GUI
works: a missing Adw method only surfaces when a widget is actually constructed.
So any change to widget construction, visibility or signal handlers gets a test
here.

Creating a GTK window with no display calls abort() rather than raising, so the
skip has to happen at import time — a try/except cannot save it.
"""

import os

import pytest

if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
    pytest.skip("no display", allow_module_level=True)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from twintray import core, icons  # noqa: E402
from twintray.prefs import Prefs  # noqa: E402
from twintray.window import TwinTrayWindow  # noqa: E402


class FakeController:
    def __init__(self):
        self.prefs = Prefs()
        self.prefs.auth_target = "*.diastasys.gr"
        self.status = core.Status(state=core.ONLINE, network="diastasys")
        self.resources = core.ResourceList()
        self.service_active = True
        self.tailscale_state = "stopped"
        self.saves = 0
        self.toggles = 0

    def save_prefs(self, on_error, previous=None):
        self.saves += 1
        return True

    def toggle_async(self, on_result=None):
        self.toggles += 1

    def connect_async(self, on_result=None):
        self.toggles += 1

    def disconnect_async(self, on_result=None):
        self.toggles += 1

    def refresh_async(self, then=None):
        pass


@pytest.fixture(scope="session")
def application():
    """One Adw.Application for the whole module.

    A fresh `Adw.Application` per test re-exports the same object path and
    fails with `An object is already exported for the interface
    org.gtk.Application at /io/github/...`.
    """
    Adw.init()
    app = Adw.Application(application_id="io.github.panas78.TwintrayTest")
    app.register(None)
    return app


@pytest.fixture
def window(application):
    controller = FakeController()
    win = TwinTrayWindow(application=application, controller=controller)
    yield win, controller
    win.destroy()


def _groups_in(widget):
    found = []

    def walk(node):
        if isinstance(node, Adw.PreferencesGroup):
            found.append(node)
            return
        child = node.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return found


def _section_titles(widget):
    return [g.get_title() for g in _groups_in(widget) if g.get_title()]


def _listbox_rows(listbox):
    rows, child = [], listbox.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def test_title_uses_the_constant(window):
    win, _ = window
    from twintray import APP_NAME

    assert win.get_title() == APP_NAME


def test_three_tabs_with_resolving_icons(window):
    win, _ = window
    pages = win._stack.get_pages()
    assert pages.get_n_items() == 3
    assert [pages.get_item(i).get_name() for i in range(3)] == [
        "connection",
        "resources",
        "options",
    ]
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    for name in ("network-vpn-symbolic", "network-server-symbolic", "emblem-system-symbolic"):
        assert theme.has_icon(name), name


def test_each_section_lives_on_its_own_page(window):
    win, _ = window
    assert _section_titles(win._connection_page) == ["Twingate", "Interface coupling"]
    assert _section_titles(win._resources_page) == ["Authorized resources"]
    assert _section_titles(win._options_page) == ["Behaviour", "Diagnostics"]


def test_nothing_accumulates_across_repeated_updates(window):
    """The regression guard: a no-op detach used to stack a copy per poll."""
    win, controller = window
    conn_baseline = len(_groups_in(win._connection_page))
    opt_baseline = len(_groups_in(win._options_page))
    for _ in range(4):
        win.update(controller.status)
        win.update_resources(controller.resources)
    assert len(_groups_in(win._connection_page)) == conn_baseline
    assert len(_groups_in(win._options_page)) == opt_baseline
    assert _section_titles(win._connection_page).count("Twingate") == 1
    assert _section_titles(win._options_page).count("Behaviour") == 1


def test_resource_rows_do_not_pile_up(window):
    win, controller = window
    controller.resources = core.ResourceList(
        items=[core.Resource("BC Prod", "10.0.0.5"), core.Resource("NAS", "10.0.0.9")]
    )
    for _ in range(3):
        win.update_resources(controller.resources)
        assert len(_listbox_rows(win._resources_list)) == 2

    controller.resources = core.ResourceList()
    win.update_resources(controller.resources)
    assert len(_listbox_rows(win._resources_list)) == 0
    assert win._resources_empty.get_visible() is True


def test_toggle_button_follows_state(window):
    win, controller = window
    for state, label in (
        (core.ONLINE, "Disconnect"),
        (core.AUTHENTICATING, "Connect"),
        (core.OFFLINE, "Connect"),
        (core.DISCONNECTED, "Connect"),
        (core.ERROR, "Connect"),
        (core.STOPPED, "Connect"),
    ):
        controller.status = core.Status(state=state)
        win.update(controller.status)
        assert win._toggle_button.get_label() == label, state
        assert win._status_row.get_title() == core.STATE_LABELS[state]


def test_online_button_is_suggested_and_live_is_destructive(window):
    win, controller = window
    controller.status = core.Status(state=core.ONLINE)
    win.update(controller.status)
    assert win._toggle_button.has_css_class("destructive-action")
    controller.status = core.Status(state=core.STOPPED)
    win.update(controller.status)
    assert win._toggle_button.has_css_class("suggested-action")
    assert not win._toggle_button.has_css_class("destructive-action")


def test_every_state_redraws_without_error(window):
    win, controller = window
    for state in icons.STATE_COLORS:
        controller.status = core.Status(state=state, network="diastasys")
        win.update(controller.status)


def _pump(milliseconds=900):
    """Run the main loop for a while so a GLib timeout can fire."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + milliseconds * 1000
    while GLib.get_monotonic_time() < deadline:
        while context.iteration(False):
            pass
        GLib.usleep(2000)


def test_auth_target_edit_is_persisted(window):
    win, controller = window
    before = controller.saves
    win._auth_target_row.set_text("*.example.gr")
    # The value is live immediately; the write is debounced.
    assert controller.prefs.auth_target == "*.example.gr"
    assert controller.saves == before
    _pump()
    assert controller.saves == before + 1


def test_auth_target_typing_is_debounced_into_one_write(window):
    win, controller = window
    before = controller.saves
    for text in ("*.a", "*.ab", "*.abc", "*.abcd"):
        win._auth_target_row.set_text(text)
    _pump()
    assert controller.saves == before + 1
    assert controller.prefs.auth_target == "*.abcd"


def test_settings_switches_write_through(window, monkeypatch):
    win, controller = window
    # Never touch the real autostart directory from a test.
    written = []
    monkeypatch.setattr(
        "twintray.window.autostart_mod.set_enabled", lambda value: written.append(value)
    )
    before = controller.saves

    win._notify_switch.set_active(not win._notify_switch.get_active())
    assert controller.prefs.notify == win._notify_switch.get_active()

    win._autostart_switch.set_active(not win._autostart_switch.get_active())
    assert written, "autostart switch did not write"

    assert controller.saves > before


def test_closing_hides_instead_of_quitting(window):
    win, _ = window
    win.set_visible(True)
    assert win._on_close_request(win) is True
    assert win.get_visible() is False


def test_network_and_auth_rows_never_show_a_bare_dash(window):
    """The reported bug: both rows read "—" permanently.

    Network was parsed from a line `twingate status -v` never prints, and
    Authentication from an expiry it never reports, so neither regex could ever
    match. Whatever the state, neither row may end up as a lone dash.
    """
    win, controller = window
    for state in (core.ONLINE, core.STOPPED, core.UNKNOWN):
        controller.status = core.Status(state=state, network="diastasys")
        controller.resources = core.ResourceList(items=[])
        win.update(controller.status)
        assert win._network_row.get_subtitle() == "diastasys", state
        assert win._auth_row.get_subtitle() != "—", state
        assert win._auth_row.get_subtitle().strip(), state


def test_network_row_says_not_configured_rather_than_a_dash(window):
    win, controller = window
    controller.status = core.Status(state=core.ONLINE, network="")
    win.update(controller.status)
    assert win._network_row.get_subtitle() == "not configured"


def test_auth_row_reports_resource_authorization_when_live(window):
    win, controller = window
    controller.status = core.Status(state=core.ONLINE, network="diastasys")
    controller.resources = core.ResourceList(
        items=[core.Resource("a", "1", auth_status="Auth expires in 6 days")]
    )
    win.update(controller.status)
    assert win._auth_row.get_subtitle() == "Auth expires in 6 days"

    controller.resources = core.ResourceList(items=[core.Resource("a", "1")])
    win.update(controller.status)
    assert win._auth_row.get_subtitle() == "not required by any resource"


def test_auth_row_prefers_a_session_expiry_when_one_is_reported(window):
    win, controller = window
    controller.status = core.Status(
        state=core.ONLINE, network="diastasys", auth_expires="6 days"
    )
    controller.resources = core.ResourceList(items=[core.Resource("a", "1")])
    win.update(controller.status)
    assert win._auth_row.get_subtitle() == "6 days"


def test_auth_row_explains_itself_while_stopped(window):
    win, controller = window
    controller.status = core.Status(state=core.STOPPED, network="diastasys")
    controller.resources = core.ResourceList()
    win.update(controller.status)
    assert "connect" in win._auth_row.get_subtitle().lower()


def test_resources_loaded_after_status_also_refresh_the_auth_row(window):
    """Resources arrive in their own thread, later than status."""
    win, controller = window
    controller.status = core.Status(state=core.ONLINE, network="diastasys")
    controller.resources = core.ResourceList()
    win.update(controller.status)
    before = win._auth_row.get_subtitle()

    controller.resources = core.ResourceList(
        items=[core.Resource("a", "1", auth_status="Auth expires in 2 days")]
    )
    win.update_resources(controller.resources)
    assert win._auth_row.get_subtitle() == "Auth expires in 2 days"
    assert win._auth_row.get_subtitle() != before
