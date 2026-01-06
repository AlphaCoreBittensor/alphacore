#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

PM2_NAMESPACE=""
CONFIG_FILE=""
INTERVAL="${INTERVAL:-2m}"
STATE_DIR="$HOME/.local/state/alphacore/autoupdate"

usage() {
  cat <<'EOF'
Ensure a per-user auto-update scheduler is enabled.

Prefers a systemd user timer. If systemd user timers are unavailable, falls back to crontab.

Usage:
  bash scripts/validator/process/ensure_autoupdate_timer.sh --pm2-namespace NAME --config /path/to/autoupdate.env [options]

Required:
  --pm2-namespace NAME     Instance key (recommended: alphacore)
  --config PATH            Config file passed to autoupdate_release.sh

Options:
  --interval DURATION      Default: 2m (systemd) or */2 minutes (cron)
  --help|-h                Show help
EOF
}

log() { echo "[ensure_autoupdate] $*"; }
warn() { echo "[ensure_autoupdate] WARNING: $*" >&2; }
die() { echo "[ensure_autoupdate] ERROR: $*" >&2; exit 1; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --pm2-namespace) PM2_NAMESPACE="$2"; shift 2 ;;
      --config) CONFIG_FILE="$2"; shift 2 ;;
      --interval) INTERVAL="$2"; shift 2 ;;
      --help|-h) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user show-environment >/dev/null 2>&1
}

write_systemd_units() {
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  mkdir -p "$STATE_DIR"

  local service_path="$unit_dir/alphacore-autoupdate@.service"
  local timer_path="$unit_dir/alphacore-autoupdate@.timer"

  cat >"$service_path" <<EOF
[Unit]
Description=AlphaCore auto-update (%i)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REPO_ROOT}
StandardOutput=append:%h/.local/state/alphacore/autoupdate/%i.log
StandardError=append:%h/.local/state/alphacore/autoupdate/%i.log
ExecStart=/bin/bash -lc 'bash ${REPO_ROOT}/scripts/validator/process/autoupdate_release.sh --config ${CONFIG_FILE}'
EOF

  cat >"$timer_path" <<EOF
[Unit]
Description=AlphaCore auto-update timer (%i)

[Timer]
OnBootSec=${INTERVAL}
OnUnitActiveSec=${INTERVAL}
RandomizedDelaySec=30
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now "alphacore-autoupdate@${PM2_NAMESPACE}.timer"
  log "Enabled systemd user timer: alphacore-autoupdate@${PM2_NAMESPACE}.timer (interval=${INTERVAL})"
}

interval_to_cron_minutes() {
  # Accept "Nm" only; fallback to 2 minutes.
  local raw="$1"
  if [[ "$raw" =~ ^([0-9]+)m$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi
  echo "2"
}

write_cron_entry() {
  command -v crontab >/dev/null 2>&1 || die "crontab not found and systemd user timers unavailable"
  local minutes
  minutes="$(interval_to_cron_minutes "$INTERVAL")"
  mkdir -p "$STATE_DIR"
  local log_path="${STATE_DIR}/${PM2_NAMESPACE}.log"
  local job="cd ${REPO_ROOT} && /bin/bash ${REPO_ROOT}/scripts/validator/process/autoupdate_release.sh --config ${CONFIG_FILE} >> ${log_path} 2>&1"
  local line="*/${minutes} * * * * ${job}"

  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'scripts/validator/process/autoupdate_release.sh' >"$tmp" || true
  printf "%s\n" "$line" >>"$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  log "Installed cron entry (every ${minutes} minutes)"
}

main() {
  parse_args "$@"
  [[ -n "$PM2_NAMESPACE" ]] || die "--pm2-namespace is required"
  [[ -n "$CONFIG_FILE" ]] || die "--config is required"
  [[ -f "$CONFIG_FILE" ]] || die "Config file not found: $CONFIG_FILE"

  if systemd_user_available; then
    write_systemd_units
  else
    warn "systemd user timers unavailable; falling back to cron. If you want systemd timers on servers, enable lingering: sudo loginctl enable-linger $USER"
    write_cron_entry
  fi
}

main "$@"
