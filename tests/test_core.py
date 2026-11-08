"""Core logic: parsing, state classification, prefs and autostart.

No display, no real service call — the subprocess runner is monkeypatched.
"""

import json

import pytest

from twintray import autostart, core, icons
from twintray.prefs import Prefs


# --------------------------------------------------------------- status parsing


def test_not_running_is_parsed():
    # Observed on this machine, verbatim.
    assert core.parse_status("not-running\n") == core.STOPPED


def test_every_known_state_round_trips():
    # The tokens come from the CLI's own state enum. Any new one must land in
    # KNOWN_STATES, otherwise it is classified UNKNOWN and the tray falls back
    # to the "stopped" rendering — a safe failure, but a silent one.
    for state in core.KNOWN_STATES:
        assert core.parse_status(state) == state


def test_surrounding_noise_does_not_break_parsing():
    assert core.parse_status("some banner\nonline\n") == core.ONLINE


def test_unknown_output_is_unknown_not_a_crash():
    assert core.parse_status("") == core.UNKNOWN
    assert core.parse_status("something entirely new") == core.UNKNOWN


def test_state_labels_cover_every_state():
    for state in (*core.KNOWN_STATES, core.UNKNOWN):
        assert core.STATE_LABELS[state]


def test_only_online_counts_as_live():
    for state in core.KNOWN_STATES:
        assert (state in core.LIVE_STATES) is (state == core.ONLINE)


# -------------------------------------------------------------- resource parsing


# Captured verbatim from `twingate resources -d` on Twingate 2026.302.7126.
# Tab-separated, four columns, header row, "-" meaning "no alias".
REAL_RESOURCES = (
    "RESOURCE NAME         \tADDRESS       \tALIAS\tAUTH STATUS\n"
    "*.diastasys.gr        \t*.diastasys.gr\t-    \t\n"
    "Diastasys Internal LAN\t10.10.0.0/24  \t-    \t\n"
    "\n"
)


def test_real_output_parses_without_the_header():
    items = core.parse_resources(REAL_RESOURCES)
    assert [i.name for i in items] == ["*.diastasys.gr", "Diastasys Internal LAN"]
    assert [i.address for i in items] == ["*.diastasys.gr", "10.10.0.0/24"]
    # "-" is the CLI's "none", not an alias called dash.
    assert [i.alias for i in items] == ["", ""]


def test_header_rows_are_never_resources():
    # The previous parser split on 2+ spaces, so this header became a resource
    # named "RESOURCE NAME" and glued ALIAS onto the address.
    for header in ("RESOURCE NAME", "MAIN RESOURCES", "BACKGROUND RESOURCES"):
        assert core.parse_resources(header + "\n") == [], header


def test_auth_status_column_is_captured():
    text = (
        "RESOURCE NAME\tADDRESS\tALIAS\tAUTH STATUS\n"
        "BC Prod\t10.0.0.5\t-\tAuth expires in 6 days\n"
        "NAS\t10.0.0.9\t-\t\n"
    )
    items = core.parse_resources(text)
    assert items[0].auth_status == "Auth expires in 6 days"
    assert items[1].auth_status == ""


def test_names_keep_their_spaces():
    # "Diastasys Internal LAN" is two words and one column.
    items = core.parse_resources(REAL_RESOURCES)
    assert any(i.name == "Diastasys Internal LAN" for i in items)


def test_non_tab_fallback_still_parses_two_fields():
    items = core.parse_resources("router.internal 10.0.0.1\n")
    assert items[0].name == "router.internal"
    assert items[0].address == "10.0.0.1"


def test_blank_lines_are_skipped():
    assert core.parse_resources("\n\n  \n") == []


def test_name_without_address_is_still_a_row():
    items = core.parse_resources("lonely\n")
    assert items[0].name == "lonely"
    assert items[0].address == ""


def test_auth_summary_reports_what_the_client_says():
    empty = core.ResourceList(items=core.parse_resources(REAL_RESOURCES))
    assert empty.auth_summary == "not required by any resource"

    pending = core.ResourceList(
        items=[
            core.Resource("a", "1", auth_status="Auth expires in 6 days"),
            core.Resource("b", "2"),
        ]
    )
    assert pending.auth_summary == "Auth expires in 6 days"

    assert core.ResourceList().auth_summary == "no resources"
    assert core.ResourceList(error="not connected").auth_summary == "not connected"


# ----------------------------------------------------------------------- status


def test_status_captures_network_and_expiry(monkeypatch):
    text = "online\nconnected to diastasys\nAuth expires in 6 days\n"
    monkeypatch.setattr(core, "run", lambda *a, **k: core.Run(True, 0, text, ""))
    current = core.status()
    assert current.state == core.ONLINE
    assert current.network == "diastasys"
    assert current.auth_expires == "6 days"
    assert current.live is True


def test_network_falls_back_to_the_client_config(monkeypatch, tmp_path):
    """`status -v` prints only "Online: User" — it never names the network."""
    conf = tmp_path / "network.conf"
    conf.write_text("diastasys")
    monkeypatch.setattr(core, "NETWORK_CONF", str(conf))
    monkeypatch.setattr(core, "run", lambda *a, **k: core.Run(True, 0, "Online: User\n", ""))
    assert core.status().network == "diastasys"


def test_network_is_empty_when_the_config_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "NETWORK_CONF", str(tmp_path / "absent.conf"))
    monkeypatch.setattr(core, "run", lambda *a, **k: core.Run(True, 0, "online\n", ""))
    assert core.status().network == ""


def test_real_verbose_output_is_recognised(monkeypatch):
    # Verbatim: the entire verbose output on this client version.
    monkeypatch.setattr(core, "network_name", lambda: "diastasys")
    monkeypatch.setattr(core, "run", lambda *a, **k: core.Run(True, 0, "Online: User\n", ""))
    current = core.status()
    assert current.state == core.ONLINE
    assert current.live is True


def test_status_carries_the_error_but_still_reports_a_state(monkeypatch):
    monkeypatch.setattr(
        core, "run", lambda *a, **k: core.Run(False, 1, "", "connection refused")
    )
    current = core.status()
    assert current.error == "connection refused"
    assert current.to_dict()["state"] == core.UNKNOWN


# --------------------------------------------------------------- service actions


def test_connect_reports_a_failed_service_start(monkeypatch):
    monkeypatch.setattr(core, "start_service", lambda **k: core.Run(False, 1, "", "denied"))
    result = core.connect()
    assert result.ok is False
    assert "denied" in result.message
    assert result.steps


def test_connect_drops_tailscale_after_the_service_starts(monkeypatch):
    order = []
    monkeypatch.setattr(
        core, "start_service", lambda **k: (order.append("start"), core.Run(True, 0))[1]
    )
    monkeypatch.setattr(
        core, "tailscale_down", lambda: (order.append("ts-down"), core.Run(True, 0))[1]
    )
    monkeypatch.setattr(core, "tailscale_available", lambda: True)
    monkeypatch.setattr(
        core, "auth_flow", lambda t: (order.append("auth"), core.Run(True, 0))[1]
    )
    result = core.connect(auth_target="*.diastasys.gr", couple_tailscale=True)
    assert result.ok
    # Same order as tg-start: daemon up, Tailscale down, then SSO.
    assert order == ["start", "ts-down", "auth"]


def test_connect_skips_auth_with_an_empty_target(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "start_service", lambda **k: core.Run(True, 0))
    monkeypatch.setattr(core, "auth_flow", lambda t: calls.append(t) or core.Run(True, 0))
    core.connect(auth_target="   ", couple_tailscale=False)
    assert calls == []


def test_connect_restores_tailscale_after_a_failed_start(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "start_service", lambda **k: core.Run(False, 1, "", "denied"))
    monkeypatch.setattr(core, "tailscale_down", lambda: calls.append("down") or core.Run(True, 0))
    result = core.connect(couple_tailscale=True)
    assert result.ok is False
    # A failed start must not have stood Tailscale down.
    assert calls == []


def test_disconnect_restores_tailscale(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "stop_service", lambda **k: core.Run(True, 0))
    monkeypatch.setattr(core, "tailscale_up", lambda: calls.append("up") or core.Run(True, 0))
    monkeypatch.setattr(core, "tailscale_available", lambda: True)
    result = core.disconnect(couple_tailscale=True)
    assert result.ok
    assert calls == ["up"]


def test_polkit_probe_is_a_noop_for_the_current_state(monkeypatch):
    """The probe must never change anything: stop when stopped, start when up."""
    issued = []

    def fake_run(args, timeout=30):
        issued.append(list(args))
        return core.Run(True, 0)

    monkeypatch.setattr(
        core, "run", lambda args, timeout=30: fake_run(args, timeout)
    )
    monkeypatch.setattr(core, "unit_state", lambda: "inactive")
    assert core.polkit_authorized() is True
    assert issued[-1] == ["systemctl", "stop", core.SYSTEMD_UNIT]

    issued.clear()
    monkeypatch.setattr(core, "unit_state", lambda: "active")
    assert core.polkit_authorized() is True
    assert issued[-1] == ["systemctl", "start", core.SYSTEMD_UNIT]


def test_polkit_probe_refuses_when_no_verb_is_a_noop(monkeypatch):
    issued = []
    monkeypatch.setattr(core, "run", lambda args, **k: issued.append(args) or core.Run(True, 0))
    monkeypatch.setattr(core, "unit_state", lambda: "activating")
    assert core.polkit_authorized() is None
    assert issued == []


def test_polkit_probe_reports_denial(monkeypatch):
    monkeypatch.setattr(core, "unit_state", lambda: "inactive")
    monkeypatch.setattr(core, "run", lambda args, **k: core.Run(False, 1, "", "denied"))
    assert core.polkit_authorized() is False


# ------------------------------------------------------------------------ prefs


def test_prefs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Prefs(auth_target="*.example.gr", notify=False).save()
    loaded = Prefs.load()
    assert loaded.auth_target == "*.example.gr"
    assert loaded.notify is False


def test_prefs_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Prefs().save()
    mode = (tmp_path / "twintray" / "prefs.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_missing_prefs_file_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    assert Prefs.load().poll_seconds == 5


def test_corrupt_prefs_file_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "twintray" / "prefs.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert Prefs.load().auth_target == ""


def test_poll_seconds_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "twintray" / "prefs.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"poll_seconds": 99999}))
    assert Prefs.load().poll_seconds == 300


def test_tailscale_coupling_resolves_when_unset(monkeypatch):
    monkeypatch.setattr(core, "tailscale_available", lambda: True)
    assert Prefs(manage_tailscale=None).tailscale_coupled() is True
    monkeypatch.setattr(core, "tailscale_available", lambda: False)
    assert Prefs(manage_tailscale=None).tailscale_coupled() is False
    assert Prefs(manage_tailscale=True).tailscale_coupled() is True


# -------------------------------------------------------------------- autostart


def test_autostart_enable_disable_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart.is_enabled() is False

    path = autostart.enable(exec_path="/opt/x/bin/twintray")
    assert path.is_file()
    assert autostart.is_enabled() is True
    text = path.read_text()
    assert "Exec=/opt/x/bin/twintray tray" in text
    assert "Name=TwinTray" in text

    autostart.disable()
    # The file must survive: deleting it would re-expose a system-wide entry.
    assert path.is_file()
    assert autostart.is_enabled() is False
    assert "Hidden=true" in path.read_text()


def test_autostart_name_is_built_from_the_constant(tmp_path, monkeypatch):
    from twintray import APP_NAME

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    text = autostart.render(exec_path="/x")
    assert f"Name={APP_NAME} (tray)" in text


def test_disable_on_missing_entry_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    autostart.disable()
    assert autostart.entry_path().exists() is False


# ------------------------------------------------------------------------- icons


def test_every_state_has_a_distinct_colour():
    # Two states sharing a colour means the icon stops conveying the case that
    # most needs attention.
    colours = list(icons.STATE_COLORS.values())
    assert len(set(colours)) == len(colours)
    for state in core.KNOWN_STATES:
        assert state in icons.STATE_COLORS


def test_online_has_no_state_dot():
    # A healthy connection gets the untouched mark.
    assert icons.ONLINE not in icons.DOT_STATES
    plain = icons.surface_to_pixmap(icons.tray_surface(icons.ONLINE, 22))[2]
    for state in icons.DOT_STATES:
        drawn = icons.surface_to_pixmap(icons.tray_surface(state, 22))[2]
        assert drawn != plain, state


def test_unknown_state_renders_as_stopped():
    unknown = icons.surface_to_pixmap(icons.tray_surface("brand-new-state", 22))[2]
    stopped = icons.surface_to_pixmap(icons.tray_surface(icons.STOPPED, 22))[2]
    assert unknown == stopped


def test_dot_centre_pixel_uses_the_state_colour():
    for state in icons.DOT_STATES:
        surface = icons.tray_surface(state, 22)
        width, height, data = icons.surface_to_pixmap(surface)
        # Dot centre, per _draw_state_dot.
        centre = int(22 * (1 - icons.DOT_OUTER - 0.05))
        offset = (centre * width + centre) * 4
        alpha, red, green, blue = data[offset : offset + 4]
        assert alpha == 255, state
        expected = icons.STATE_COLORS[state]
        got = (red / 255, green / 255, blue / 255)
        assert got == pytest.approx(expected, abs=0.03), state


def test_pixmap_is_tightly_packed():
    width, height, data = icons.tray_pixmap(icons.ONLINE, 22)[0]
    assert (width, height) == (22, 22)
    assert len(data) == width * height * 4
    assert max(data[0::4]) > 0  # something was actually drawn


def test_pixmap_is_always_tightly_packed():
    # Cairo's ARGB32 stride is already width*4 (4 bytes per pixel is inherently
    # 4-byte aligned), so `surface_to_pixmap`'s row loop does not actually have
    # padding to strip *for cairo surfaces*. It stays because the contract it
    # guarantees — a buffer of exactly w*h*4 — is what D-Bus requires, and a
    # surface arriving from any other producer may not be so tidy.
    for size in (5, 7, 22, 23, 25, 33):
        surface = icons.tray_surface(icons.ONLINE, size)
        width, height, data = icons.surface_to_pixmap(surface)
        assert (width, height) == (size, size)
        assert len(data) == width * height * 4


def test_plates_differ_from_tray_rendering():
    tray = icons.surface_to_pixmap(icons.tray_surface(icons.ONLINE, 48))[2]
    plate = icons.surface_to_pixmap(icons.plate_surface(icons.ONLINE, 48))[2]
    assert tray != plate
