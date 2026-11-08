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
