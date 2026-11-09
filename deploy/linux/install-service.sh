#!/usr/bin/env sh
# Run Trioditis at login as a systemd *user* service (no root needed), plus a daily
# `trioditis refit` (rates, drift charts, model facts; it changes nothing the router uses).
# Usage: ./deploy/linux/install-service.sh        Remove: ./deploy/linux/install-service.sh --remove
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UNITS="$HOME/.config/systemd/user"
UNIT="$UNITS/trioditis.service"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now trioditis trioditis-refit.timer 2>/dev/null || true
  rm -f "$UNIT" "$UNITS/trioditis-refit.service" "$UNITS/trioditis-refit.timer" && systemctl --user daemon-reload
  echo "removed"; exit 0
fi

[ -x "$REPO/.venv/bin/python" ] || { echo "create the venv first (see README)"; exit 1; }
[ -f "$REPO/config.yaml" ] || { echo "create config.yaml first (see README)"; exit 1; }

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=Trioditis LLM router (localhost only)
After=network-online.target

[Service]
WorkingDirectory=$REPO
ExecStart=$REPO/.venv/bin/python -m trioditis --config config.yaml serve --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
LockPersonality=true
RestrictRealtime=true
SystemCallArchitectures=native
MemoryMax=512M

[Install]
WantedBy=default.target
UNIT
cat > "$UNITS/trioditis-refit.service" <<UNIT
[Unit]
Description=Trioditis daily refit (reads the call record and model facts; writes ~/.hlidskjalv/trioditis-fitted.json)

[Service]
Type=oneshot
WorkingDirectory=$REPO
# First what the config's plan: section names (does nothing without one); "-": a failed import never blocks the refit.
ExecStartPre=-$REPO/.venv/bin/python -m trioditis --config config.yaml plan import
ExecStart=$REPO/.venv/bin/python -m trioditis --config config.yaml refit
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
LockPersonality=true
RestrictRealtime=true
SystemCallArchitectures=native
UNIT
cat > "$UNITS/trioditis-refit.timer" <<UNIT
[Unit]
Description=Run the Trioditis refit once a day

[Timer]
OnCalendar=daily
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now trioditis trioditis-refit.timer
echo "running. logs: journalctl --user -u trioditis -f; refit: journalctl --user -u trioditis-refit"
