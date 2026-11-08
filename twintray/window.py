"""The TwinTray window: three tabs over one view stack.

Rows are **updated in place**, never detached and re-appended. The alternative —
tearing the page down and rebuilding it on every poll — is the bug that eats
this class of app: `Adw.PreferencesPage.add()` parents your widget to an
internal box, so `widget.get_parent() is page` is always False and a "only
remove it if it is really there" guard silently removes nothing, stacking a
fresh copy of every section on every tick.
"""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk, GLib, Pango  # noqa: E402,F401

from . import APP_NAME, VERSION, core, icons
from . import autostart as autostart_mod


def _dot_factory(state: str, size: int = 16):
    """A draw callback for a filled state dot, in that state's colour.

    Drawn rather than themed: a themed state icon picks up the icon theme's own
    colours and is ambiguous on a dark bar, whereas this is exactly one colour,
    always.
    """
    colour = icons.STATE_COLORS[icons.canonical(state)]

    def draw(_area, cr, width, height):
        radius = min(width, height) / 2.0 - 1.0
        cr.set_source_rgb(*colour)
        cr.arc(width / 2.0, height / 2.0, radius, 0, 2 * math.pi)
        cr.fill()

    return draw


def _dot_area(state: str, size: int = 16) -> Gtk.DrawingArea:
    area = Gtk.DrawingArea(content_width=size, content_height=size)
    area.set_valign(Gtk.Align.CENTER)
    area.set_draw_func(_dot_factory(state, size))
    return area


class TwinTrayWindow(Adw.ApplicationWindow):
    def __init__(self, application, controller):
        super().__init__(application=application, title=APP_NAME)
        self.controller = controller
        self.set_default_size(520, 660)

        self._stack = Adw.ViewStack()
        self._switcher_title = Adw.ViewSwitcherTitle(title=APP_NAME)
        # Set the property rather than calling set_stack(): the method is
        # deprecated in favour of the property in libadwaita 1.10, and the
        # warning fires on every construction.
        self._switcher_title.set_property("stack", self._stack)

        header = Adw.HeaderBar()
        header.set_title_widget(self._switcher_title)

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(self._build_menu())
        header.pack_end(menu_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._stack)

        self._switcher_bar = Adw.ViewSwitcherBar()
        self._switcher_bar.set_property("stack", self._stack)
        toolbar.add_bottom_bar(self._switcher_bar)
        self._switcher_title.connect(
            "notify::title-visible",
            lambda *_: self._switcher_bar.set_reveal(
                self._switcher_title.get_title_visible()
            ),
        )

        self._toasts = Adw.ToastOverlay()
        self._toasts.set_child(toolbar)
        self.set_content(self._toasts)

        self._build_connection_page()
        self._build_resources_page()
        self._build_options_page()

        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------- menu

    def _build_menu(self):
        menu = Gio.Menu()
        about = Gio.Menu()
        about.append(
            APP_NAME, f"app.{self.get_application().get_application_id()}.about"
        )
        menu.append_section(None, about)
        quit_section = Gio.Menu()
        quit_section.append("Quit", "app.quit")
        menu.append_section(None, quit_section)
        return menu

    # ------------------------------------------------------------- page: status

    def _build_connection_page(self) -> None:
        page = Adw.PreferencesPage()
        self._connection_page = page

        group = Adw.PreferencesGroup(title="Twingate")
        self._status_row = Adw.ActionRow(title="Reading status…")
        self._status_dot = _dot_area(core.UNKNOWN, 18)
        self._status_row.add_prefix(self._status_dot)
        self._status_row.set_activatable(False)
        group.add(self._status_row)

        self._network_row = Adw.ActionRow(title="Network", subtitle="—")
        self._network_row.set_activatable(False)
        group.add(self._network_row)

        self._auth_row = Adw.ActionRow(title="Authentication", subtitle="—")
        self._auth_row.set_activatable(False)
        group.add(self._auth_row)

        self._service_row = Adw.ActionRow(title="Background service", subtitle="—")
        self._service_row.set_activatable(False)
        group.add(self._service_row)
        page.add(group)

        actions = Adw.PreferencesGroup()
        self._toggle_button = Gtk.Button(label="Connect")
        self._toggle_button.add_css_class("suggested-action")
        self._toggle_button.add_css_class("pill")
        self._toggle_button.set_margin_top(6)
        self._toggle_button.set_margin_bottom(6)
        self._toggle_button.connect("clicked", self._on_toggle_clicked)
        actions.add(self._toggle_button)
        self._action_steps = Gtk.Label(label="")
        self._action_steps.set_wrap(True)
        self._action_steps.set_xalign(0)
        self._action_steps.set_selectable(True)
        self._action_steps.add_css_class("dim-label")
        self._action_steps.add_css_class("caption")
        actions.add(self._action_steps)
        page.add(actions)

        coupling = Adw.PreferencesGroup(
            title="Interface coupling",
            description="Twingate and Tailscale both claim 100.96.12.0/12, so one must stand down.",
        )
        self._tailscale_row = Adw.ActionRow(title="Tailscale", subtitle="—")
        self._tailscale_row.set_activatable(False)
        coupling.add(self._tailscale_row)
        page.add(coupling)

        self._stack.add_titled_with_icon(
            page, "connection", "Connection", "network-vpn-symbolic"
        )

    # ---------------------------------------------------------- page: resources

    def _build_resources_page(self) -> None:
        page = Adw.PreferencesPage()
        self._resources_page = page

        group = Adw.PreferencesGroup(
            title="Authorized resources",
            description="Read from the Twingate client. Requires the service to be running.",
        )
        self._resources_list = Gtk.ListBox()
        self._resources_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._resources_list.add_css_class("boxed-list")
        group.add(self._resources_list)

        self._resources_empty = Adw.ActionRow(title="No resources loaded")
        self._resources_empty.set_activatable(False)
        group.add(self._resources_empty)

        refresh = Gtk.Button(label="Refresh")
        refresh.set_halign(Gtk.Align.CENTER)
        refresh.set_margin_top(6)
        refresh.connect("clicked", lambda *_: self.controller.refresh_async())
        group.add(refresh)

        page.add(group)
        self._stack.add_titled_with_icon(
            page, "resources", "Resources", "network-server-symbolic"
        )

    # ------------------------------------------------------------ page: options

    def _build_options_page(self) -> None:
        page = Adw.PreferencesPage()
        self._options_page = page
        self._syncing = False
        self._auth_save_id = 0
        prefs = self.controller.prefs

        behaviour = Adw.PreferencesGroup(title="Behaviour")

        self._auth_target_row = Adw.EntryRow(title="Auth target after connect")
        self._auth_target_row.set_text(prefs.auth_target)
        self._auth_target_row.set_tooltip_text(
            "Resource pattern handed to `twingate auth` once the service is up. "
            "Leave empty to skip."
        )
        self._auth_target_row.connect("changed", self._on_auth_target_changed)
        behaviour.add(self._auth_target_row)

        self._tailscale_switch = Adw.SwitchRow(
            title="Manage Tailscale",
            subtitle="Drop Tailscale while Twingate is up, restore it after",
        )
        self._tailscale_switch.set_active(prefs.tailscale_coupled())
        self._tailscale_switch.connect("notify::active", self._on_tailscale_switch)
        behaviour.add(self._tailscale_switch)

        self._notify_switch = Adw.SwitchRow(
            title="Notifications",
            subtitle="Announce connection changes",
        )
        self._notify_switch.set_active(prefs.notify)
        self._notify_switch.connect("notify::active", self._on_notify_switch)
        behaviour.add(self._notify_switch)

        self._autostart_switch = Adw.SwitchRow(
            title="Start at login",
            subtitle="Run the tray icon when you sign in",
        )
        self._autostart_switch.set_active(autostart_mod.is_enabled())
        self._autostart_switch.connect("notify::active", self._on_autostart_switch)
        behaviour.add(self._autostart_switch)

        page.add(behaviour)

        diagnostics = Adw.PreferencesGroup(
            title="Diagnostics",
            description="What the app needs on this machine and whether it is there.",
        )
        self._doctor_row = Adw.ActionRow(title="Run doctor")
        self._doctor_row.set_subtitle("Checks the CLI, service, polkit and toolkit")
        self._doctor_row.set_activatable(True)
        self._doctor_row.connect("activated", self._on_doctor)
        diagnostics.add(self._doctor_row)
        page.add(diagnostics)

        about = Adw.PreferencesGroup()
        about_row = Adw.ActionRow(
            title=APP_NAME,
            subtitle=f"Version {VERSION} · Twingate client front-end",
        )
        about_row.set_activatable(False)
        about.add(about_row)
        page.add(about)

        self._stack.add_titled_with_icon(
            page, "options", "Options", "emblem-system-symbolic"
        )

    # ---------------------------------------------------------------- updating

    def update(self, status: core.Status) -> None:
        """Re-sync every row from one snapshot. Never re-parents anything."""
        label = status.label
        detail = status.network or status.error
        self._status_row.set_title(label)
        self._status_row.set_subtitle(detail or core.STATE_LABELS.get(status.state, ""))
        self._status_dot.set_draw_func(
            _dot_factory(status.state, 18)
        )
        self._status_dot.queue_draw()

        self._network_row.set_subtitle(status.network or "not configured")
        self._update_auth_row()

        active = self.controller.service_active
        self._service_row.set_subtitle("running" if active else "stopped")

        live = status.state in core.LIVE_STATES
        self._toggle_button.set_label("Disconnect" if live else "Connect")
        for css in ("suggested-action", "destructive-action"):
            self._toggle_button.remove_css_class(css)
        self._toggle_button.add_css_class(
            "destructive-action" if live else "suggested-action"
        )

        ts_state = self.controller.tailscale_state
        if ts_state == "absent":
            self._tailscale_row.set_subtitle("not installed")
        else:
            coupled = self.controller.prefs.tailscale_coupled()
            self._tailscale_row.set_subtitle(
                f"{ts_state}{' · managed by TwinTray' if coupled else ''}"
            )

    def _update_auth_row(self) -> None:
        """Authentication comes from two places, and neither is `status -v`.

        A per-session expiry only appears if the client ever starts reporting
        one; otherwise the honest source is the resources list's AUTH STATUS
        column. While stopped there is nothing to report, so say that rather
        than leaving a dash.
        """
        status = self.controller.status
        listing = self.controller.resources

        if status.auth_expires:
            self._auth_row.set_subtitle(status.auth_expires)
            return
        if status.state in core.LIVE_STATES:
            self._auth_row.set_subtitle(listing.auth_summary)
        elif listing.items:
            self._auth_row.set_subtitle("unavailable while stopped")
        else:
            self._auth_row.set_subtitle("not checked — connect to read it")

    def update_resources(self, listing: core.ResourceList) -> None:
        while child := self._resources_list.get_first_child():
            self._resources_list.remove(child)

        items = listing.items
        if not items:
            self._resources_empty.set_visible(True)
            self._resources_empty.set_title(
                listing.error or "No resources — is the service running?"
            )
            self._update_auth_row()
            return
        self._resources_empty.set_visible(False)
        for resource in items:
            row = Adw.ActionRow(title=resource.name, subtitle=resource.address or "—")
            if resource.auth_status:
                row.add_suffix(Gtk.Label(label=resource.auth_status))
            row.set_activatable(False)
            self._resources_list.append(row)
        self._update_auth_row()

    def set_action_steps(self, steps: list[str]) -> None:
        self._action_steps.set_label("\n".join(steps))

    # ---------------------------------------------------------------- handlers

    def _on_toggle_clicked(self, _button) -> None:
        self.controller.toggle_async()

    def _on_auth_target_changed(self, row) -> None:
        if self._syncing:
            return
        self.controller.prefs.auth_target = row.get_text().strip()
        # Debounce: `Adw.EntryRow` emits `changed` more than once per edit (and
        # again on every programmatic set), and writing prefs.json per keystroke
        # is a pointless fsync storm. One write, 400 ms after the last edit.
        if self._auth_save_id:
            GLib.source_remove(self._auth_save_id)
        self._auth_save_id = GLib.timeout_add(400, self._flush_auth_target)

    def _flush_auth_target(self) -> bool:
        self._auth_save_id = 0
        self.controller.save_prefs(self.toast)
        return False  # one-shot

    def _on_tailscale_switch(self, row, _pspec) -> None:
        if self._syncing:
            return
        previous = self.controller.prefs.manage_tailscale
        self.controller.prefs.manage_tailscale = row.get_active()
        if not self.controller.save_prefs(self._revert_tailscale, previous):
            return

    def _revert_tailscale(self, message: str) -> None:
        self._syncing = True
        try:
            self._tailscale_switch.set_active(
                self.controller.prefs.tailscale_coupled()
            )
        finally:
            self._syncing = False
        self.toast(message)

    def _on_notify_switch(self, row, _pspec) -> None:
        if self._syncing:
            return
        self.controller.prefs.notify = row.get_active()
        self.controller.save_prefs(self.toast)

    def _on_autostart_switch(self, row, _pspec) -> None:
        if self._syncing:
            return
        wanted = row.get_active()
        try:
            autostart_mod.set_enabled(wanted)
        except OSError as exc:
            self._syncing = True
            try:
                row.set_active(not wanted)
            finally:
                self._syncing = False
            self.toast(f"Could not update start-at-login: {exc.strerror or exc}")
            return
        self.controller.prefs.start_at_login = wanted
        self.controller.save_prefs(self.toast)

    def _on_doctor(self, _row) -> None:
        checks = core.doctor()
        lines = []
        for name, ok, detail in checks:
            flag = "ok" if ok else ("?" if ok is None else "FAIL")
            lines.append(f"[{flag}] {name}: {detail}")
        dialog = Adw.AlertDialog(
            heading="Doctor",
            body="\n".join(lines),
        )
        dialog.add_response("close", "Close")
        dialog.present(self)

    def toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, timeout=4))

    def _on_close_request(self, *_args) -> bool:
        self.set_visible(False)
        return True
