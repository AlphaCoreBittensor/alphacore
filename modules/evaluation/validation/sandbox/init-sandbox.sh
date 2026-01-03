#!/bin/sh
# Unified guest init script: mounts workspace dir, enforces egress policy, and runs validator.
set -e
exec > /dev/ttyS0 2>&1

echo "[Guest] Setting up minimal mounts (read-only rootfs)"
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /run

# Rootfs is read-only; provide a writable /var for tools like dhclient.
if [ -d /var ]; then
    mount -t tmpfs tmpfs /var 2>/dev/null || true
    mkdir -p /var/lib/dhcp /var/run 2>/dev/null || true
fi

# Writable resolv.conf on tmpfs, bind-mounted over the read-only rootfs copy.
echo "nameserver 172.16.0.1" > /run/resolv.conf
mount --bind /run/resolv.conf /etc/resolv.conf

echo "[Guest] System Booted. User ID: $(id -u)"

# Disable IPv6 inside the guest to avoid AAAA/proxy edge cases.
if [ -f /proc/sys/net/ipv6/conf/all/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || true
fi
if [ -f /proc/sys/net/ipv6/conf/default/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/default/disable_ipv6 2>/dev/null || true
fi
if [ -f /proc/sys/net/ipv6/conf/eth0/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/eth0/disable_ipv6 2>/dev/null || true
fi

PROXY_URL="${PROXY_URL:-http://172.16.0.1:8888}"
ACORE_NET_CHECKS="${ACORE_NET_CHECKS:-0}"
ACORE_NET_CHECK_TIMEOUT="${ACORE_NET_CHECK_TIMEOUT:-5}"
ACORE_STATIC_IP="${ACORE_STATIC_IP:-}"
ACORE_STATIC_GW="${ACORE_STATIC_GW:-172.16.0.1}"
ACORE_STATIC_DNS="${ACORE_STATIC_DNS:-172.16.0.1}"

# Ensure terraform bundle path is included (keeps net-checks + runners consistent).
export PATH="/opt/acore-sandbox-bundle/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

CMDLINE="$(cat /proc/cmdline 2>/dev/null || true)"
for arg in $CMDLINE; do
    case "$arg" in
        acore_net_checks=*)
            ACORE_NET_CHECKS="${arg#acore_net_checks=}"
            ;;
        acore_net_check_timeout=*)
            ACORE_NET_CHECK_TIMEOUT="${arg#acore_net_check_timeout=}"
            ;;
        acore_static_ip=*)
            ACORE_STATIC_IP="${arg#acore_static_ip=}"
            ;;
        acore_static_gw=*)
            ACORE_STATIC_GW="${arg#acore_static_gw=}"
            ;;
        acore_static_dns=*)
            ACORE_STATIC_DNS="${arg#acore_static_dns=}"
            ;;
    esac
done

WORKSPACE_DEVICE="/dev/vdb"
WORKSPACE_RW_DEVICE="/dev/vdc"
RESULTS_DEVICE="/dev/vdd"
VALIDATOR_DEVICE="/dev/vde"
SRC_WORKDIR="/run/workspace-src"
SCRATCH_MNT="/run/workspace-scratch"
OVERLAY_WORKDIR="/run/workspace-ovl-work"
WORKDIR="/run/workspace-run"
RESULTS_DIR="/run/results"
VALIDATOR_DIR="/tmp/validator"
WORKSPACE_PATH="${WORKSPACE_PATH:-}"
SKIP_TF="${SKIP_TF:-0}"
GUEST_RUNNER="/tmp/validator/modules/evaluation/validation/sandbox/guest_runner.py"
ERROR_CONTEXT="Init failure"

ip link set lo up
ip link set eth0 up

DHCP_TIMEOUT="${DHCP_TIMEOUT:-10}"
UDHCPC_TRIES="${UDHCPC_TRIES:-8}"
UDHCPC_INTERVAL="${UDHCPC_INTERVAL:-1}"
ACORE_DHCP_JITTER_MS="${ACORE_DHCP_JITTER_MS:-0}"
if [ -n "$ACORE_STATIC_IP" ]; then
    static_cidr="$ACORE_STATIC_IP"
    case "$static_cidr" in
        */*) ;;
        *) static_cidr="${static_cidr}/24" ;;
    esac
    echo "[Guest] Configuring static IPv4 on eth0: ${static_cidr} (gw=${ACORE_STATIC_GW}, dns=${ACORE_STATIC_DNS})"
    ip addr flush dev eth0 2>/dev/null || true
    ip addr add "${static_cidr}" dev eth0
    if [ -n "${ACORE_STATIC_GW}" ]; then
        ip route replace default via "${ACORE_STATIC_GW}" dev eth0
    fi
    echo "nameserver ${ACORE_STATIC_DNS}" > /run/resolv.conf
else
echo "[Guest] Bringing up eth0 via DHCP (udhcpc, timeout ${DHCP_TIMEOUT}s, tries=${UDHCPC_TRIES}, interval=${UDHCPC_INTERVAL}s)..."
cat > /run/udhcpc.sh <<'EOF'
#!/bin/sh
set -eu

iface="${interface:-eth0}"

case "${1:-}" in
  deconfig)
    ip addr flush dev "${iface}" 2>/dev/null || true
    ;;
  bound|renew)
    ip addr flush dev "${iface}" 2>/dev/null || true
    ip addr add "${ip}/${mask}" dev "${iface}"

    # router can contain multiple IPs; take the first.
    gw="$(printf '%s\n' "${router:-}" | awk '{print $1}')"
    if [ -n "${gw}" ]; then
      ip route replace default via "${gw}" dev "${iface}"
    fi
    ;;
esac

exit 0
EOF
chmod +x /run/udhcpc.sh

tries=0
while [ "$tries" -lt 3 ]; do
    tries=$((tries + 1))
    if [ ! -x /sbin/udhcpc ]; then
        echo "[Guest] ERROR: /sbin/udhcpc missing. Rebuild the rootfs by re-running setup.sh."
        echo "[Guest] TEST_FAILED"
        exit 1
    fi
    if [ "${ACORE_DHCP_JITTER_MS}" -gt 0 ] 2>/dev/null; then
        mac="$(cat /sys/class/net/eth0/address 2>/dev/null || echo "")"
        if [ -n "$mac" ]; then
            delay="$(printf '%s' "$mac" | cksum | awk -v m="${ACORE_DHCP_JITTER_MS}" '{printf "%.3f", ($1 % m) / 1000.0}')"
            sleep "$delay" 2>/dev/null || true
        fi
    fi
    if /sbin/udhcpc -i eth0 -q -t "${UDHCPC_TRIES}" -T "${UDHCPC_INTERVAL}" -n -s /run/udhcpc.sh; then
        break
    fi
    sleep 1
done

if ! ip -4 addr show dev eth0 2>/dev/null | grep -q "inet "; then
    echo "[Guest] ERROR: DHCP failed on eth0."
    echo "[Guest] TEST_FAILED"
    exit 1
fi

# Ensure sandbox DNS remains pinned to the host-side dnsmasq.
echo "nameserver 172.16.0.1" > /run/resolv.conf
fi

if [ -n "$PROXY_URL" ]; then
    export http_proxy="$PROXY_URL"
    export https_proxy="$PROXY_URL"
    echo "[Guest] Proxy configured: $PROXY_URL"
fi

fail_net_check() {
    echo "[Guest] ERROR: $1"
    echo "[Guest] TEST_FAILED"
    exit 1
}

run_net_checks() {
    echo "[Guest] Running network checks (ACORE_NET_CHECKS=1)..."
    if ! command -v curl >/dev/null 2>&1; then
        fail_net_check "curl missing; cannot run network checks."
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        fail_net_check "python3 missing; cannot run DNS checks."
    fi
    if ! command -v terraform >/dev/null 2>&1; then
        fail_net_check "terraform missing; rebuild rootfs/bundle via setup.sh."
    fi
    if [ ! -f /acore-net-checks.py ]; then
        fail_net_check "acore-net-checks.py missing in guest; host injection is broken."
    fi

    echo "[Guest] terraform --version:"
    terraform --version || true
    echo "[Guest] python3 --version:"
    python3 --version || true

    echo "[Guest] ip -4 addr:"
    ip -4 addr show || true
    echo "[Guest] ip route:"
    ip route show || true

    ERROR_CONTEXT="Network policy checks failed"
    if ! python3 /acore-net-checks.py --timeout "${ACORE_NET_CHECK_TIMEOUT}" --proxy-url "${PROXY_URL}"; then
        echo "[Guest] TEST_FAILED"
        exit 1
    fi

    echo "[Guest] Network checks passed."
}

if [ "$ACORE_NET_CHECKS" = "1" ]; then
    ERROR_CONTEXT="Network checks failed"
    run_net_checks
fi

write_error_json() {
    # Best-effort error.json writer without Python/JQ dependencies.
    msg="$1"
    score="${2:-}"
    [ -z "$RESULTS_DIR" ] && return
    mkdir -p "$RESULTS_DIR" 2>/dev/null || true
    escaped=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
    msg_file="$RESULTS_DIR/error.json"
    printf '{ "status": "fail", "msg": "%s"' "$escaped" >"$msg_file" 2>/dev/null || true
    if [ -n "$score" ]; then
        case "$score" in
            '' ) ;;
            *[!0-9.]* ) printf ', "score": "%s"' "$score" >>"$msg_file" 2>/dev/null || true ;;
            * ) printf ', "score": %s' "$score" >>"$msg_file" 2>/dev/null || true ;;
        esac
    fi
    printf ' }\n' >>"$msg_file" 2>/dev/null || true
    sync || true
}

on_exit() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "[Guest] ERROR: Init script exiting with status $status"
        if [ ! -f "$RESULTS_DIR/error.json" ]; then
            if [ -n "$TF_ERROR_FILE" ] && [ -f "$TF_ERROR_FILE" ]; then
                cp -f "$TF_ERROR_FILE" "$RESULTS_DIR/error.json" 2>/dev/null || true
            fi
            write_error_json "${ERROR_CONTEXT:-Init failure}"
        fi
        sync || true
    fi
}

trap 'on_exit' EXIT

prepare_workspace_dir() {
    # Expect WORKSPACE_PATH to point to a dir. Bind-mount as read-only into SRC_WORKDIR.
    mkdir -p "$SRC_WORKDIR"
    if [ ! -d "$WORKSPACE_PATH" ]; then
        echo "[Guest] ERROR: WORKSPACE_PATH is not a directory: $WORKSPACE_PATH"
        return 1
    fi
    if ! mount --bind "$WORKSPACE_PATH" "$SRC_WORKDIR"; then
        echo "[Guest] ERROR: Failed to bind-mount workspace directory: $WORKSPACE_PATH"
        return 1
    fi
    if ! mount -o remount,ro "$SRC_WORKDIR"; then
        echo "[Guest] ERROR: Failed to remount workspace directory read-only."
        return 1
    fi
    echo "[Guest] Workspace directory bound read-only from $WORKSPACE_PATH"
}

prepare_workspace() {
    if [ -b "$WORKSPACE_DEVICE" ]; then
        mkdir -p "$SRC_WORKDIR"
        if mount -t ext4 "$WORKSPACE_DEVICE" "$SRC_WORKDIR" -o ro; then
            echo "[Guest] Workspace mounted read-only from $WORKSPACE_DEVICE to $SRC_WORKDIR"
            return 0
        else
            echo "[Guest] ERROR: Failed to mount workspace volume from $WORKSPACE_DEVICE"
            return 1
        fi
    fi

    if [ -z "$WORKSPACE_PATH" ]; then
        echo "[Guest] ERROR: No workspace device and WORKSPACE_PATH not provided."
        return 1
    fi

    if [ -d "$WORKSPACE_PATH" ]; then
        prepare_workspace_dir || return 1
        return 0
    fi

    if [ -f "$WORKSPACE_PATH" ]; then
        echo "[Guest] ERROR: WORKSPACE_PATH must point to a directory. Zip archives must be extracted by the host."
        return 1
    fi

    echo "[Guest] ERROR: WORKSPACE_PATH must be a directory. Got: $WORKSPACE_PATH"
    return 1
}

ERROR_CONTEXT="Workspace preparation failed"
if ! prepare_workspace; then
    echo "[Guest] TEST_FAILED"
    exit 1
fi

ERROR_CONTEXT="Failed to mount workspace scratch volume"
if [ -b "$WORKSPACE_RW_DEVICE" ]; then
    mkdir -p "$SCRATCH_MNT"
    if mount -t ext4 "$WORKSPACE_RW_DEVICE" "$SCRATCH_MNT"; then
        echo "[Guest] Workspace scratch mounted at $SCRATCH_MNT"
    else
        echo "[Guest] ERROR: Failed to mount workspace scratch volume."
        echo "[Guest] TEST_FAILED"
        exit 1
    fi
else
    echo "[Guest] ERROR: No workspace scratch drive provided."
    echo "[Guest] TEST_FAILED"
    exit 1
fi

ERROR_CONTEXT="Failed to mount results volume"
if [ -b "$RESULTS_DEVICE" ]; then
    mkdir -p "$RESULTS_DIR"
    if mount -t ext4 "$RESULTS_DEVICE" "$RESULTS_DIR"; then
        echo "[Guest] Results volume mounted at $RESULTS_DIR"
    else
        echo "[Guest] ERROR: Failed to mount results volume."
        echo "[Guest] TEST_FAILED"
        exit 1
    fi
else
    echo "[Guest] ERROR: No results drive provided."
    echo "[Guest] TEST_FAILED"
    exit 1
fi

ERROR_CONTEXT="Failed to mount validator volume"
if [ -b "$VALIDATOR_DEVICE" ]; then
    mkdir -p "$VALIDATOR_DIR"
    if mount -t ext4 "$VALIDATOR_DEVICE" "$VALIDATOR_DIR" -o ro; then
        echo "[Guest] Validator bundle mounted at $VALIDATOR_DIR"
    else
        echo "[Guest] ERROR: Failed to mount validator volume."
        echo "[Guest] TEST_FAILED"
        exit 1
    fi
else
    echo "[Guest] ERROR: No validator drive provided."
    echo "[Guest] TEST_FAILED"
    exit 1
fi

ERROR_CONTEXT="Failed to mount overlay workspace"
UPPER_DIR="$SCRATCH_MNT/upper"
WORK_DIR="$SCRATCH_MNT/work"
mkdir -p "$UPPER_DIR" "$WORK_DIR" "$WORKDIR"
if ! mount -t overlay overlay -o "lowerdir=$SRC_WORKDIR,upperdir=$UPPER_DIR,workdir=$WORK_DIR" "$WORKDIR"; then
    echo "[Guest] ERROR: Failed to mount overlay workspace."
    echo "[Guest] TEST_FAILED"
    exit 1
fi
echo "[Guest] Workspace overlay mounted at $WORKDIR (lower=$SRC_WORKDIR, upper=$UPPER_DIR)"

cd "$WORKDIR"
export HOME="$WORKDIR"
export WORKDIR
export RESULTS_DIR
export VALIDATOR_DIR
export PYTHONPATH="$VALIDATOR_DIR:${PYTHONPATH:-}"
TF_RUNNER_USER="tf-runner"
VALIDATOR_USER="validator"
TF_RUNNER_GROUP="$(id -gn "$TF_RUNNER_USER" 2>/dev/null || echo "$TF_RUNNER_USER")"
VALIDATOR_GROUP="$(id -gn "$VALIDATOR_USER" 2>/dev/null || echo "$VALIDATOR_USER")"
TF_ERROR_FILE="$WORKDIR/tf-error.json"
run_as_user() {
    # Wrapper to execute commands as a specific user and return exit code without breaking errexit.
    user="$1"
    shift
    su -s /bin/sh "$user" -c "$*"
    return $?
}

ERROR_CONTEXT="Token file missing or unreadable"
if [ ! -f "$WORKDIR/gcp-access-token" ]; then
    echo "[Guest] ERROR: Token file missing at $WORKDIR/gcp-access-token"
    echo "[Guest] TEST_FAILED"
    exit 1
fi
if ! GOOGLE_OAUTH_ACCESS_TOKEN="$(cat "$WORKDIR/gcp-access-token" 2>/dev/null)"; then
    echo "[Guest] ERROR: Failed to read token file at $WORKDIR/gcp-access-token"
    echo "[Guest] TEST_FAILED"
    exit 1
fi
export GOOGLE_OAUTH_ACCESS_TOKEN
AUTH_ENV="GOOGLE_OAUTH_ACCESS_TOKEN=\"$GOOGLE_OAUTH_ACCESS_TOKEN\""
echo "[Guest] Auth mode: token"

# Speed up ADC by providing a credentials file path (metadata is blocked in this sandbox).
# The file is a token-only ADC stub generated by the host.
if [ -f "$WORKDIR/gcp-creds.json" ]; then
    export GOOGLE_APPLICATION_CREDENTIALS="$WORKDIR/gcp-creds.json"
fi
if [ -f "$WORKDIR/tf-provider-debug" ]; then
    TF_LOG_PROVIDER_DEBUG="1"
else
    TF_LOG_PROVIDER_DEBUG="0"
fi
export TF_LOG_PROVIDER_DEBUG

TASK_JSON_SRC="$SRC_WORKDIR/task.json"
TFSTATE_SRC="$SRC_WORKDIR/terraform.tfstate"
TFSTATE_COPY="$WORKDIR/terraform.tfstate"

# Ensure required artifacts exist; keep task.json read-only in SRC.
if [ ! -f "$TASK_JSON_SRC" ]; then
    echo "[Guest] ERROR: Task file missing at $TASK_JSON_SRC"
    echo "[Guest] TEST_FAILED"
    exit 1
fi
# Copy state into writable workspace when present so Terraform can reuse it.
if [ -f "$TFSTATE_SRC" ]; then
    cp -f "$TFSTATE_SRC" "$TFSTATE_COPY" || true
fi

chown -R "$TF_RUNNER_USER":"$TF_RUNNER_GROUP" "$WORKDIR"
chown -R "$VALIDATOR_USER":"$VALIDATOR_GROUP" "$RESULTS_DIR"
chmod 755 "$RESULTS_DIR"

if [ "$SKIP_TF" != "1" ]; then
    ERROR_CONTEXT="Terraform runner failed"
    echo "[Guest] Running Terraform as $TF_RUNNER_USER..."
	    if ! run_as_user "$TF_RUNNER_USER" "
	        cd \"$WORKDIR\" && \
	        env WORKDIR=\"$WORKDIR\" HOME=\"$WORKDIR\" RESULTS_DIR=\"$RESULTS_DIR\" PYTHONUNBUFFERED=\"1\" \
	        TF_ERROR_JSON=\"$TF_ERROR_FILE\" \
	        GCE_METADATA_HOST=\"127.0.0.1\" \
	        ${AUTH_ENV} \
	        TF_LOG_PROVIDER_DEBUG=\"$TF_LOG_PROVIDER_DEBUG\" \
	        python3 \"$WORKDIR/terraform_runner.py\"
	    "; then
	        echo "[Guest] ERROR: Terraform runner failed."
        if [ -f "$TF_ERROR_FILE" ]; then
            cp -f "$TF_ERROR_FILE" "$RESULTS_DIR/error.json" || true
        fi
        sync || true
        echo "[Guest] TEST_FAILED"
        exit 1
    fi
else
    echo "[Guest] Skipping terraform execution (SKIP_TF=1)."
fi

ERROR_CONTEXT="Validator run failed"

echo "[Guest] Running validator as $VALIDATOR_USER..."
if ! run_as_user "$VALIDATOR_USER" "
    cd \"$WORKDIR\" && \
    env PYTHONPATH=\"$VALIDATOR_DIR\" WORKDIR=\"$WORKDIR\" RESULTS_DIR=\"$RESULTS_DIR\" HOME=\"$WORKDIR\" \
    GCE_METADATA_HOST=\"127.0.0.1\" \
    ${AUTH_ENV} \
    VALIDATOR_DIR=\"$VALIDATOR_DIR\" \
    TF_LOG_PROVIDER_DEBUG=\"$TF_LOG_PROVIDER_DEBUG\" \
    TASK_JSON_PATH=\"$TASK_JSON_SRC\" \
    TFSTATE_PATH=\"$WORKDIR/terraform.tfstate\" \
    SKIP_TF=\"1\" \
    python3 \"$GUEST_RUNNER\"
"; then
    echo "[Guest] ERROR: Validator run failed."
    echo "[Guest] TEST_FAILED"
    exit 1
fi
if [ -f "$RESULTS_DIR/success.json" ]; then
    echo "[Guest] SUCCESS JSON:"
    cat "$RESULTS_DIR/success.json"
fi
if [ -f "$RESULTS_DIR/error.json" ]; then
    echo "[Guest] ERROR JSON:"
    cat "$RESULTS_DIR/error.json"
fi

# Flush results to disk and unmount cleanly so the host can mount read-only.
sync
umount "$RESULTS_DIR" || true

echo "[Guest] Init script completed successfully."
exit 0
