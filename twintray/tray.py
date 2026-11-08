"""StatusNotifierItem + DBusMenu tray icon, written directly against Gio.DBus.

No pystray, no libappindicator: pystray wants GTK3/X11 and does not speak SNI on
Wayland, and libappindicator adds a C dependency. Both protocols are small and
are implemented here in full.

The menu is deliberately three entries and its *shape never changes*: the
connect/disconnect label flips, no row ever appears or disappears. A menu whose
structure tracks state is a second copy of the window that has to be maintained
twice — and the copy that gets forgotten.
"""

from __future__ import annotations


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gio, GLib  # noqa: E402

from . import APP_NAME
from . import core, icons

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

ID_TOGGLE = 1
ID_SEPARATOR = 2
ID_OPEN = 3
ID_QUIT = 4

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewTitle"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""


class MenuItem:
    """A dbusmenu node.

    `tree()` returns plain tuples and `variant()` wraps them, because PyGObject
    cannot nest an already-built GLib.Variant inside a tuple — that raises
    `TypeError: Expected GLib.Variant, but got tuple` from a place that points
    at the wrong argument.
    """

    def __init__(self, item_id: int, label: str = "", children=None, kind: str = "standard", enabled: bool = True):
        self.id = item_id
        self.label = label
        self.children = children or []
        self.kind = kind
        self.enabled = enabled

    def props(self) -> dict:
        properties: dict[str, GLib.Variant] = {}
        if self.kind == "separator":
            properties["type"] = GLib.Variant("s", "separator")
            return properties
        properties["label"] = GLib.Variant("s", self.label)
        properties["enabled"] = GLib.Variant("b", self.enabled)
        properties["visible"] = GLib.Variant("b", True)
        return properties

    def tree(self):
        return (self.id, self.props(), [child.variant() for child in self.children])

    def variant(self):
        return GLib.Variant("(ia{sv}av)", self.tree())


class Tray:
    """Registers one SNI item and its menu, and repaints on state changes."""

    def __init__(self, on_toggle, on_open, on_quit, title: str | None = None):
        self._on_toggle = on_toggle
        self._on_open = on_open
        self._on_quit = on_quit
        self._title = title or APP_NAME
        self._status = core.STOPPED
        self._detail = ""
        self._revision = 1
        self._pixmap = icons.tray_pixmap(core.STOPPED, 22)
        self._connection: Gio.DBusConnection | None = None
        self._registrations: list[int] = []
        self._owner_id = 0
        self._bus_name = f"org.kde.StatusNotifierItem-{__import__('os').getpid()}-1"

    # ------------------------------------------------------------- properties

    def _status_string(self) -> str:
        """Always "Active" — never "Passive".

        The SNI spec lets a host hide a Passive item ("implementation detail"),
        and Noctalia does exactly that: with `Status` derived from the tunnel
        state, a disconnected TwinTray vanished from the bar entirely. Verified
        against the running bar — every visible item (`krbctl`, `solaar`,
        chrome) reported Active, and ours was the only Passive one and the only
        one missing.

        That failure mode is backwards for a VPN: "disconnected" is precisely
        when the user needs to click it. State is carried by the icon's corner
        dot, not by whether the icon exists.
        """
        return "Active"

    def _tooltip_text(self) -> tuple[str, str]:
        label = core.STATE_LABELS.get(self._status, core.STATE_LABELS[core.UNKNOWN])
        subtitle = self._detail or f"Twingate: {self._status}"
        return f"{APP_NAME} — {label}", subtitle

    def _prop_value(self, name: str):
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", "twintray")
        if name == "Title":
            return GLib.Variant("s", self._title)
        if name == "Status":
            return GLib.Variant("s", self._status_string())
        # IconName MUST stay empty. Any theme name here makes the host draw that
        # glyph and ignore IconPixmap entirely, which silently throws away both
        # the mark and the state dot.
        if name == "IconName":
            return GLib.Variant("s", "")
        if name == "OverlayIconName":
            return GLib.Variant("s", "")
        if name == "AttentionIconName":
            return GLib.Variant("s", "")
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", self._pixmap)
        if name == "ToolTip":
            title, subtitle = self._tooltip_text()
            return GLib.Variant("(sa(iiay)ss)", ("", [], title, subtitle))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return None

    # ---------------------------------------------------------------- signals

    def _emit(self, interface: str, path: str, name: str, args=None) -> None:
        if self._connection is None:
            return
        try:
            self._connection.emit_signal(None, path, interface, name, args)
        except GLib.Error:
            pass

    def _notify_icon(self) -> None:
        self._emit("org.kde.StatusNotifierItem", ITEM_PATH, "NewIcon")
        self._emit("org.kde.StatusNotifierItem", ITEM_PATH, "NewToolTip")
        self._emit("org.kde.StatusNotifierItem", ITEM_PATH, "NewTitle")
        self._emit(
            "org.kde.StatusNotifierItem",
            ITEM_PATH,
            "NewStatus",
            GLib.Variant("(s)", (self._status_string(),)),
        )

    # ------------------------------------------------------------------- menu

    def menu_root(self) -> MenuItem:
        toggle = MenuItem(
            ID_TOGGLE,
            "Disconnect" if self._status in core.LIVE_STATES else "Connect",
        )
        return MenuItem(0, "", [toggle, MenuItem(ID_SEPARATOR, kind="separator"), MenuItem(ID_OPEN, f"Open {APP_NAME}"), MenuItem(ID_QUIT, "Quit")])

    def _menu_prop_value(self, name: str):
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    # -------------------------------------------------------------- handlers

    def _on_sni_method(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "Activate":
                self._on_open()
            elif method == "SecondaryActivate":
                self._on_toggle()
            elif method == "ContextMenu":
                pass
            elif method == "Scroll":
                pass
            invocation.return_value(None)
        except Exception:  # pragma: no cover - never kill the bus handler
            invocation.return_value(None)

    def _on_sni_get(self, conn, sender, path, iface, prop, *_extra):
        # The 6th argument is absent for property reads — a named `_data`
        # parameter raises TypeError on every read, which the host retries in a
        # tight loop and buries the real error under thousands of tracebacks.
        value = self._prop_value(prop)
        if value is None:
            return None
        return value

    def _on_menu_method(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "GetLayout":
                root = self.menu_root()
                invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, root.tree())))
            elif method == "GetGroupProperties":
                ids = list(params.unpack()[0])
                root = self.menu_root()
                table = {root.id: root}
                for child in root.children:
                    table[child.id] = child
                rows = [(i, table[i].props()) for i in ids if i in table]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
            elif method == "GetProperty":
                item_id, name = params.unpack()
                root = self.menu_root()
                table = {root.id: root}
                for child in root.children:
                    table[child.id] = child
                item = table.get(item_id)
                if item is None:
                    invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
                else:
                    props = item.props()
                    value = props.get(name, GLib.Variant("s", ""))
                    invocation.return_value(GLib.Variant("(v)", (value,)))
            elif method == "Event":
                item_id, event_id, _data, _timestamp = params.unpack()
                if event_id == "clicked":
                    self._dispatch(item_id)
                invocation.return_value(None)
            elif method == "AboutToShow":
                invocation.return_value(GLib.Variant("(b)", (False,)))
            else:
                invocation.return_value(None)
        except Exception:  # pragma: no cover
            invocation.return_value(None)

    def _dispatch(self, item_id: int) -> None:
        if item_id == ID_TOGGLE:
            self._on_toggle()
        elif item_id == ID_OPEN:
            self._on_open()
        elif item_id == ID_QUIT:
            self._on_quit()

    def _on_menu_get(self, conn, sender, path, iface, prop, *_extra):
        return self._menu_prop_value(prop)

    # ------------------------------------------------------------- lifecycle

    def register(self, connection: Gio.DBusConnection) -> bool:
        self._connection = connection
        try:
            info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
            self._registrations.append(
                connection.register_object(ITEM_PATH, info.interfaces[0], self._on_sni_method, self._on_sni_get, None)
            )
            menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
            self._registrations.append(
                connection.register_object(MENU_PATH, menu_info.interfaces[0], self._on_menu_method, self._on_menu_get, None)
            )
        except GLib.Error:
            return False

        self._owner_id = Gio.bus_own_name_on_connection(
            connection, self._bus_name, Gio.BusNameOwnerFlags.NONE, None, None
        )

        try:
            connection.call_sync(
                WATCHER,
                WATCHER_PATH,
                WATCHER,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error:
            # No watcher means no bar to draw into. The caller keeps the window
            # usable and stays quittable from the menu it does not have — so it
            # must not treat this as fatal, only as "no icon".
            return False
        return True

    def unregister(self) -> None:
        if self._connection is None:
            return
        for registration in self._registrations:
            try:
                self._connection.unregister_object(registration)
            except GLib.Error:
                pass
        self._registrations.clear()
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0

    def set_status(self, status: core.Status) -> None:
        self._status = status.state
        self._detail = status.network or status.error or ""
        self._pixmap = icons.tray_pixmap(status.state, 22)
        self._revision += 1
        self._notify_icon()
        self._emit(
            "com.canonical.dbusmenu",
            MENU_PATH,
            "LayoutUpdated",
            GLib.Variant("(ui)", (self._revision, 0)),
        )
