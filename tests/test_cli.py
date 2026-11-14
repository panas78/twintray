"""CLI surface: output shape, exit codes, and that the toolkit is never imported."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from twintray import cli, core


def test_status_json_shape(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "status",
        lambda verbose=True: core.Status(
            state=core.ONLINE, network="diastasys", auth_expires="6 days"
        ),
    )
    # status now reports which resources are locked, so pin that too instead of
    # shelling out to the real client from a unit test.
    monkeypatch.setattr(
        core,
        "resources",
        lambda include_hidden=False: core.ResourceList(
            items=[core.Resource("BC Prod SQL", "10.0.0.5")]
        ),
    )
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "state": "online",
        "label": "Connected",
        "live": True,
        "network": "diastasys",
        "auth_expires": "6 days",
        "lines": [],
        "error": "",
        "pending_auth": [],
    }


def test_status_unknown_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(core, "status", lambda verbose=True: core.Status(state=core.UNKNOWN))
    assert cli.main(["status"]) == 1


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["status"], True),          # default: verbose
        (["status", "-v"], True),    # twingate's own spelling
        (["status", "--verbose"], True),
        (["status", "-b"], False),   # brief
        (["status", "--brief"], False),
    ],
)
def test_status_verbosity_flags(monkeypatch, argv, expected):
    """`-v` is what fingers type, having been trained by `twingate status -v`."""
    seen = {}
    monkeypatch.setattr(
        core, "status", lambda verbose=True: seen.update(v=verbose) or core.Status(state=core.ONLINE)
    )
    assert cli.main(argv) == 0
    assert seen["v"] is expected, argv


def test_resources_prints_aligned_rows(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "resources",
        lambda include_hidden=False: core.ResourceList(
            items=[core.Resource("BC Prod", "10.0.0.5"), core.Resource("NAS", "10.0.0.9")]
        ),
    )
    assert cli.main(["resources"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("BC Prod")
    assert "10.0.0.5" in lines[0]
    assert "10.0.0.9" in lines[1]


def test_resources_error_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "resources",
        lambda include_hidden=False: core.ResourceList(
            error="Twingate must be connected to display available resources."
        ),
    )
    assert cli.main(["resources"]) == 1
    assert "must be connected" in capsys.readouterr().err


def test_resources_json_counts(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "resources",
        lambda include_hidden=False: core.ResourceList(items=[core.Resource("a", "b")]),
    )
    assert cli.main(["resources", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1


def test_connect_passes_target_and_coupling(monkeypatch, capsys):
    seen = {}

    def fake_connect(auth_target="", couple_tailscale=False):
        seen["target"] = auth_target
        seen["coupled"] = couple_tailscale
        return core.ActionResult(True, "Twingate started", ["step"])

    monkeypatch.setattr(core, "connect", fake_connect)
    assert cli.main(["connect", "--target", "*.x.gr", "--no-couple-tailscale"]) == 0
    assert seen == {"target": "*.x.gr", "coupled": False}


def test_connect_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "connect",
        lambda **k: core.ActionResult(False, "Could not start Twingate"),
    )
    assert cli.main(["connect"]) == 1


def test_doctor_reports_unverifiable_without_failing(monkeypatch, capsys):
    # `None` means "cannot be determined from an unprivileged process" and must
    # not be reported as a failure.
    monkeypatch.setattr(
        core, "doctor", lambda: [("polkit rule", None, "unverifiable without root")]
    )
    assert cli.main(["doctor"]) == 0
    assert "??" in capsys.readouterr().out


def test_doctor_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(core, "doctor", lambda: [("twingate CLI", False, "not on PATH")])
    assert cli.main(["doctor"]) == 1


def test_autostart_status_reflects_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["autostart", "status"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["autostart", "on"]) == 0
    assert cli.main(["autostart", "status"]) == 0
    assert "enabled" in capsys.readouterr().out


def test_no_subcommand_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_status_runs_without_importing_gtk():
    """`twintray status` must work over SSH with no display."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); from twintray import cli; "
         "cli.main(['about']); "
         "print('gi' in sys.modules, 'gi.repository.Gtk' in sys.modules)" % str(Path(__file__).resolve().parent.parent)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("False False")


def test_status_calls_out_a_resource_that_still_needs_auth(monkeypatch, capsys):
    """The whole point: online is not the same as usable."""
    monkeypatch.setattr(
        core,
        "status",
        lambda verbose=True: core.Status(state=core.ONLINE, network="diastasys"),
    )
    monkeypatch.setattr(
        core,
        "resources",
        lambda include_hidden=False: core.ResourceList(
            items=core.parse_resources(
                "RESOURCE NAME\tADDRESS\tALIAS\tAUTH STATUS\n"
                "*.diastasys.gr\t*.diastasys.gr\t-\tPending\n"
            )
        ),
    )
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending:" in out
    assert "*.diastasys.gr (Pending)" in out
    assert "twintray auth" in out


def test_status_stays_quiet_when_nothing_needs_auth(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "status",
        lambda verbose=True: core.Status(state=core.ONLINE, network="diastasys"),
    )
    monkeypatch.setattr(
        core,
        "resources",
        lambda include_hidden=False: core.ResourceList(
            items=[core.Resource("BC Prod SQL", "10.0.0.5")]
        ),
    )
    assert cli.main(["status"]) == 0
    assert "pending" not in capsys.readouterr().out


def test_status_does_not_ask_the_client_while_disconnected(monkeypatch, capsys):
    # resources only lists what the tunnel exposes; asking while down either
    # errors or wastes a network round-trip on every status check.
    def fail(*_a, **_k):
        raise AssertionError("resources should not be queried when down")

    monkeypatch.setattr(
        core, "status", lambda verbose=True: core.Status(state=core.STOPPED)
    )
    monkeypatch.setattr(core, "resources", fail)
    assert cli.main(["status"]) == 0


def test_auth_prints_each_step_and_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        core,
        "authenticate",
        lambda target="": core.ActionResult(
            False, "Authentication failed for X", ["twingate auth X: FAILED"]
        ),
    )
    assert cli.main(["auth", "X"]) == 1
    out = capsys.readouterr().out
    assert "twingate auth X: FAILED" in out
    assert "Authentication failed for X" in out


def test_auth_with_no_target_is_accepted(monkeypatch, capsys):
    seen = {}

    def fake(target=""):
        seen["target"] = target
        return core.ActionResult(True, "Nothing needs authenticating", [])

    monkeypatch.setattr(core, "authenticate", fake)
    assert cli.main(["auth"]) == 0
    assert seen["target"] == ""
    assert "Nothing needs authenticating" in capsys.readouterr().out
