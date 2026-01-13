#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# REPO_ROOT points to the repository root.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

WALLET_NAME=""
WALLET_HOTKEY=""
NETUID=""

CHAIN_ENDPOINT=""
NETWORK=""
PROFILE=""

VALIDATOR_PROCESS_NAME=""
VALIDATOR_ENV_OUT=""
HTTP_HOST="0.0.0.0"
HTTP_PORT="8000"

VALIDATION_PROCESS_NAME=""
VALIDATION_ENV_OUT=""
VALIDATION_HTTP_HOST="127.0.0.1"
VALIDATION_HTTP_PORT="8888"
VALIDATION_API_ENDPOINT=""
GCP_CREDS_FILE=""

WALLET_PATH="${BT_WALLET_PATH:-$HOME/.bittensor/wallets}"
VALIDATOR_VENV_DIR=""
VALIDATION_VENV_DIR=""

VALIDATOR_SA=""

ENABLE_LLM="true"
LLM_FALLBACK="false"
OPENAI_API_KEY_ARG=""
AUTO_CONFIRM="false"
AUTO_INSTALL_PM2="0"
SKIP_REGISTER="true"
LOOP_MODE=""
TICK_SECONDS=""
EPOCH_SLOTS=""
EPOCH_SLOT_INDEX=""

usage() {
  cat <<'EOF'
Launch AlphaCore Validation API (PM2) then a validator (PM2) on any network.

This script is meant for real deployments: it enables the validator's validation
API integration and starts the sandbox Validation API first.

Prereqs (one-time, on the host):
  sudo bash modules/evaluation/validation/sandbox/setup.sh
  # plus your GCP service account JSON key (or GOOGLE_OAUTH_ACCESS_TOKEN)

Usage:
  bash scripts/validator/process/launch_with_validation_api.sh \
    --wallet-name WALLET --hotkey HOTKEY --netuid NETUID \
    --network NETWORK_ALIAS [options]

New split scripts:
  bash scripts/validator/process/launch_validation_api.sh --gcp-creds-file PATH [options]
  bash scripts/validator/process/launch_validator.sh --wallet-name WALLET --hotkey HOTKEY --netuid NETUID --network NETWORK [options]

Required:
  --wallet-name NAME       Coldkey wallet name
  --hotkey NAME            Hotkey name
  --netuid NUM             Subnet netuid
  --network NAME           Network alias (local|test|finney)

Optional chain selection:
  --chain-endpoint URL     Subtensor websocket endpoint (ws:// or wss://)
                           Default: inferred from --network

Validation API auth (required):
  --gcp-creds-file PATH    Service account JSON key for token minting
    OR set GOOGLE_OAUTH_ACCESS_TOKEN in the environment (not written to disk)

Options:
  --profile NAME           Validator profile: local|testing|production (default: inferred)
  --validator-sa EMAIL     Service account email to embed in tasks (required)
  --validator-process NAME Default: validator-<hotkey>-<network>
  --validation-process NAME Default: alphacore-validation-api-<network>
  --validator-env-out PATH  Default: env/<network>/validator-<wallet>-<hotkey>.env
  --validation-env-out PATH Default: env/<network>/validation-api.env
  --http-port PORT         Validator task API port (default: 8000)
  --validation-port PORT   Validation API port (default: 8888)
  --validation-bind-host HOST Validation API bind host (default: 127.0.0.1)
  --validation-api-endpoint URL Validator's API URL (default: http://<bind-host>:<port>)
  --wallet-path PATH       Default: ~/.bittensor/wallets
  --validator-venv-dir PATH Default: autodetect (../local-development/venv or ./venv)
  --validation-venv-dir PATH Default: <repo>/.venv-validation-api
  --disable-llm            Disable LLM prompt generation (use deterministic fallback)
  --allow-llm-fallback     Allow deterministic fallback if LLM fails (default: disabled)
  --openai-api-key KEY     Write OpenAI key into the validator env file (chmod 600)
  --auto-install-pm2       Install PM2 if missing (uses install_pm2.sh)
  --yes                    Auto-confirm registration prompts
  --register               Run btcli registration step (default: skipped)
  --timed                  Run timed rounds (ignore epoch gating)
  --tick-seconds SECONDS   Only with timed rounds (default: validator internal default)
  --epoch-slots N           In epoch mode, split the epoch into N windows and only start
                            a round inside this validator's window (helps stagger multiple validators).
                            Default: 4 on test/finney, 1 on local.
  --epoch-slot-index I      Optional explicit window index [0..N-1]. Default: derived from UID/hotkey.

Examples:
  # Testnet
  bash scripts/validator/process/launch_with_validation_api.sh \
    --wallet-name mycold --hotkey myhot --netuid 123 \
    --chain-endpoint wss://testnet-finney.opentensor.io:443 --network test \
    --gcp-creds-file /secure/gcp-creds.json --yes

  # Mainnet
  bash scripts/validator/process/launch_with_validation_api.sh \
    --wallet-name mycold --hotkey myhot --netuid 123 \
    --chain-endpoint wss://finney.opentensor.io:443 --network finney \
    --gcp-creds-file /secure/gcp-creds.json --yes
EOF
}

infer_profile() {
  local network="$1"
  case "$network" in
    local) echo "local" ;;
    test) echo "testing" ;;
    finney) echo "production" ;;
    *) echo "production" ;;
  esac
}

default_chain_endpoint() {
  local network="$1"
  case "$network" in
    local) echo "ws://127.0.0.1:9944" ;;
    test) echo "wss://testnet-finney.opentensor.io:443" ;;
    finney) echo "wss://finney.opentensor.io:443" ;;
    *) echo "" ;;
  esac
}

infer_validator_venv() {
  if [[ -n "$VALIDATOR_VENV_DIR" ]]; then
    return 0
  fi
  if [[ -n "${VENV_DIR:-}" ]]; then
    VALIDATOR_VENV_DIR="${VENV_DIR}"
    return 0
  fi
  if [[ -d "$REPO_ROOT/../local-development/venv" ]]; then
    VALIDATOR_VENV_DIR="$REPO_ROOT/../local-development/venv"
    return 0
  fi
  VALIDATOR_VENV_DIR="$REPO_ROOT/venv"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wallet-name) WALLET_NAME="$2"; shift 2 ;;
    --hotkey) WALLET_HOTKEY="$2"; shift 2 ;;
    --netuid) NETUID="$2"; shift 2 ;;
    --chain-endpoint) CHAIN_ENDPOINT="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --validator-sa) VALIDATOR_SA="$2"; shift 2 ;;
    --validator-process) VALIDATOR_PROCESS_NAME="$2"; shift 2 ;;
    --validation-process) VALIDATION_PROCESS_NAME="$2"; shift 2 ;;
    --validator-env-out) VALIDATOR_ENV_OUT="$2"; shift 2 ;;
    --validation-env-out) VALIDATION_ENV_OUT="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --validation-port) VALIDATION_HTTP_PORT="$2"; shift 2 ;;
    --validation-bind-host) VALIDATION_HTTP_HOST="$2"; shift 2 ;;
    --validation-api-endpoint) VALIDATION_API_ENDPOINT="$2"; shift 2 ;;
    --wallet-path) WALLET_PATH="$2"; shift 2 ;;
    --validator-venv-dir) VALIDATOR_VENV_DIR="$2"; shift 2 ;;
    --validation-venv-dir) VALIDATION_VENV_DIR="$2"; shift 2 ;;
    --gcp-creds-file) GCP_CREDS_FILE="$2"; shift 2 ;;
    --disable-llm) ENABLE_LLM="false"; shift ;;
    --allow-llm-fallback) LLM_FALLBACK="true"; shift ;;
    --openai-api-key) OPENAI_API_KEY_ARG="$2"; shift 2 ;;
    --auto-install-pm2) AUTO_INSTALL_PM2="1"; shift ;;
    --register) SKIP_REGISTER="false"; shift ;;
    --timed) LOOP_MODE="timed"; shift ;;
    --tick-seconds) TICK_SECONDS="$2"; shift 2 ;;
    --epoch-slots) EPOCH_SLOTS="$2"; shift 2 ;;
    --epoch-slot-index) EPOCH_SLOT_INDEX="$2"; shift 2 ;;
    --yes|-y) AUTO_CONFIRM="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "[launch_with_validation_api] Unknown option: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [[ -z "$WALLET_NAME" || -z "$WALLET_HOTKEY" || -z "$NETUID" ]]; then
  echo "[launch_with_validation_api] Missing required args: --wallet-name, --hotkey, --netuid" >&2
  usage
  exit 1
fi
if [[ -z "$NETWORK" ]]; then
  echo "[launch_with_validation_api] Missing required args: --network" >&2
  usage
  exit 1
fi

# Default to epoch mode unless explicitly overridden with --timed.
if [[ -z "${LOOP_MODE:-}" ]]; then
  LOOP_MODE="epoch"
fi

if [[ -z "$PROFILE" ]]; then
  PROFILE="$(infer_profile "$NETWORK")"
fi
if [[ "$PROFILE" != "local" && "$PROFILE" != "testing" && "$PROFILE" != "production" ]]; then
  echo "[launch_with_validation_api] Invalid --profile: $PROFILE (expected local|testing|production)" >&2
  exit 1
fi

# Default epoch slot staggering for real networks (can override with --epoch-slots 1).
if [[ -z "${EPOCH_SLOTS:-}" ]]; then
  if [[ "$NETWORK" == "local" ]]; then
    EPOCH_SLOTS="1"
  else
    EPOCH_SLOTS="4"
  fi
fi

if [[ -z "$VALIDATION_VENV_DIR" ]]; then
  VALIDATION_VENV_DIR="$REPO_ROOT/.venv-validation-api"
fi

if [[ -z "$VALIDATOR_PROCESS_NAME" ]]; then
  VALIDATOR_PROCESS_NAME="validator-${WALLET_HOTKEY}-${NETWORK}"
fi
if [[ -z "$VALIDATION_PROCESS_NAME" ]]; then
  VALIDATION_PROCESS_NAME="alphacore-validation-api-${NETWORK}"
fi

if [[ -z "$VALIDATOR_ENV_OUT" ]]; then
  VALIDATOR_ENV_OUT="$REPO_ROOT/env/${NETWORK}/validator-${WALLET_NAME}-${WALLET_HOTKEY}.env"
fi
if [[ -z "$VALIDATION_ENV_OUT" ]]; then
  VALIDATION_ENV_OUT="$REPO_ROOT/env/${NETWORK}/validation-api.env"
fi

mkdir -p "$(dirname "$VALIDATOR_ENV_OUT")"
mkdir -p "$(dirname "$VALIDATION_ENV_OUT")"

VALIDATION_API_ENDPOINT="${VALIDATION_API_ENDPOINT:-http://${VALIDATION_HTTP_HOST}:${VALIDATION_HTTP_PORT}}"

if [[ -z "$CHAIN_ENDPOINT" ]]; then
  CHAIN_ENDPOINT="$(default_chain_endpoint "$NETWORK")"
  if [[ -z "$CHAIN_ENDPOINT" ]]; then
    echo "[launch_with_validation_api] No default chain endpoint for --network '$NETWORK'; pass --chain-endpoint explicitly." >&2
    exit 1
  fi
fi

case "$CHAIN_ENDPOINT" in
  ws://*|wss://*) ;;
  *)
    echo "[launch_with_validation_api] --chain-endpoint must start with ws:// or wss:// (got: $CHAIN_ENDPOINT)" >&2
    exit 1 ;;
esac

if ! [[ "$NETUID" =~ ^[0-9]+$ ]]; then
  echo "[launch_with_validation_api] --netuid must be an integer (got: $NETUID)" >&2
  exit 1
fi
if ! [[ "$HTTP_PORT" =~ ^[0-9]+$ ]]; then
  echo "[launch_with_validation_api] --http-port must be an integer (got: $HTTP_PORT)" >&2
  exit 1
fi
if ! [[ "$VALIDATION_HTTP_PORT" =~ ^[0-9]+$ ]]; then
  echo "[launch_with_validation_api] --validation-port must be an integer (got: $VALIDATION_HTTP_PORT)" >&2
  exit 1
fi
if [[ -n "$TICK_SECONDS" ]] && ! [[ "$TICK_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "[launch_with_validation_api] --tick-seconds must be a number (got: $TICK_SECONDS)" >&2
  exit 1
fi

case "$VALIDATION_API_ENDPOINT" in
  http://*|https://*) ;;
  *)
    echo "[launch_with_validation_api] --validation-api-endpoint must start with http:// or https:// (got: $VALIDATION_API_ENDPOINT)" >&2
    exit 1 ;;
esac

if [[ -z "$GCP_CREDS_FILE" && -z "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ]]; then
  echo "[launch_with_validation_api] Missing Validation API auth: pass --gcp-creds-file or set GOOGLE_OAUTH_ACCESS_TOKEN" >&2
  exit 1
fi
if [[ -n "$GCP_CREDS_FILE" && ! -f "$GCP_CREDS_FILE" ]]; then
  echo "[launch_with_validation_api] --gcp-creds-file not found: $GCP_CREDS_FILE" >&2
  exit 1
fi

infer_validator_sa_from_creds() {
  local creds_file="$1"
  if [[ -z "$creds_file" || ! -f "$creds_file" ]]; then
    return 0
  fi
  python3 - "$creds_file" 2>/dev/null <<'PY' || true
import json
import sys

path = sys.argv[1]
try:
  with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
  email = (data.get("client_email") or "").strip()
  if email:
    print(email)
except Exception:
  pass
PY
}

is_placeholder_validator_sa() {
  local sa="$1"
  case "$sa" in
    ""|"validator@example.com"|"your-validator-sa@project.iam.gserviceaccount.com")
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

if [[ -z "$VALIDATOR_SA" ]]; then
  VALIDATOR_SA="${ALPHACORE_VALIDATOR_SA:-}"
fi
if is_placeholder_validator_sa "${VALIDATOR_SA}"; then
  inferred_sa="$(infer_validator_sa_from_creds "$GCP_CREDS_FILE")"
  if [[ -n "${inferred_sa:-}" ]]; then
    VALIDATOR_SA="$inferred_sa"
  fi
fi
if [[ -z "$VALIDATOR_SA" ]]; then
  echo "[launch_with_validation_api] Missing required --validator-sa (or set ALPHACORE_VALIDATOR_SA in the environment)." >&2
  if [[ -n "$GCP_CREDS_FILE" ]]; then
    echo "[launch_with_validation_api] Hint: --validator-sa can be inferred from --gcp-creds-file if it contains client_email." >&2
  fi
  exit 1
fi
if is_placeholder_validator_sa "${VALIDATOR_SA}"; then
  echo "[launch_with_validation_api] --validator-sa appears to be a placeholder: ${VALIDATOR_SA}" >&2
  echo "[launch_with_validation_api] Pass a real service account email (or provide --gcp-creds-file with client_email to infer it)." >&2
  exit 1
fi
if ! [[ "$VALIDATOR_SA" =~ .+@.+\..+ ]]; then
  echo "[launch_with_validation_api] --validator-sa must look like an email address (got: $VALIDATOR_SA)" >&2
  exit 1
fi

if [[ "$ENABLE_LLM" == "true" ]]; then
  OPENAI_KEY_EFFECTIVE="${OPENAI_API_KEY_ARG:-${ALPHACORE_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}}"
  if [[ -z "$OPENAI_KEY_EFFECTIVE" ]]; then
    echo "[launch_with_validation_api] Missing OpenAI key for LLM task prompt generation." >&2
    echo "[launch_with_validation_api] Set OPENAI_API_KEY or ALPHACORE_OPENAI_API_KEY in your environment," >&2
    echo "[launch_with_validation_api] or pass --openai-api-key to write it to the validator env file, or use --disable-llm." >&2
    exit 1
  fi
fi

if [[ ! -e /dev/kvm ]]; then
  echo "[launch_with_validation_api] WARNING: /dev/kvm not found; the sandbox Validation API will not be able to run Firecracker jobs." >&2
  echo "[launch_with_validation_api] Run the one-time setup: sudo bash $REPO_ROOT/modules/evaluation/validation/sandbox/setup.sh" >&2
fi

# ----------------------------
# 1) Start Validation API (PM2)
# ----------------------------
VALIDATION_ARGS=(--process-name "$VALIDATION_PROCESS_NAME" --env-out "$VALIDATION_ENV_OUT")
VALIDATION_ARGS+=(--venv-dir "$VALIDATION_VENV_DIR" --bind-host "$VALIDATION_HTTP_HOST" --port "$VALIDATION_HTTP_PORT")
if [[ -n "$GCP_CREDS_FILE" ]]; then
  VALIDATION_ARGS+=(--gcp-creds-file "$GCP_CREDS_FILE")
fi
if [[ "$AUTO_INSTALL_PM2" == "1" ]]; then
  VALIDATION_ARGS+=(--auto-install-pm2)
fi

bash "$REPO_ROOT/scripts/validator/process/launch_validation_api.sh" \
  "${VALIDATION_ARGS[@]}"

# ----------------------------
# 2) Launch validator (PM2)
# ----------------------------
VALIDATOR_ARGS=(
  --wallet-name "$WALLET_NAME"
  --hotkey "$WALLET_HOTKEY"
  --netuid "$NETUID"
  --network "$NETWORK"
  --chain-endpoint "$CHAIN_ENDPOINT"
  --profile "$PROFILE"
  --validator-sa "$VALIDATOR_SA"
  --process-name "$VALIDATOR_PROCESS_NAME"
  --env-out "$VALIDATOR_ENV_OUT"
  --http-port "$HTTP_PORT"
  --wallet-path "$WALLET_PATH"
  --venv-dir "$VALIDATOR_VENV_DIR"
  --validation-api-endpoint "$VALIDATION_API_ENDPOINT"
)

if [[ -n "$GCP_CREDS_FILE" ]]; then
  VALIDATOR_ARGS+=(--gcp-creds-file "$GCP_CREDS_FILE")
fi
if [[ "$ENABLE_LLM" != "true" ]]; then
  VALIDATOR_ARGS+=(--disable-llm)
fi
if [[ "$LLM_FALLBACK" == "true" ]]; then
  VALIDATOR_ARGS+=(--allow-llm-fallback)
fi
if [[ -n "$OPENAI_API_KEY_ARG" ]]; then
  VALIDATOR_ARGS+=(--openai-api-key "$OPENAI_API_KEY_ARG")
fi
if [[ "$SKIP_REGISTER" != "true" ]]; then
  VALIDATOR_ARGS+=(--register)
fi
if [[ "$AUTO_CONFIRM" == "true" ]]; then
  VALIDATOR_ARGS+=(--yes)
fi
if [[ -n "$LOOP_MODE" && "$LOOP_MODE" == "timed" ]]; then
  VALIDATOR_ARGS+=(--timed)
fi
if [[ -n "$TICK_SECONDS" ]]; then
  VALIDATOR_ARGS+=(--tick-seconds "$TICK_SECONDS")
fi
if [[ -n "$EPOCH_SLOTS" ]]; then
  VALIDATOR_ARGS+=(--epoch-slots "$EPOCH_SLOTS")
fi
if [[ -n "$EPOCH_SLOT_INDEX" ]]; then
  VALIDATOR_ARGS+=(--epoch-slot-index "$EPOCH_SLOT_INDEX")
fi

exec bash "$REPO_ROOT/scripts/validator/process/launch_validator.sh" \
  "${VALIDATOR_ARGS[@]}"
