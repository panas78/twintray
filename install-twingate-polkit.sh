#!/bin/bash
# install-twingate-polkit.sh — Authorize passwordless Twingate start/stop for
# active wheel members, so the Noctalia bar toggle buttons (and the
# tg-start/tg-stop scripts) work with a single click and no polkit agent.
#
# Background:
#   tg-start/tg-stop run `systemctl start/stop twingate`. As a non-root user
#   that's gated by polkit's org.freedesktop.systemd1.manage-units action,
#   which by default prompts for admin auth. A bare wlroots session
#   (Umbriel + Noctalia) ships no polkit agent, so the prompt never appears
#   and the command silently fails. This rule returns YES for twingate.service
#   only, making that one unit passwordless while everything else stays
#   protected.
#
# Usage:
#   sudo ./install-twingate-polkit.sh          # install the rule
#   sudo ./install-twingate-polkit.sh --remove # remove the rule
#   sudo ./install-twingate-polkit.sh --status # show if the rule is installed

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RULE_SRC="$SCRIPT_DIR/polkit/50-twingate.rules"
RULE_FILE="/etc/polkit-1/rules.d/50-twingate.rules"

case "${1:-}" in
--remove|-r)
    if [[ -f "$RULE_FILE" ]]; then
        rm -f "$RULE_FILE"
        systemctl restart polkit 2>/dev/null || true
        echo "Removed $RULE_FILE and restarted polkit."
    else
        echo "Nothing to remove: $RULE_FILE does not exist."
    fi
    exit 0
    ;;
--status|-s)
    if [[ -f "$RULE_FILE" ]]; then
        echo "INSTALLED: $RULE_FILE"
        echo "Installed on: $(stat -c %y "$RULE_FILE")"
    else
        echo "NOT INSTALLED: $RULE_FILE does not exist."
    fi
    exit 0
    ;;
esac

# Must run as root (sudo) — the rules dir is root-owned.
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    echo "Run:  sudo $0" >&2
    exit 1
fi

if [[ ! -f "$RULE_SRC" ]]; then
    echo "ERROR: rule source not found: $RULE_SRC" >&2
    exit 1
fi

mkdir -p "$(dirname "$RULE_FILE")"
install -m 0644 "$RULE_SRC" "$RULE_FILE"

# Reload polkit so the rule takes effect immediately (no relogin needed).
systemctl restart polkit 2>/dev/null || true

echo "Installed: $RULE_FILE"
echo "Twingate start/stop is now passwordless for active wheel members."
echo "Verify:  systemctl start twingate && systemctl stop twingate  (no prompt)"
