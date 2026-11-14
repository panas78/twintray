"""TwinTray application: one poll loop, one controller, one tray icon.

Blocking work never happens on the main loop. Anything that shells out can hang
for seconds when the daemon is unreachable or a network call stalls, and a hung
main loop is indistinguishable from a crashed app. Every shell-out goes through
`run_async`, which runs the work on a thread and delivers the result back on the
main thread via `GLib.idle_add`.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, core
from .prefs import Prefs

# Seconds between resource refreshes while connected. Short enough that a
# resource dropping out of authentication is noticed while the user is still
# wondering why it stopped working; long enough not to hammer the controller.
RESOURCE_REFRESH = 15.0


def run_async(work: Callable, on_success=None, on_error=None) -> None:
    def runner() -> None:
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - delivered, not swallowed
            if on_error is not None:
                GLib.idle_add(on_error, exc)
            return
        if on_success is not None:
            GLib.idle_add(on_success, result)

    threading.Thread(target=runner, daemon=True).start()


class Controller:
    """Owns the last known state and the only path to changing it."""

    def __init__(self) -> None:
        self.prefs = Prefs.load()
        self.status = core.Status()
        self.resources = core.ResourceList()
        self.service_active = False
        self.tailscale_state = "unknown"
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ listeners

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # pragma: no cover - a bad listener must not stop the poll
                pass

    # --------------------------------------------------------------- reading

    def poll(self) -> core.Status:
        """Blocking snapshot. Call from a thread, or from the poll tick."""
        self.status = core.status(verbose=True)
        self.service_active = core.service_active()
        self.tailscale_state = core.tailscale_state()
        return self.status

    def load_resources(self) -> core.ResourceList:
        self.resources = core.resources()
        return self.resources

    def refresh_async(self, then=None) -> None:
        run_async(
            self.load_resources,
            on_success=lambda listing: self._after_resources(listing, then),
        )

    def authenticate_async(self, target: str = "", on_result=None) -> None:
        """Run the SSO round-trip off the main loop.

        `twingate auth` blocks until the browser hand-off completes (or times
        out at two minutes), so running it inline would freeze the icon and the
        window for the whole duration.
        """
        run_async(
            lambda: core.authenticate(target),
            on_success=lambda result: self._after_auth(result, on_result),
        )

    def _after_resources(self, listing: core.ResourceList, then) -> None:
        self._notify()
        if then is not None:
            then()

    # --------------------------------------------------------------- writing

    def toggle_async(self, on_result=None) -> None:
        if self.status.state in core.LIVE_STATES or self.service_active:
            self.disconnect_async(on_result)
        else:
            self.connect_async(on_result)

    def connect_async(self, on_result=None) -> None:
        coupled = self.prefs.tailscale_coupled()
        target = self.prefs.auth_target
        run_async(
            lambda: core.connect(auth_target=target, couple_tailscale=coupled),
            on_success=lambda result: self._after_action(result, on_result),
        )

    def disconnect_async(self, on_result=None) -> None:
        coupled = self.prefs.tailscale_coupled()
        run_async(
            lambda: core.disconnect(couple_tailscale=coupled),
            on_success=lambda result: self._after_action(result, on_result),
        )

    def _after_action(self, result: core.ActionResult, on_result) -> None:
        # Re-read immediately: the point of the action is the state it produced,
        # and a cached snapshot would report the state it replaced.
        self.poll()
        self._notify()
        if on_result is not None:
            on_result(result)

    def _after_auth(self, result: core.ActionResult, on_result) -> None:
        """Refresh the resources after an auth attempt, not just the status.

        Authentication changes the AUTH STATUS column, not `twingate status`,
        so re-polling the status alone would leave the icon amber and the
        pending row populated after a successful login.
        """
        self.poll()
        self.refresh_async()
        if on_result is not None:
            on_result(result)

    # ------------------------------------------------------------------ prefs

    def save_prefs(self, on_error, _previous=None) -> bool:
        try:
            self.prefs.save()
        except OSError as exc:
            if on_error is not None:
                on_error(f"Could not save settings: {exc.strerror or exc}")
            return False
        self._notify()
        return True


class TwinTrayApp(Adw.Application):
    def __init__(self, show_window: bool = False):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._show_on_activate = show_window
        self.controller = Controller()
        self.window = None
        self.tray = None
        self._timer_id = 0
        self._tray_available = False
        self._last_state = None
        self._resources_requested = False
        self._resources_loaded_at = 0.0

    # -------------------------------------------------------------- lifecycle

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)

        for name, handler in (
            ("quit", self._on_quit_action),
            ("about", self._on_about_action),
            ("show", self._on_show_action),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def do_command_line(self, command_line) -> int:
        self.activate()
        return 0

    def do_activate(self) -> None:
        # A GApplication with no window exits the moment activate returns. A
        # tray app must outlive that, so hold a reference for as long as the
        # icon is on the bar.
        self.hold()

        from .window import TwinTrayWindow
        from .tray import Tray

        if self.window is None:
            self.window = TwinTrayWindow(application=self, controller=self.controller)
            self.controller.subscribe(self._sync_ui)

        if self.tray is None:
            self.tray = Tray(
                on_toggle=lambda: self.controller.toggle_async(),
                on_open=self.show_window,
                on_quit=self.quit_app,
                on_auth=self.authenticate_async,
            )
            self._tray_available = self.tray.register(self.get_dbus_connection())

        self._sync_ui()
        self._start_polling()
        if self._show_on_activate:
            self.show_window()

    # ------------------------------------------------------------------- poll

    def _start_polling(self) -> None:
        if self._timer_id:
            return
        interval = max(2, int(self.controller.prefs.poll_seconds))
        self._timer_id = GLib.timeout_add_seconds(interval, self._on_tick)
        self._poll_now()

    def _poll_now(self) -> None:
        run_async(self.controller.poll, on_success=self._accept_status)

    def _on_tick(self) -> bool:
        self._poll_now()
        return True

    def _accept_status(self, status: core.Status) -> None:
        self._after_poll(status)

    def _after_poll(self, status: core.Status) -> None:
        self._sync_ui()
        self._notify_if_changed(status)
        self._maybe_load_resources(status)

    def _maybe_load_resources(self, status: core.Status) -> None:
        """Refresh resources while live, on a slower cadence than the poll.

        The Authentication row and the pending-auth indicator are both fed by
        the resources list, so one fetch per session is not enough: a resource
        can fall out of authentication *after* a successful connect, and that is
        exactly the case where the app must say so rather than keep showing
        green.

        Rate-limited because `twingate resources` is a network call and must not
        sit in front of every status update.
        """
        if not status.live:
            self._resources_requested = False
            self._resources_loaded_at = 0.0
            return

        now = time.monotonic()
        if self._resources_requested and now - self._resources_loaded_at < RESOURCE_REFRESH:
            return
        self._resources_requested = True
        self._resources_loaded_at = now
        self.controller.refresh_async(then=self._sync_ui)

    def _notify_if_changed(self, status: core.Status) -> None:
        previous, self._last_state = self._last_state, status.state
        if previous is None or previous == status.state:
            return
        if not self.controller.prefs.notify:
            return
        note = Gio.Notification.new(f"{APP_NAME}: {status.label}")
        note.set_body(status.network or status.error or "")
        note.set_priority(
            Gio.NotificationPriority.NORMAL
            if status.state in core.LIVE_STATES
            else Gio.NotificationPriority.LOW
        )
        try:
            self.send_notification(None, note)
        except GLib.Error:
            pass

    # --------------------------------------------------------------------- ui

    def _sync_ui(self) -> None:
        if self.window is not None:
            self.window.update(self.controller.status)
            self.window.update_resources(self.controller.resources)
        if self.tray is not None:
            self.tray.set_pending([r.name for r in self.controller.resources.pending])
            self.tray.set_status(self.controller.status)

    def show_window(self) -> None:
        if self.window is None:
            return
        self.window.set_visible(True)
        self.window.present()

    # ---------------------------------------------------------------- actions

    def _on_quit_action(self, *_args) -> None:
        self.quit_app()

    def _on_show_action(self, *_args) -> None:
        self.show_window()

    def _on_about_action(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID.lower(),
            version=VERSION,
            developer_name="Panagiotis Perivolaris",
            comments="A Twingate client front-end for the Linux system tray.",
        )
        about.present(self.window)

    def quit_app(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        if self.tray is not None:
            self.tray.unregister()
        if self.window is not None:
            # Bypass the close-request handler: that one hides instead of
            # closing, which is right for the desktop's close button and wrong
            # for Quit.
            self.window.set_visible(False)
        self.release()
        self.quit()


def main(show_window: bool = False) -> int:
    app = TwinTrayApp(show_window=show_window)
    return app.run([])
