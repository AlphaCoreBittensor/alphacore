#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

CONFIG_FILE=""
REMOTE="origin"
BRANCH="release"
FORCE="0"
DRY_RUN="0"

usage() {
  cat <<'EOF'
Auto-update an AlphaCore validator machine from origin/release (or any remote/branch).

This script is intended to run on validator machines under a timer (cron/systemd).
It is update-safe: it refuses to restart while a validator round is active or the
Validation API is running sandbox jobs, unless --force is used.

Usage:
  bash scripts/validator/process/autoupdate_release.sh --config /path/to/autoupdate.env [options]

Required:
  --config PATH           Path to a shell env file (KEY=VALUE) containing machine settings.

Options:
  --remote NAME           Default: origin
  --branch NAME           Default: release
  --dry-run               Print what would happen; do not stop/update/restart
  --force                 Override safety gates (NOT recommended)
  --help|-h               Show help

Config file keys (minimal):
  NETWORK=finney|test|local
  WALLET_NAME=...
  WALLET_HOTKEY=...
  NETUID=...
  GCP_CREDS_FILE=/abs/path/to/key.json   # or export GOOGLE_OAUTH_ACCESS_TOKEN in the environment

Recommended:
  PM2_NAMESPACE=alphacore   # default: alphacore

  # Optional
  CHAIN_ENDPOINT=wss://...
  PROFILE=production|testing|local
  VALIDATOR_PROCESS_NAME=validator-<hotkey>-<network>
  VALIDATION_PROCESS_NAME=alphacore-validation-api
  VALIDATION_API_ENDPOINT=http://127.0.0.1:8888
  VALIDATOR_VENV_DIR=/abs/path/to/venv
  VALIDATION_VENV_DIR=/abs/path/to/.venv-validation-api
  WALLET_PATH=/home/.../.bittensor/wallets

  # Round gate (recommended; written by the validator)
  VALIDATOR_ROUND_LOCKFILE=/tmp/alphacore-validator-round.lock

  # Extra passthrough args (optional)
  # VALIDATOR_EXTRA_ARGS="--timed --tick-seconds 30"
  # VALIDATION_API_EXTRA_ARGS=""
EOF
}

ts() { date +"%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "[$(ts)] [autoupdate] $*"; }
warn() { echo "[$(ts)] [autoupdate] WARNING: $*" >&2; }
die() { echo "[$(ts)] [autoupdate] ERROR: $*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config) CONFIG_FILE="$2"; shift 2 ;;
      --remote) REMOTE="$2"; shift 2 ;;
      --branch) BRANCH="$2"; shift 2 ;;
      --dry-run) DRY_RUN="1"; shift ;;
      --force) FORCE="1"; shift ;;
      --help|-h) usage; exit 0 ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
  done
}

is_validation_api_idle() {
  local endpoint="${VALIDATION_API_ENDPOINT:-http://127.0.0.1:8888}"
  local url="${endpoint%/}/validate/active"
  local body
  if ! body="$(curl -fsS --max-time 2 "$url" 2>/dev/null)"; then
    # If the API is down, treat it as not-busy; restart may be exactly what we need.
    warn "Validation API not reachable at $url; treating as idle for update gating."
    return 0
  fi

  # Expected response shape:
  #   {"active":[...]}
  # Treat anything other than an explicit empty array as "busy/unknown" to be safe.
  local compact
  compact="$(printf "%s" "$body" | tr -d '\r\n\t ')"
  if [[ "$compact" == *'"active":[]'* ]]; then
    return 0
  fi
  if [[ "$compact" == *'"active":['* ]]; then
    return 1
  fi
  warn "Unexpected /validate/active response; treating as busy for update gating: ${body:0:200}"
  return 2
}

is_validator_round_active() {
  local lockfile="${VALIDATOR_ROUND_LOCKFILE:-/tmp/alphacore-validator-round.lock}"
  [[ -f "$lockfile" ]]
}

should_run_sandbox_setup() {
  # Decide whether the host provisioning needs to be re-run.
  # Prefer an explicit SETUP_VERSION stamp in the *target* commit; fallback to a "setup script changed" heuristic.
  local from_sha="$1"
  local to_sha="$2"
  local installed_file="/var/lib/alphacore/sandbox_setup_version"

  local desired=""
  desired="$(git show "${to_sha}:modules/evaluation/validation/sandbox/SETUP_VERSION" 2>/dev/null | tr -d '\r\n\t ' || true)"

  if [[ -n "$desired" ]]; then
    local installed=""
    if [[ -f "$installed_file" ]]; then
      installed="$(tr -d '\r\n\t ' <"$installed_file" || true)"
    fi
    [[ "$desired" != "$installed" ]]
    return
  fi

  # Heuristic: if setup.sh itself changed between the deployed and target SHAs.
  git diff --name-only "${from_sha}..${to_sha}" | grep -q '^modules/evaluation/validation/sandbox/setup\.sh$'
}

refresh_validator_deps() {
  local venv="${VALIDATOR_VENV_DIR:-$REPO_ROOT/venv}"
  local req="$REPO_ROOT/modules/requirements.txt"
  if [[ ! -f "$req" ]]; then
    warn "requirements missing at $req; skipping validator dependency refresh"
    return 0
  fi
  if [[ ! -f "$venv/bin/activate" ]]; then
    warn "validator venv missing at $venv; skipping validator dependency refresh"
    return 0
  fi
  # shellcheck disable=SC1090
  source "$venv/bin/activate"
  python -m pip install -U pip setuptools wheel >/dev/null 2>&1 || true
  python -m pip install -r "$req"
}

main() {
  parse_args "$@"
  [[ -n "$CONFIG_FILE" ]] || die "--config is required"
  [[ -f "$CONFIG_FILE" ]] || die "Config file not found: $CONFIG_FILE"

  require_cmd git
  require_cmd curl
  require_cmd flock
  require_cmd pm2

  # Load machine-specific settings.
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  set +a

  [[ -n "${NETWORK:-}" ]] || die "Config missing: NETWORK"
  [[ -n "${WALLET_NAME:-}" ]] || die "Config missing: WALLET_NAME"
  [[ -n "${WALLET_HOTKEY:-}" ]] || die "Config missing: WALLET_HOTKEY"
  [[ -n "${NETUID:-}" ]] || die "Config missing: NETUID"

  if [[ -z "${GCP_CREDS_FILE:-}" && -z "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ]]; then
    die "Config missing: GCP_CREDS_FILE (or set GOOGLE_OAUTH_ACCESS_TOKEN in environment)"
  fi
  if [[ -n "${GCP_CREDS_FILE:-}" && ! -f "${GCP_CREDS_FILE}" ]]; then
    die "GCP_CREDS_FILE not found: ${GCP_CREDS_FILE}"
  fi

  PM2_NAMESPACE="${PM2_NAMESPACE:-alphacore}"
  VALIDATION_API_ENDPOINT="${VALIDATION_API_ENDPOINT:-http://127.0.0.1:8888}"

  # Concurrency guard (avoid multiple overlapping updaters).
  local lock_path="${AUTOUPDATE_LOCKFILE:-/tmp/alphacore-autoupdate-${PM2_NAMESPACE}.lock}"
  exec 9>"$lock_path"
  if ! flock -n 9; then
    log "Another updater is running (lock: $lock_path); exiting."
    exit 0
  fi

  cd "$REPO_ROOT"

  # Ensure working tree is clean so we can hard-reset safely.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "Working tree has local modifications; refusing to update (clean or deploy from a dedicated clone)."
  fi

  log "Fetching $REMOTE/$BRANCH..."
  git fetch --prune "$REMOTE" "$BRANCH"

  local target_sha current_sha
  target_sha="$(git rev-parse "${REMOTE}/${BRANCH}")"
  current_sha="$(git rev-parse HEAD)"

  if [[ "$target_sha" == "$current_sha" ]]; then
    log "Already up-to-date at $current_sha."
    exit 0
  fi

  log "Update available: $current_sha -> $target_sha"

  local sandbox_setup_needed="0"
  if should_run_sandbox_setup "$current_sha" "$target_sha"; then
    sandbox_setup_needed="1"
  fi

  if [[ "$FORCE" != "1" ]]; then
    if is_validator_round_active; then
      log "Validator round appears active (lockfile: ${VALIDATOR_ROUND_LOCKFILE:-/tmp/alphacore-validator-round.lock}); deferring update."
      exit 0
    fi
    if ! is_validation_api_idle; then
      log "Validation API is busy (active jobs); deferring update."
      exit 0
    fi
  else
    warn "--force set; skipping safety gates."
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    log "--dry-run: would stop PM2 namespace '$PM2_NAMESPACE', reset to '$target_sha', refresh deps, and restart services."
    exit 0
  fi

  log "Stopping PM2 namespace: $PM2_NAMESPACE"
  pm2 stop all --namespace "$PM2_NAMESPACE" >/dev/null 2>&1 || true

  log "Checking out target SHA: $target_sha"
  git reset --hard "$target_sha"

  if [[ "$sandbox_setup_needed" == "1" ]]; then
    log "Sandbox host setup required; running setup.sh (requires sudo)..."
    sudo bash "$REPO_ROOT/modules/evaluation/validation/sandbox/setup.sh"
  else
    log "Sandbox host setup not required."
  fi

  log "Refreshing validator Python deps (venv)..."
  refresh_validator_deps

  log "Re-launching Validation API (PM2) under namespace: $PM2_NAMESPACE"
  VALIDATION_LAUNCH_ARGS=(--network "$NETWORK" --pm2-namespace "$PM2_NAMESPACE")
  if [[ -n "${GCP_CREDS_FILE:-}" ]]; then
    VALIDATION_LAUNCH_ARGS+=(--gcp-creds-file "$GCP_CREDS_FILE")
  fi
  if [[ -n "${VALIDATION_PROCESS_NAME:-}" ]]; then
    VALIDATION_LAUNCH_ARGS+=(--process-name "$VALIDATION_PROCESS_NAME")
  fi
  if [[ -n "${VALIDATION_ENV_FILE:-}" ]]; then
    VALIDATION_LAUNCH_ARGS+=(--env-out "$VALIDATION_ENV_FILE")
  fi
  if [[ -n "${VALIDATION_VENV_DIR:-}" ]]; then
    VALIDATION_LAUNCH_ARGS+=(--venv-dir "$VALIDATION_VENV_DIR")
  fi
  if [[ -n "${VALIDATION_HTTP_HOST:-}" ]]; then
    VALIDATION_LAUNCH_ARGS+=(--bind-host "$VALIDATION_HTTP_HOST")
  fi
  if [[ -n "${VALIDATION_HTTP_PORT:-}" ]]; then
    VALIDATION_LAUNCH_ARGS+=(--port "$VALIDATION_HTTP_PORT")
  fi
  if [[ -n "${VALIDATION_API_EXTRA_ARGS:-}" ]]; then
    # shellcheck disable=SC2206
    VALIDATION_LAUNCH_ARGS+=(${VALIDATION_API_EXTRA_ARGS})
  fi

  bash "$REPO_ROOT/scripts/validator/process/launch_validation_api.sh" "${VALIDATION_LAUNCH_ARGS[@]}"

  log "Re-launching validator (PM2) under namespace: $PM2_NAMESPACE"
  VALIDATOR_LAUNCH_ARGS=(
    --wallet-name "$WALLET_NAME"
    --hotkey "$WALLET_HOTKEY"
    --netuid "$NETUID"
    --network "$NETWORK"
    --pm2-namespace "$PM2_NAMESPACE"
    --validation-api-endpoint "$VALIDATION_API_ENDPOINT"
  )
  if [[ -n "${GCP_CREDS_FILE:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--gcp-creds-file "$GCP_CREDS_FILE")
  fi
  if [[ -n "${VALIDATOR_PROCESS_NAME:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--process-name "$VALIDATOR_PROCESS_NAME")
  fi
  if [[ -n "${VALIDATOR_ENV_FILE:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--env-out "$VALIDATOR_ENV_FILE")
  fi
  if [[ -n "${VALIDATOR_HTTP_PORT:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--http-port "$VALIDATOR_HTTP_PORT")
  fi
  if [[ -n "${WALLET_PATH:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--wallet-path "$WALLET_PATH")
  fi
  if [[ -n "${VALIDATOR_VENV_DIR:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--venv-dir "$VALIDATOR_VENV_DIR")
  fi
  if [[ -n "${CHAIN_ENDPOINT:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--chain-endpoint "$CHAIN_ENDPOINT")
  fi
  if [[ -n "${PROFILE:-}" ]]; then
    VALIDATOR_LAUNCH_ARGS+=(--profile "$PROFILE")
  fi
  if [[ -n "${VALIDATOR_EXTRA_ARGS:-}" ]]; then
    # shellcheck disable=SC2206
    VALIDATOR_LAUNCH_ARGS+=(${VALIDATOR_EXTRA_ARGS})
  fi

  bash "$REPO_ROOT/scripts/validator/process/launch_validator.sh" "${VALIDATOR_LAUNCH_ARGS[@]}"

  log "Update applied successfully: $target_sha"
}

main "$@"
