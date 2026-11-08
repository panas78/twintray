"""TwinTray core — every operation the app performs, with no GTK import.

This module must stay toolkit-free so that `twintray status --json` works over
SSH, in a cron job and in the test suite with no display attached. The GUI and
the tray are consumers, never the other way round.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, Sequence

TWINGATE = "twingate"
SYSTEMD_UNIT = "twingate.service"

ONLINE = "online"
AUTHENTICATING = "authenticating"
OFFLINE = "offline"
DISCONNECTED = "disconnected"
ERROR = "error"
STOPPED = "not-running"
UNKNOWN = "unknown"

# Every token `twingate status` can print on its own. Taken from the binary's
# own state enum, so a new one shows up here as UNKNOWN rather than crashing.
KNOWN_STATES = (ONLINE, AUTHENTICATING, OFFLINE, DISCONNECTED, ERROR, STOPPED)

# States in which the tunnel is carrying traffic.
LIVE_STATES = (ONLINE,)

STATE_LABELS = {
    ONLINE: "Connected",
    AUTHENTICATING: "Authenticating",
    OFFLINE: "Offline",
    DISCONNECTED: "Disconnected",
    ERROR: "Error",
    STOPPED: "Stopped",
    UNKNOWN: "Unknown",
}


class TwingateMissing(RuntimeError):
    """The twingate CLI is not installed or not on PATH."""


# --------------------------------------------------------------------- results


@dataclass
class Run:
    ok: bool
    code: int
    out: str = ""
    err: str = ""

    @property
    def text(self) -> str:
        return (self.out or self.err or "").strip()


def run(args: Sequence[str], timeout: int = 30) -> Run:
    """Run a command with a pinned locale.

    `LC_ALL=C` matters for parsing, not just tidiness: a localised
    `twingate status` on this machine would answer in Greek.
    """
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise TwingateMissing(f"{args[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired:
        return Run(False, -1, err=f"{args[0]} timed out after {timeout}s")
    return Run(proc.returncode == 0, proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------- status


@dataclass
class Status:
    state: str = UNKNOWN
    network: str = ""
    auth_expires: str = ""
    lines: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, STATE_LABELS[UNKNOWN])

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "label": self.label,
            "live": self.live,
            "network": self.network,
            "auth_expires": self.auth_expires,
            "lines": self.lines,
            "error": self.error,
        }


# `twingate status` prints one word ("online") and `-v` prints one line
# ("Online: User"). It never contains the network name or an auth expiry —
# those regexes existed on a guess and could never match in practice.
_NETWORK_RE = re.compile(r"connected to\s+(.+?)\s*$", re.IGNORECASE)
_EXPIRES_RE = re.compile(r"auth(?:entication)?\s+expires\s+in\s+(.+)", re.IGNORECASE)

# World-readable and present whether or not the daemon is up.
NETWORK_CONF = "/etc/twingate/network.conf"


def parse_status(text: str) -> str:
    """Reduce `twingate status` output to one of KNOWN_STATES.

    The non-verbose command prints a single token, but the parser takes the
    first recognised token anywhere in the output so that a banner or an
    unexpected leading line cannot turn a real state into UNKNOWN.
    """
    for raw in (text or "").splitlines():
        token = raw.strip().strip(".").lower()
        if token in KNOWN_STATES:
            return token
    for raw in (text or "").splitlines():
        lowered = raw.strip().lower()
        for candidate in KNOWN_STATES:
            if lowered.startswith(candidate):
                return candidate
    return UNKNOWN


def network_name() -> str:
    """The Twingate network slug, read from the client's own config.

    `twingate status -v` does not report it, and `/var/lib/twingate` (where the
    real state lives) is root-only. `network.conf` is world-readable, holds one
    line, and is present whether or not the daemon is running — so the UI can
    name the network even while disconnected.
    """
    try:
        with open(NETWORK_CONF, encoding="utf-8") as handle:
            for line in handle:
                value = line.strip()
                if value:
                    return value
    except OSError:
        pass
    return ""


def status(verbose: bool = True) -> Status:
    """Read the current state, optionally with the verbose detail lines."""
    args = [TWINGATE, "status"]
    if verbose:
        args.append("-v")
    args.append("-d")

    result = run(args)
    text = result.out or result.err
    state = parse_status(text)

    detail = Status(state=state, lines=[l for l in text.splitlines() if l.strip()])
    if not result.ok:
        detail.error = result.text

    for line in detail.lines:
        if not detail.network:
            match = _NETWORK_RE.search(line)
            if match:
                detail.network = match.group(1)
        if not detail.auth_expires:
            match = _EXPIRES_RE.search(line)
            if match:
                detail.auth_expires = match.group(1)

    # Neither of the above fires against the current client, so fall back to the
    # config file rather than showing a dash forever.
    if not detail.network:
        detail.network = network_name()
    return detail


# ------------------------------------------------------------------- resources


@dataclass
class Resource:
    name: str
    address: str = ""
    alias: str = ""
    auth_status: str = ""
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "alias": self.alias,
            "auth_status": self.auth_status,
        }


@dataclass
class ResourceList:
    items: list[Resource] = field(default_factory=list)
    raw: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "count": len(self.items),
            "auth": self.auth_summary,
            "error": self.error,
        }

    @property
    def auth_summary(self) -> str:
        """What the client says about per-resource authorization.

        The CLI exposes no global SSO expiry — `twingate status -v` prints only
        "Online: User" — so the only real authorization signal is the AUTH
        STATUS column of `twingate resources`. Render what it actually says
        instead of a permanent dash.
        """
        if self.error:
            return self.error
        if not self.items:
            return "no resources"
        statuses = sorted({r.auth_status for r in self.items if r.auth_status})
        if not statuses:
            return "not required by any resource"
        return ", ".join(statuses)


_HEADER_LINES = {"resource name", "main resources", "background resources", "resources"}


def _is_header(line: str) -> bool:
    """True for a column-header or section-title row, never for a resource.

    Matched on the **whole line** with whitespace collapsed, before any column
    splitting: a tab-header collapses to "resource name address alias auth
    status", while a header with no tab and no wide gap would otherwise split
    into "RESOURCE"/"NAME" and slip past a first-column test.
    """
    probe = " ".join(line.split()).lower()
    return probe in _HEADER_LINES or (
        probe.startswith("resource name ") and "address" in probe
    )


def _column(columns: list[str], index: int) -> str:
    """Read one column positionally, treating the CLI's "-" as "no value"."""
    if index >= len(columns):
        return ""
    value = columns[index]
    return "" if value == "-" else value


def parse_resources(text: str) -> list[Resource]:
    """Turn `twingate resources` into rows.

    The real output is **tab-separated with a header row**:

        RESOURCE NAME         \\tADDRESS       \\tALIAS\\tAUTH STATUS
        *.diastasys.gr        \\t*.diastasys.gr\\t-    \\t

    So split on tabs first. Splitting on runs of whitespace (the previous
    approach) kept the header as a resource named "RESOURCE NAME" and glued the
    alias onto the address. Whitespace splitting stays as a fallback for a
    future format change, with the header filter applying to both.
    """
    items: list[Resource] = []
    for line in (text or "").splitlines():
        if not line.strip() or _is_header(line):
            continue

        if "\t" in line:
            columns = [c.strip() for c in line.split("\t")]
        else:
            columns = [c.strip() for c in re.split(r"\s{2,}", line.strip())]
            # A line with no tab and no wide gap still carries two fields.
            if len(columns) == 1:
                parts = columns[0].split(None, 1)
                if len(parts) == 2:
                    columns = parts
        if not columns[0]:
            continue

        items.append(
            Resource(
                name=columns[0],
                address=_column(columns, 1),
                alias=_column(columns, 2),
                auth_status=_column(columns, 3),
                raw=line.rstrip(),
            )
        )
    return items


def resources(include_hidden: bool = False) -> ResourceList:
    args = [TWINGATE, "resources", "-d"]
    if include_hidden:
        args.insert(2, "--all")
    result = run(args, timeout=45)
    text = result.out if result.ok else (result.out or result.err)
    listing = ResourceList(raw=text.strip())
    if not result.ok:
        listing.error = result.text
        return listing
    listing.items = parse_resources(text)
    return listing


# --------------------------------------------------------------------- systemd


def service_active() -> bool:
    result = run(["systemctl", "is-active", SYSTEMD_UNIT])
    return result.text.strip() == "active"


def unit_state() -> str:
    return run(["systemctl", "is-active", SYSTEMD_UNIT]).text.strip()


def polkit_authorized() -> bool | None:
    """Can we start/stop the unit without a password prompt?

    Probed by issuing a command that is a **no-op because of the state the unit
    is already in** — `systemctl stop` on an inactive unit, `systemctl start` on
    an active one. Both still pass through polkit, so the exit code is the real
    authorization answer, and neither changes anything.

    That is the only honest probe available to an unprivileged process:
    `/etc/polkit-1/rules.d` is 0700 root so the rule file cannot be stat'ed, and
    `pkcheck --detail unit …` refuses untrusted callers
    (`Only trusted callers (e.g. uid 0 or an action owner) can use
    CheckAuthorization()`). Verified on this machine: `systemctl stop twingate`
    against a stopped unit returns 0 with no prompt when the rule is installed.

    Returns None when the unit is in a transitional state, because then no verb
    is a no-op and probing would really change something.
    """
    state = unit_state()
    if state not in ("active", "inactive"):
        return None
    verb = "start" if state == "active" else "stop"
    return run(["systemctl", verb, SYSTEMD_UNIT], timeout=30).ok


def start_service(timeout: int = 45) -> Run:
    """Start the daemon. Passwordless via the polkit rule for wheel members."""
    return run(["systemctl", "start", SYSTEMD_UNIT], timeout=timeout)


def stop_service(timeout: int = 45) -> Run:
    return run(["systemctl", "stop", SYSTEMD_UNIT], timeout=timeout)


# ------------------------------------------------------------------- tailscale


def tailscale_available() -> bool:
    return shutil.which("tailscale") is not None


def tailscale_up() -> Run:
    return run(["tailscale", "up"], timeout=30)


def tailscale_down() -> Run:
    return run(["tailscale", "down"], timeout=30)


def tailscale_state() -> str:
    if not tailscale_available():
        return "absent"
    result = run(["tailscale", "status", "--json"], timeout=15)
    if not result.ok:
        return "unknown"
    try:
        payload = json.loads(result.out)
    except ValueError:
        return "unknown"
    return (payload.get("BackendState") or "unknown").lower()


# ------------------------------------------------------------------- lifecycle


@dataclass
class ActionResult:
    ok: bool
    message: str = ""
    steps: list[str] = field(default_factory=list)


def connect(auth_target: str = "", couple_tailscale: bool = False) -> ActionResult:
    """Bring the tunnel up, mirroring what `tg-start` does.

    Order matters and matches the existing script: start the daemon, drop
    Tailscale (they both claim 100.96.12.0/12, so leaving it up produces a
    routing fight), then kick the SSO flow for `auth_target`.
    """
    steps: list[str] = []
    started = start_service()
    steps.append(f"systemctl start {SYSTEMD_UNIT}: {'ok' if started.ok else 'FAILED'}")
    if not started.ok:
        return ActionResult(False, started.text or "Could not start Twingate", steps)

    if couple_tailscale and tailscale_available():
        down = tailscale_down()
        steps.append(f"tailscale down: {'ok' if down.ok else 'FAILED'}")

    if auth_target.strip():
        auth = auth_flow(auth_target.strip())
        steps.append(f"twingate auth {auth_target}: {'ok' if auth.ok else 'FAILED'}")

    return ActionResult(True, "Twingate started", steps)


def disconnect(couple_tailscale: bool = False) -> ActionResult:
    steps: list[str] = []
    stopped = stop_service()
    steps.append(f"systemctl stop {SYSTEMD_UNIT}: {'ok' if stopped.ok else 'FAILED'}")
    if not stopped.ok:
        return ActionResult(False, stopped.text or "Could not stop Twingate", steps)

    if couple_tailscale and tailscale_available():
        up = tailscale_up()
        steps.append(f"tailscale up: {'ok' if up.ok else 'FAILED'}")

    return ActionResult(True, "Twingate stopped", steps)


def auth_flow(target: str) -> Run:
    return run([TWINGATE, "auth", target], timeout=120)


def reconnect() -> Run:
    return run([TWINGATE, "connect"], timeout=60)


# ----------------------------------------------------------------------- doctor


def doctor() -> list[tuple[str, bool | None, str]]:
    """Everything the app needs, and whether it is there.

    First thing to run on a new machine, and the fastest way to explain
    "nothing happens when I click the icon".

    The third element of `ok` is **None** for "cannot be determined from an
    unprivileged process". That is deliberate: `/etc/polkit-1/rules.d` is
    0700 root, so `os.access()` on the rule file is always False for us and a
    plain boolean would report a missing rule on a machine that has one.
    `pkcheck` is no help either — it refuses `--detail` from an untrusted
    caller, and without the `unit` detail the rule under test never matches.
    """
    checks: list[tuple[str, bool | None, str]] = []

    binary = shutil.which(TWINGATE)
    checks.append(("twingate CLI", bool(binary), binary or "not on PATH"))

    if binary:
        version = run([TWINGATE, "version"])
        checks.append(
            (
                "twingate version",
                version.ok,
                version.text.splitlines()[0] if version.ok else "unreadable",
            )
        )
        current = status()
        checks.append(
            ("twingate status", current.state != UNKNOWN, f"{current.state} ({current.label})")
        )
        checks.append(
            ("twingate service", True, "active" if service_active() else "inactive")
        )

    systemctl = shutil.which("systemctl")
    checks.append(("systemctl", bool(systemctl), systemctl or "not on PATH"))

    rule = "/etc/polkit-1/rules.d/50-twingate.rules"
    required = "start/stop will prompt — or silently fail with no polkit agent"
    if os.access(rule, os.R_OK):
        checks.append(("polkit rule", True, f"{rule} present"))
    elif not os.access("/etc/polkit-1/rules.d", os.R_OK):
        # The directory is root-only, so the file cannot be stat'ed. Probe the
        # authorization itself instead of guessing from a missing file — see
        # polkit_authorized() for why this is safe.
        authorized = polkit_authorized()
        if authorized is None:
            checks.append(("polkit rule", None, f"unit busy; could not probe. {required}"))
        elif authorized:
            checks.append(
                ("polkit rule", True, "start/stop authorized without a password")
            )
        else:
            checks.append(("polkit rule", False, f"not authorized — {required}"))
    else:
        checks.append(("polkit rule", False, f"{rule} missing — {required}"))

    checks.append(
        ("tailscale", True, "coupled" if tailscale_available() else "not installed")
    )

    try:
        import gi  # noqa: F401

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401

        checks.append(
            (
                "GTK4 + libadwaita",
                True,
                f"Gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}, Adw {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}",
            )
        )
    except Exception as exc:  # pragma: no cover - depends on the machine
        checks.append(("GTK4 + libadwaita", False, str(exc)))

    from . import prefs as prefs_mod

    current_prefs = prefs_mod.Prefs.load()
    checks.append(
        (
            "prefs",
            True,
            f"{prefs_mod.prefs_path()} (auth_target={current_prefs.auth_target or 'unset'})",
        )
    )
    return checks


def summarise(checks: Iterable[tuple[str, bool | None, str]]) -> bool:
    """True when nothing is definitively broken. `None` is not a failure."""
    return all(ok is not False for _, ok, _ in checks)
