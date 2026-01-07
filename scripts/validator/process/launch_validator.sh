#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

WALLET_NAME=""
WALLET_HOTKEY=""
NETUID=""

CHAIN_ENDPOINT=""
NETWORK=""
PROFILE=""

PROCESS_NAME=""
ENV_OUT=""
HTTP_HOST="0.0.0.0"
HTTP_PORT="8000"
WALLET_PATH="${BT_WALLET_PATH:-$HOME/.bittensor/wallets}"
VENV_DIR=""
PM2_NAMESPACE="${PM2_NAMESPACE:-}"

VALIDATOR_SA=""
GCP_CREDS_FILE=""

ENABLE_LLM="true"
LLM_FALLBACK="false"
OPENAI_API_KEY_ARG=""
PROMPT_POSTPROCESS="${ALPHACORE_PROMPT_POSTPROCESS:-none}"

SKIP_REGISTER="true"
AUTO_CONFIRM="false"

LOOP_MODE=""
TICK_SECONDS=""
EPOCH_SLOTS=""
EPOCH_SLOT_INDEX=""

VALIDATION_API_ENABLED="true"
VALIDATION_API_ENDPOINT=""

AUTOUPDATE_ENABLED="true"
AUTOUPDATE_INTERVAL="${ALPHACORE_AUTOUPDATE_INTERVAL:-2m}"


usage() {
  cat <<'EOF'
Launch the validator under PM2.

Usage:
  bash scripts/validator/process/launch_validator.sh \
    --wallet-name WALLET --hotkey HOTKEY --netuid NETUID --network NETWORK [options]

Required:
  --wallet-name NAME
  --hotkey NAME
  --netuid NUM
  --network NAME           local|test|finney (used for defaults)

Options:
  --chain-endpoint URL     Default: inferred from --network
  --profile NAME           local|testing|production (default: inferred from --network)
  --validator-sa EMAIL     Service account email to embed in tasks (required; can infer via --gcp-creds-file)
  --gcp-creds-file PATH    Optional: used only to infer --validator-sa (not required to run)

  --validation-api-endpoint URL  Default: http://127.0.0.1:8888
  --disable-validation-api        Set ALPHACORE_VALIDATION_API_ENABLED=false

  --process-name NAME      Default: validator-<hotkey>-<network>
  --pm2-namespace NAME     Default: alphacore (or env PM2_NAMESPACE)
  --env-out PATH           Default: env/<network>/validator-<wallet>-<hotkey>.env
  --http-port PORT         Default: 8000
  --wallet-path PATH       Default: ~/.bittensor/wallets
  --venv-dir PATH          Default: autodetect (../local-development/venv or ./venv)

  --no-autoupdate          Do not enable the auto-update scheduler on this machine
  --autoupdate-interval D  Auto-update interval (default: 2m)

  --disable-llm            Disable LLM prompt generation
  --allow-llm-fallback     Allow deterministic fallback if LLM fails
  --openai-api-key KEY     Write OpenAI key into the env file (chmod 600)
  --prompt-postprocess MODE  Prompt postprocessing: none|minimal|full (default: none)

  --register               Run btcli registration step (default: skipped)
  --yes                    Auto-confirm registration prompts

  --timed                  Run timed rounds (ignore epoch gating)
  --tick-seconds SECONDS   Only with timed rounds
  --epoch-slots N          Split epoch into N windows (default: 4 on test/finney, 1 on local)
  --epoch-slot-index I     Optional explicit window index [0..N-1]
  --validator.epoch_slots N       Alias for --epoch-slots
  --validator.epoch_slot_index I  Alias for --epoch-slot-index

  --help|-h                Show this help
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
  if [[ -n "$VENV_DIR" ]]; then
    return 0
  fi
  if [[ -n "${VENV_DIR_OVERRIDE:-}" ]]; then
    VENV_DIR="${VENV_DIR_OVERRIDE}"
    return 0
  fi
  if [[ -n "${VENV_DIR:-}" ]]; then
    return 0
  fi
  if [[ -d "$REPO_ROOT/../local-development/venv" ]]; then
    VENV_DIR="$REPO_ROOT/../local-development/venv"
    return 0
  fi
  VENV_DIR="$REPO_ROOT/venv"
}

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wallet-name) WALLET_NAME="$2"; shift 2 ;;
    --hotkey) WALLET_HOTKEY="$2"; shift 2 ;;
    --netuid) NETUID="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --chain-endpoint) CHAIN_ENDPOINT="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --validator-sa) VALIDATOR_SA="$2"; shift 2 ;;
    --gcp-creds-file) GCP_CREDS_FILE="$2"; shift 2 ;;

    --validation-api-endpoint) VALIDATION_API_ENDPOINT="$2"; shift 2 ;;
    --disable-validation-api) VALIDATION_API_ENABLED="false"; shift ;;

    --process-name) PROCESS_NAME="$2"; shift 2 ;;
    --pm2-namespace) PM2_NAMESPACE="$2"; shift 2 ;;
    --env-out) ENV_OUT="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --wallet-path) WALLET_PATH="$2"; shift 2 ;;
    --venv-dir) VENV_DIR="$2"; shift 2 ;;

    --no-autoupdate) AUTOUPDATE_ENABLED="false"; shift ;;
    --autoupdate-interval) AUTOUPDATE_INTERVAL="$2"; shift 2 ;;

    --disable-llm) ENABLE_LLM="false"; shift ;;
    --allow-llm-fallback) LLM_FALLBACK="true"; shift ;;
    --openai-api-key) OPENAI_API_KEY_ARG="$2"; shift 2 ;;
    --prompt-postprocess) PROMPT_POSTPROCESS="$2"; shift 2 ;;

    --register) SKIP_REGISTER="false"; shift ;;
    --yes|-y) AUTO_CONFIRM="true"; shift ;;

    --timed) LOOP_MODE="timed"; shift ;;
    --tick-seconds) TICK_SECONDS="$2"; shift 2 ;;
    --epoch-slots) EPOCH_SLOTS="$2"; shift 2 ;;
    --epoch-slot-index) EPOCH_SLOT_INDEX="$2"; shift 2 ;;
    --validator.epoch_slots) EPOCH_SLOTS="$2"; shift 2 ;;
    --validator.epoch_slot_index) EPOCH_SLOT_INDEX="$2"; shift 2 ;;

    --help|-h) usage; exit 0 ;;
    *)
      echo "[launch_validator] Unknown option: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [[ -z "$WALLET_NAME" || -z "$WALLET_HOTKEY" || -z "$NETUID" || -z "$NETWORK" ]]; then
  echo "[launch_validator] Missing required args: --wallet-name, --hotkey, --netuid, --network" >&2
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
  echo "[launch_validator] Invalid --profile: $PROFILE (expected local|testing|production)" >&2
  exit 1
fi

if [[ -z "${EPOCH_SLOTS:-}" ]]; then
  if [[ "$NETWORK" == "local" ]]; then
    EPOCH_SLOTS="1"
  else
    EPOCH_SLOTS="4"
  fi
fi

if [[ -z "$PROCESS_NAME" ]]; then
  PROCESS_NAME="validator-${WALLET_HOTKEY}-${NETWORK}"
fi
if [[ -z "${PM2_NAMESPACE:-}" ]]; then
  PM2_NAMESPACE="alphacore"
fi
if [[ -z "$ENV_OUT" ]]; then
  ENV_OUT="$REPO_ROOT/env/${NETWORK}/validator-${WALLET_NAME}-${WALLET_HOTKEY}.env"
fi
mkdir -p "$(dirname "$ENV_OUT")"

if [[ -z "$CHAIN_ENDPOINT" ]]; then
  CHAIN_ENDPOINT="$(default_chain_endpoint "$NETWORK")"
  if [[ -z "$CHAIN_ENDPOINT" ]]; then
    echo "[launch_validator] No default chain endpoint for --network '$NETWORK'; pass --chain-endpoint explicitly." >&2
    exit 1
  fi
fi
case "$CHAIN_ENDPOINT" in
  ws://*|wss://*) ;;
  *)
    echo "[launch_validator] --chain-endpoint must start with ws:// or wss:// (got: $CHAIN_ENDPOINT)" >&2
    exit 1 ;;
esac

if ! [[ "$NETUID" =~ ^[0-9]+$ ]]; then
  echo "[launch_validator] --netuid must be an integer (got: $NETUID)" >&2
  exit 1
fi
if ! [[ "$HTTP_PORT" =~ ^[0-9]+$ ]]; then
  echo "[launch_validator] --http-port must be an integer (got: $HTTP_PORT)" >&2
  exit 1
fi

if [[ -n "$GCP_CREDS_FILE" && ! -f "$GCP_CREDS_FILE" ]]; then
  echo "[launch_validator] --gcp-creds-file not found: $GCP_CREDS_FILE" >&2
  exit 1
fi

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
  echo "[launch_validator] Missing required --validator-sa (or set ALPHACORE_VALIDATOR_SA)." >&2
  if [[ -n "$GCP_CREDS_FILE" ]]; then
    echo "[launch_validator] Hint: --validator-sa can be inferred from --gcp-creds-file if it contains client_email." >&2
  fi
  exit 1
fi
if is_placeholder_validator_sa "${VALIDATOR_SA}"; then
  echo "[launch_validator] --validator-sa appears to be a placeholder: ${VALIDATOR_SA}" >&2
  exit 1
fi
if ! [[ "$VALIDATOR_SA" =~ .+@.+\..+ ]]; then
  echo "[launch_validator] --validator-sa must look like an email address (got: $VALIDATOR_SA)" >&2
  exit 1
fi

if [[ -z "$VALIDATION_API_ENDPOINT" ]]; then
  VALIDATION_API_ENDPOINT="http://127.0.0.1:8888"
fi
case "$VALIDATION_API_ENDPOINT" in
  http://*|https://*) ;;
  *)
    echo "[launch_validator] --validation-api-endpoint must start with http:// or https:// (got: $VALIDATION_API_ENDPOINT)" >&2
    exit 1 ;;
esac

if [[ "$ENABLE_LLM" == "true" ]]; then
  OPENAI_KEY_EFFECTIVE="${OPENAI_API_KEY_ARG:-${ALPHACORE_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}}"
  if [[ -z "$OPENAI_KEY_EFFECTIVE" ]]; then
    echo "[launch_validator] Missing OpenAI key for LLM task prompt generation." >&2
    echo "[launch_validator] Set OPENAI_API_KEY or ALPHACORE_OPENAI_API_KEY," >&2
    echo "[launch_validator] or pass --openai-api-key, or use --disable-llm." >&2
    exit 1
  fi
fi

case "${PROMPT_POSTPROCESS}" in
  none|minimal|full) ;;
  *)
    echo "[launch_validator] Invalid --prompt-postprocess: ${PROMPT_POSTPROCESS} (expected none|minimal|full)" >&2
    exit 1
    ;;
esac

infer_validator_venv

CONFIG_PATH="$REPO_ROOT/modules/task_config.yaml"
cat >"$ENV_OUT" <<ENVFILE
# Auto-generated by scripts/validator/process/launch_validator.sh

ALPHACORE_ENV="${PROFILE}"
ALPHACORE_NETWORK="${NETWORK}"
BT_NETWORK="${NETWORK}"
ALPHACORE_CHAIN_ENDPOINT="${CHAIN_ENDPOINT}"
ALPHACORE_NETUID="${NETUID}"

ALPHACORE_WALLET_NAME="${WALLET_NAME}"
ALPHACORE_WALLET_HOTKEY="${WALLET_HOTKEY}"
BT_WALLET_PATH="${WALLET_PATH}"
ALPHACORE_WALLET_PATH="${WALLET_PATH}"

ALPHACORE_HTTP_HOST="${HTTP_HOST}"
ALPHACORE_HTTP_PORT="${HTTP_PORT}"
ALPHACORE_CONFIG="${CONFIG_PATH}"
ALPHACORE_VALIDATOR_SA="${VALIDATOR_SA}"

ALPHACORE_BT_LOGGING_LEVEL="info"

ALPHACORE_VALIDATION_API_ENABLED="${VALIDATION_API_ENABLED}"
ALPHACORE_VALIDATION_API_ENDPOINT="${VALIDATION_API_ENDPOINT}"
ALPHACORE_FAIL_FAST_ON_VALIDATION_API="true"

ALPHACORE_LOCAL_TEST_MODE="false"
ALPHACORE_LOCAL_MINERS_FALLBACK="false"

ALPHACORE_MINER_CONCURRENCY="128"
ALPHACORE_MINER_RESPONSE_TIMEOUT_SECONDS="10"
ALPHACORE_HANDSHAKE_TIMEOUT_SECONDS="5"
ALPHACORE_TASK_SYNAPSE_TIMEOUT_SECONDS="1800"
ALPHACORE_DISPATCH_PROGRESS_LOG_INTERVAL_S="30"

ALPHACORE_PRE_GENERATED_TASKS="0"

ALPHACORE_ENABLE_LLM="${ENABLE_LLM}"
ALPHACORE_LLM_FALLBACK="${LLM_FALLBACK}"
ALPHACORE_PROMPT_POSTPROCESS="${PROMPT_POSTPROCESS}"
ENVFILE

if [[ -n "$LOOP_MODE" ]]; then
  printf 'ALPHACORE_LOOP_MODE="%s"\n' "$LOOP_MODE" >>"$ENV_OUT"
fi
if [[ -n "$TICK_SECONDS" ]]; then
  printf 'ALPHACORE_TICK_SECONDS="%s"\n' "$TICK_SECONDS" >>"$ENV_OUT"
fi
if [[ -n "$EPOCH_SLOTS" ]]; then
  printf 'ALPHACORE_EPOCH_SLOTS="%s"\n' "$EPOCH_SLOTS" >>"$ENV_OUT"
fi
if [[ -n "$EPOCH_SLOT_INDEX" ]]; then
  printf 'ALPHACORE_EPOCH_SLOT_INDEX="%s"\n' "$EPOCH_SLOT_INDEX" >>"$ENV_OUT"
fi

chmod 600 "$ENV_OUT" || true

if [[ -n "$OPENAI_API_KEY_ARG" ]]; then
  printf 'ALPHACORE_OPENAI_API_KEY="%s"\n' "$OPENAI_API_KEY_ARG" >>"$ENV_OUT"
fi

LAUNCH_ARGS=(--env-file "$ENV_OUT" --process-name "$PROCESS_NAME" --venv-dir "$VENV_DIR")
if [[ "$SKIP_REGISTER" == "true" ]]; then
  LAUNCH_ARGS+=(--skip-register)
fi
if [[ "$AUTO_CONFIRM" == "true" ]]; then
  LAUNCH_ARGS+=(--yes)
fi

PM2_NAMESPACE="$PM2_NAMESPACE" bash "$REPO_ROOT/scripts/validator/process/launch_pm2.sh" "${LAUNCH_ARGS[@]}"

if [[ "$AUTOUPDATE_ENABLED" == "true" ]]; then
  AUTOUPDATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/alphacore/autoupdate"
  mkdir -p "$AUTOUPDATE_DIR"
  AUTOUPDATE_CONFIG="${AUTOUPDATE_DIR}/${PM2_NAMESPACE}.env"

  VALIDATOR_EXTRA_ARGS=()
  if [[ "${LOOP_MODE:-}" == "timed" ]]; then
    VALIDATOR_EXTRA_ARGS+=(--timed)
  fi
  if [[ -n "${TICK_SECONDS:-}" ]]; then
    VALIDATOR_EXTRA_ARGS+=(--tick-seconds "$TICK_SECONDS")
  fi
  if [[ -n "${EPOCH_SLOTS:-}" ]]; then
    VALIDATOR_EXTRA_ARGS+=(--epoch-slots "$EPOCH_SLOTS")
  fi
  if [[ -n "${EPOCH_SLOT_INDEX:-}" ]]; then
    VALIDATOR_EXTRA_ARGS+=(--epoch-slot-index "$EPOCH_SLOT_INDEX")
  fi

  {
    echo "NETWORK=${NETWORK}"
    echo "PM2_NAMESPACE=${PM2_NAMESPACE}"
    echo "WALLET_NAME=${WALLET_NAME}"
    echo "WALLET_HOTKEY=${WALLET_HOTKEY}"
    echo "NETUID=${NETUID}"
    if [[ -n "${GCP_CREDS_FILE:-}" ]]; then
      echo "GCP_CREDS_FILE=${GCP_CREDS_FILE}"
    fi
    if [[ -n "${VALIDATION_API_ENDPOINT:-}" ]]; then
      echo "VALIDATION_API_ENDPOINT=${VALIDATION_API_ENDPOINT}"
    fi
    if [[ -n "${CHAIN_ENDPOINT:-}" ]]; then
      echo "CHAIN_ENDPOINT=${CHAIN_ENDPOINT}"
    fi
    if [[ -n "${PROFILE:-}" ]]; then
      echo "PROFILE=${PROFILE}"
    fi
    if [[ -n "${WALLET_PATH:-}" ]]; then
      echo "WALLET_PATH=${WALLET_PATH}"
    fi
    if [[ -n "${VENV_DIR:-}" ]]; then
      echo "VALIDATOR_VENV_DIR=${VENV_DIR}"
    fi
    if [[ -n "${OPENAI_API_KEY_ARG:-}" ]]; then
      echo "ALPHACORE_OPENAI_API_KEY=${OPENAI_API_KEY_ARG}"
    elif [[ -n "${ALPHACORE_OPENAI_API_KEY:-}" ]]; then
      echo "ALPHACORE_OPENAI_API_KEY=${ALPHACORE_OPENAI_API_KEY}"
    elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
      echo "ALPHACORE_OPENAI_API_KEY=${OPENAI_API_KEY}"
    fi
    if ((${#VALIDATOR_EXTRA_ARGS[@]} > 0)); then
      printf 'VALIDATOR_EXTRA_ARGS="%s"\n' "${VALIDATOR_EXTRA_ARGS[*]}"
    fi
    echo 'VALIDATOR_ROUND_LOCKFILE=/tmp/alphacore-validator-round.lock'
  } >"$AUTOUPDATE_CONFIG"
  chmod 600 "$AUTOUPDATE_CONFIG" || true

  bash "$REPO_ROOT/scripts/validator/process/ensure_autoupdate_timer.sh" \
    --pm2-namespace "$PM2_NAMESPACE" \
    --config "$AUTOUPDATE_CONFIG" \
    --interval "$AUTOUPDATE_INTERVAL" || true
fi

exit 0
