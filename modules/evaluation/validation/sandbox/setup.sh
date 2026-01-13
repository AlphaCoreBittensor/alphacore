#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

### VERSIONS ###
TF_VERSION="1.14.0"
PROVIDER_GOOGLE_VERSION="7.12.0"
PROVIDER_GOOGLE_BETA_VERSION="7.12.0"
PROVIDER_RANDOM_VERSION="3.7.2"

FIRECRACKER_VERSION="v1.13.1"
UBUNTU_CODENAME="jammy"   # 22.04 LTS
# Size of sandbox rootfs image (can override with ACORE_ROOTFS_SIZE_MB)
ROOTFS_SIZE_MB="${ACORE_ROOTFS_SIZE_MB:-2048}"
# Ubuntu mirror (override with ACORE_UBUNTU_MIRROR, keep trailing path off)
UBUNTU_MIRROR="${ACORE_UBUNTU_MIRROR:-http://us-central1.gce.archive.ubuntu.com/ubuntu}"

### PATHS ###
TF_BIN_DIR="/usr/local/bin"
TFRC_DIR="/etc/terraform.d"
TFRC_FILE="${TFRC_DIR}/terraform.rc"
PROVIDER_MIRROR_DIR="/opt/tf-providers"
TEST_PROJECT_DIR="/opt/acore-tf-test"

TERRAFORM_USER="terraformrunner"
TERRAFORM_HOME="/var/${TERRAFORM_USER}"

FIRECRACKER_DIR="/opt/firecracker"
SANDBOX_BUNDLE_DIR="/opt/acore-sandbox-bundle"
ROOTFS_BUILD_DIR="/opt/acore-rootfs-build"
ROOTFS_MNT_DIR="/mnt/acore-rootfs-mnt"
ACORE_KERNEL_IMG="${FIRECRACKER_DIR}/acore-sandbox-kernel-v1.bin"
ACORE_ROOTFS_IMG="${FIRECRACKER_DIR}/acore-sandbox-rootfs-v1.ext4"

ensure_rule() {
  # Append a rule only if it is not already present.
  local table="$1"; shift
  if ! iptables -t "${table}" -C "$@" 2>/dev/null; then
    iptables -t "${table}" -A "$@"
  fi
}

ensure_rule_first() {
  # Insert a rule at the head of a chain if it is not already present
  local table="$1"; shift
  if ! iptables -t "${table}" -C "$@" 2>/dev/null; then
    iptables -t "${table}" -I "$@"
  fi
}

ensure_chain_reset() {
  # Ensure a chain exists and is empty.
  local chain="$1"
  if iptables -nL "${chain}" >/dev/null 2>&1; then
    iptables -F "${chain}"
  else
    iptables -N "${chain}"
  fi
}

echo "=== AlphaCore Terraform + Firecracker validator setup ==="
echo "Terraform:           ${TF_VERSION}"
echo "google:              ${PROVIDER_GOOGLE_VERSION}"
echo "google-beta:         ${PROVIDER_GOOGLE_BETA_VERSION}"
echo "random:              ${PROVIDER_RANDOM_VERSION}"
echo "Firecracker:         ${FIRECRACKER_VERSION}"
echo "Ubuntu rootfs:       ${UBUNTU_CODENAME}, ${ROOTFS_SIZE_MB}MB"
echo "Ubuntu mirror:       ${UBUNTU_MIRROR}"
echo

########################################
# 1. Basic dependencies
########################################
echo "==> Installing dependencies..."
apt-get update -y
apt-get install -y \
  ca-certificates curl gnupg unzip tar acl rsync debootstrap e2fsprogs \
  git python3-full python3-pip screen socat \
  iproute2 iptables tinyproxy dnsmasq zip logrotate

# NOTE:
# Do not install Ubuntu's `npm` package here. On many systems `nodejs` is installed
# from NodeSource (which already bundles npm) and Ubuntu's `npm` can fail to resolve
# or conflict, breaking this setup.
#
# If you need `npm` (e.g. to install `pm2`), install Node.js from NodeSource and
# use the bundled npm.
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "==> Installing Node.js (includes npm)..."

  NODE_MAJOR="${NODE_MAJOR:-20}" # Override with e.g. NODE_MAJOR=24
  NODE_KEYRING="/usr/share/keyrings/nodesource.gpg"
  NODE_LIST="/etc/apt/sources.list.d/nodesource.list"

  if [[ ! -f "$NODE_LIST" ]]; then
    curl -fsSL "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key" \
      | gpg --dearmor -o "$NODE_KEYRING"
    echo "deb [signed-by=$NODE_KEYRING] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > "$NODE_LIST"
  fi

  apt-get update -y
  apt-get install -y nodejs

  echo "Node: $(node -v)"
  echo "npm:  $(npm -v)"
fi

########################################
# 1a. Install PM2 and configure startup
########################################
if ! command -v pm2 >/dev/null 2>&1; then
  echo "==> Installing PM2..."
  npm install -g pm2
  echo "PM2: $(pm2 -v)"
else
  echo "==> PM2 already installed, skipping."
fi

PM2_USER="${ACORE_PM2_USER:-${SUDO_USER:-}}"
if [[ -n "${PM2_USER:-}" ]]; then
  PM2_HOME="$(getent passwd "$PM2_USER" | cut -d: -f6 || true)"
  if [[ -n "${PM2_HOME:-}" && -d "$PM2_HOME" ]]; then
    echo "==> Configuring PM2 startup for user '${PM2_USER}'..."
    env PATH="$PATH" pm2 startup systemd -u "$PM2_USER" --hp "$PM2_HOME" || true
  else
    echo "==> PM2 startup skipped; home not found for user '${PM2_USER}'." >&2
  fi
else
  echo "==> PM2 startup skipped; set ACORE_PM2_USER to configure boot persistence." >&2
fi

########################################
# 1b. Install gcloud CLI
########################################
if command -v gcloud >/dev/null 2>&1; then
  echo "==> gcloud already installed, skipping."
else
  echo "==> Installing Google Cloud CLI (gcloud)..."
  install -m 0755 -d /usr/share/keyrings
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list
  apt-get update -y
  apt-get install -y google-cloud-cli
fi

########################################
# 2. Install Terraform
########################################
if ! command -v terraform >/dev/null 2>&1; then
  echo "==> Installing Terraform ${TF_VERSION}..."
  cd /tmp
  curl -fsSLo "terraform_${TF_VERSION}_linux_amd64.zip" \
    "https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_amd64.zip"
  unzip -o "terraform_${TF_VERSION}_linux_amd64.zip"
  mv terraform "${TF_BIN_DIR}/terraform"
  chmod +x "${TF_BIN_DIR}/terraform"
else
  echo "==> Terraform already installed, skipping."
fi

echo "Terraform version installed:"
terraform version
echo

########################################
# 3. Create terraformrunner user
########################################
if ! id -u "${TERRAFORM_USER}" >/dev/null 2>&1; then
  echo "==> Creating user '${TERRAFORM_USER}'..."
  useradd -m -d "${TERRAFORM_HOME}" -s /bin/bash "${TERRAFORM_USER}"
else
  echo "==> User '${TERRAFORM_USER}' already exists, skipping."
fi

mkdir -p "${TERRAFORM_HOME}"
chown -R "${TERRAFORM_USER}:${TERRAFORM_USER}" "${TERRAFORM_HOME}"

########################################
# 4. Create test project with pinned providers
########################################
echo "==> Creating test Terraform project at ${TEST_PROJECT_DIR}..."
mkdir -p "${TEST_PROJECT_DIR}"
cd "${TEST_PROJECT_DIR}"

cat > versions.tf <<EOF
terraform {
  required_version = "~> ${TF_VERSION}"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= ${PROVIDER_GOOGLE_VERSION}"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "= ${PROVIDER_GOOGLE_BETA_VERSION}"
    }
    random = {
      source  = "hashicorp/random"
      version = "= ${PROVIDER_RANDOM_VERSION}"
    }
  }
}

provider "google" {
  project = "dummy-project-id"
  region  = "us-central1"
}

provider "google-beta" {
  project = "dummy-project-id"
  region  = "us-central1"
}

provider "random" {}
EOF

cat > main.tf <<'EOF'
resource "random_id" "example" {
  byte_length = 4
}
EOF

chown -R "${TERRAFORM_USER}:${TERRAFORM_USER}" "${TEST_PROJECT_DIR}"

########################################
# 5. Build provider mirror (downloads providers)
########################################
echo "==> Building curated provider mirror at ${PROVIDER_MIRROR_DIR}..."

mkdir -p "${PROVIDER_MIRROR_DIR}"
chown -R "${TERRAFORM_USER}:${TERRAFORM_USER}" "${PROVIDER_MIRROR_DIR}"

sudo -u "${TERRAFORM_USER}" bash -c "
  set -euo pipefail
  cd '${TEST_PROJECT_DIR}'

  echo '==> terraform init (downloads providers from registry)...'
  terraform init -input=false

  echo '==> Mirroring providers to ${PROVIDER_MIRROR_DIR}...'
  terraform providers mirror \
    -platform=linux_amd64 \
    '${PROVIDER_MIRROR_DIR}'
"

echo "Mirror contents:"
find "${PROVIDER_MIRROR_DIR}" -maxdepth 6 -type f -name 'terraform-provider-*'
echo

########################################
# 6. Terraform CLI config (forces filesystem mirror)
########################################
echo "==> Creating Terraform CLI config at ${TFRC_FILE}..."

mkdir -p "${TFRC_DIR}"

cat > "${TFRC_FILE}" <<EOF
provider_installation {
  filesystem_mirror {
    path    = "${PROVIDER_MIRROR_DIR}"
    include = [
      "hashicorp/google",
      "hashicorp/google-beta",
      "hashicorp/random",
    ]
  }

  direct {
    exclude = ["registry.terraform.io/*/*"]
  }
}
EOF

chmod 644 "${TFRC_FILE}"

########################################
# 7. Wrapper that always uses terraform.rc
########################################
WRAPPER="/usr/local/bin/acore-tf"
echo "==> Creating wrapper ${WRAPPER}..."

cat > "${WRAPPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

export TF_CLI_CONFIG_FILE="${TFRC_FILE}"
export HOME="${TERRAFORM_HOME}"

unset TF_PLUGIN_CACHE_DIR
unset TF_CLI_ARGS
unset TF_CLI_CONFIG_FILE_ORIG

exec terraform "\$@"
EOF

chmod +x "${WRAPPER}"

########################################
# 8. Create sandbox bundle directory for microVM usage
########################################
echo "==> Creating sandbox bundle at ${SANDBOX_BUNDLE_DIR}..."
mkdir -p "${SANDBOX_BUNDLE_DIR}"/{bin,config,providers}

cp -f "${TF_BIN_DIR}/terraform" "${SANDBOX_BUNDLE_DIR}/bin/terraform"
cp -f "${TFRC_FILE}" "${SANDBOX_BUNDLE_DIR}/config/terraform.rc"
rsync -a "${PROVIDER_MIRROR_DIR}/" "${SANDBOX_BUNDLE_DIR}/providers/"

chown -R "${TERRAFORM_USER}:${TERRAFORM_USER}" "${SANDBOX_BUNDLE_DIR}"

########################################
# 9. Install Firecracker binary
########################################
echo "==> Installing Firecracker ${FIRECRACKER_VERSION}..."
ARCH="$(uname -m)"
TMPDIR="$(mktemp -d)"
cd "${TMPDIR}"

curl -fsSL -o firecracker.tgz \
  "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-${ARCH}.tgz"

tar -xzf firecracker.tgz

mkdir -p "${FIRECRACKER_DIR}"

FIRECRACKER_BIN="$(find . -maxdepth 3 -type f -name "firecracker*${ARCH}" | head -n1)"
JAILER_BIN="$(find . -maxdepth 3 -type f -name "jailer*${ARCH}" | head -n1)"

if [ -n "${FIRECRACKER_BIN}" ] && [ -f "${FIRECRACKER_BIN}" ]; then
  mv "${FIRECRACKER_BIN}" "${FIRECRACKER_DIR}/firecracker"
  chmod +x "${FIRECRACKER_DIR}/firecracker"
  ln -sf "${FIRECRACKER_DIR}/firecracker" /usr/local/bin/firecracker
else
  echo "ERROR: Firecracker binary not found in archive." >&2
  exit 1
fi

if [ -n "${JAILER_BIN}" ] && [ -f "${JAILER_BIN}" ]; then
  mv "${JAILER_BIN}" "${FIRECRACKER_DIR}/jailer"
  chmod +x "${FIRECRACKER_DIR}/jailer"
  ln -sf "${FIRECRACKER_DIR}/jailer" /usr/local/bin/jailer
fi

cd /
rm -rf "${TMPDIR}"

echo "Firecracker version:"
firecracker --version
echo

# Reinforce symlinks in case previous run failed
if [ -x "${FIRECRACKER_DIR}/firecracker" ]; then
  ln -sf "${FIRECRACKER_DIR}/firecracker" /usr/local/bin/firecracker
fi
if [ -x "${FIRECRACKER_DIR}/jailer" ]; then
  ln -sf "${FIRECRACKER_DIR}/jailer" /usr/local/bin/jailer
fi

# Ensure /opt/firecracker is on PATH for login shells
echo "==> Adding ${FIRECRACKER_DIR} to PATH via /etc/profile.d/firecracker.sh..."
cat > /etc/profile.d/firecracker.sh <<EOF
# Added by AlphaCore validator setup
export PATH="${FIRECRACKER_DIR}:\$PATH"
EOF

########################################
# 10. Download Firecracker CI vmlinux 5.10 kernel
########################################
echo "==> Downloading Firecracker CI vmlinux 5.10 into ${FIRECRACKER_DIR}..."
mkdir -p "${FIRECRACKER_DIR}"
cd "${FIRECRACKER_DIR}"

ARCH="$(uname -m)"

latest=$(curl -fsSL "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/v1.11/${ARCH}/vmlinux-5.10&list-type=2" \
  | grep -oP "(?<=<Key>)(firecracker-ci/v1.11/${ARCH}/vmlinux-5\.10\.[0-9]{1,3})(?=</Key>)" \
  | sort \
  | tail -n1)

if [ -z "${latest}" ]; then
  echo "ERROR: Could not determine latest vmlinux-5.10 image from S3" >&2
  exit 1
fi

echo "Latest kernel key: ${latest}"

curl -fsSL -o "${ACORE_KERNEL_IMG}" "https://s3.amazonaws.com/spec.ccfc.min/${latest}"

echo "Using Firecracker kernel:"
ls -lh "${ACORE_KERNEL_IMG}"

########################################
# 11. Build custom Ubuntu rootfs and bake bundle
########################################
echo "==> Building custom Ubuntu ${UBUNTU_CODENAME} rootfs at ${ACORE_ROOTFS_IMG}..."

# Clean any previous attempts
rm -rf "${ROOTFS_BUILD_DIR}" "${ACORE_ROOTFS_IMG}" "${ROOTFS_MNT_DIR}"
mkdir -p "${ROOTFS_BUILD_DIR}" "${ROOTFS_MNT_DIR}"

# Create minimal Ubuntu rootfs
debootstrap --variant=minbase --arch=amd64 "${UBUNTU_CODENAME}" "${ROOTFS_BUILD_DIR}" "${UBUNTU_MIRROR}"

# Create sandbox users for Terraform runner and validator
echo "==> Creating sandbox users..."
chroot "${ROOTFS_BUILD_DIR}" /bin/bash -c "
  useradd -u 2000 -m -s /bin/bash tf-runner
  useradd -u 2001 -m -s /bin/bash validator
"

# Basic config inside rootfs
cat > "${ROOTFS_BUILD_DIR}/etc/hostname" <<EOF
acore-sandbox
EOF

cat > "${ROOTFS_BUILD_DIR}/etc/hosts" <<'EOF'
127.0.0.1   localhost
127.0.1.1   acore-sandbox
EOF

# Minimal resolv.conf
mkdir -p "${ROOTFS_BUILD_DIR}/etc"
cat > "${ROOTFS_BUILD_DIR}/etc/resolv.conf" <<'EOF'
nameserver 8.8.8.8
EOF

# Ensure apt sources include main + updates + security.
cat > "${ROOTFS_BUILD_DIR}/etc/apt/sources.list" <<EOF
deb ${UBUNTU_MIRROR} ${UBUNTU_CODENAME} main restricted universe multiverse
deb ${UBUNTU_MIRROR} ${UBUNTU_CODENAME}-updates main restricted universe multiverse
deb http://security.ubuntu.com/ubuntu ${UBUNTU_CODENAME}-security main restricted universe multiverse
EOF

# Install a few useful tools inside chroot
chroot "${ROOTFS_BUILD_DIR}" /bin/bash -c "
set -e
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates iproute2 iptables curl bash iputils-ping python3 python3-venv python3-pip busybox
if command -v busybox >/dev/null 2>&1; then
  # Ensure a stable udhcpc path in the guest (busybox applet).
  mkdir -p /sbin
  ln -sf \"\$(command -v busybox)\" /sbin/udhcpc || true
fi
# We expect udhcpc to be available for DHCP in the guest.
if [ ! -x /sbin/udhcpc ]; then
  echo \"ERROR: /sbin/udhcpc missing in guest rootfs.\" >&2
  exit 1
fi
apt-get clean
rm -rf /var/lib/apt/lists/*
"

# Pre-seed Python deps inside rootfs if requirements.txt is present
if [ -f "${REPO_ROOT}/requirements.txt" ]; then
  cp "${REPO_ROOT}/requirements.txt" "${ROOTFS_BUILD_DIR}/tmp/requirements.txt"
  chroot "${ROOTFS_BUILD_DIR}" /bin/bash -c "
  set -e
  if [ -f /tmp/requirements.txt ]; then
    python3 -m pip install --no-cache-dir -r /tmp/requirements.txt
    rm -f /tmp/requirements.txt
  fi
  "
fi

# Create directory to hold the terraform bundle inside guest
mkdir -p "${ROOTFS_BUILD_DIR}/opt/acore-sandbox-bundle"
rsync -a "${SANDBOX_BUNDLE_DIR}/" "${ROOTFS_BUILD_DIR}/opt/acore-sandbox-bundle/"

# Write a sandbox-specific terraform.rc inside the bundle that points to the in-guest mirror.
cat > "${ROOTFS_BUILD_DIR}/opt/acore-sandbox-bundle/config/terraform.rc" <<EOF
provider_installation {
  filesystem_mirror {
    path    = "/opt/acore-sandbox-bundle/providers"
    include = ["hashicorp/*"]
  }

  direct {
    exclude = ["registry.terraform.io/*/*"]
  }
}
EOF

# Expose terraform on the standard PATH inside the rootfs.
mkdir -p "${ROOTFS_BUILD_DIR}/usr/local/bin"
ln -sf /opt/acore-sandbox-bundle/bin/terraform "${ROOTFS_BUILD_DIR}/usr/local/bin/terraform"

# Create ext4 image
dd if=/dev/zero of="${ACORE_ROOTFS_IMG}" bs=1M count="${ROOTFS_SIZE_MB}"
mkfs.ext4 -F "${ACORE_ROOTFS_IMG}"

# Mount and copy rootfs into image
mount -o loop "${ACORE_ROOTFS_IMG}" "${ROOTFS_MNT_DIR}"
rsync -a "${ROOTFS_BUILD_DIR}/" "${ROOTFS_MNT_DIR}/"
sync
umount "${ROOTFS_MNT_DIR}"

########################################
# 12. Networking: Bridge + DHCP + Firewall (Whitelist)
########################################
echo "==> Setting up microVM bridge (acore-br0), DHCP, and Firewall..."

# --- FIX: ENABLE KERNEL FORWARDING ---
# Required for packets to pass from acore-br0 -> ${EGRESS_IFACE}
echo "==> Enabling Kernel IP Forwarding..."
sysctl -w net.ipv4.ip_forward=1 >/dev/null
# Persist across reboots.
cat > /etc/sysctl.d/99-alphacore-sandbox.conf <<'EOF'
net.ipv4.ip_forward=1
EOF
# -------------------------------------

BR_NAME="acore-br0"
BR_CIDR="172.16.0.1/24"
SUBNET_CIDR="172.16.0.0/24"
TAP_PREFIX="acore-tap"
TAP_POOL_SIZE="${ACORE_TAP_POOL_SIZE:-32}"
DHCP_START="${ACORE_DHCP_START:-172.16.0.100}"
DHCP_END="${ACORE_DHCP_END:-172.16.0.199}"
DHCP_LEASE="12h"
PROXY_PORT="8888"
resolve_uid_gid() {
  local user="$1"
  local uid gid
  uid="$(id -u "$user" 2>/dev/null || true)"
  gid="$(id -g "$user" 2>/dev/null || true)"
  if [[ -n "${uid:-}" && -n "${gid:-}" ]]; then
    echo "${uid}:${gid}"
    return 0
  fi
  return 1
}

if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
  TAP_OWNER_UID="${SUDO_UID}"
  TAP_OWNER_GID="${SUDO_GID}"
elif resolve_uid_gid ubuntu >/dev/null; then
  TAP_OWNER_UID="${TAP_OWNER_UID:-${SUDO_UID:-$(resolve_uid_gid ubuntu | cut -d: -f1)}}"
  TAP_OWNER_GID="${TAP_OWNER_GID:-${SUDO_GID:-$(resolve_uid_gid ubuntu | cut -d: -f2)}}"
elif resolve_uid_gid "${TERRAFORM_USER}" >/dev/null; then
  TAP_OWNER_UID="${TAP_OWNER_UID:-${SUDO_UID:-$(resolve_uid_gid "${TERRAFORM_USER}" | cut -d: -f1)}}"
  TAP_OWNER_GID="${TAP_OWNER_GID:-${SUDO_GID:-$(resolve_uid_gid "${TERRAFORM_USER}" | cut -d: -f2)}}"
else
  TAP_OWNER_UID="${TAP_OWNER_UID:-$(id -u)}"
  TAP_OWNER_GID="${TAP_OWNER_GID:-$(id -g)}"
fi

# Detect default egress interface (used by proxy, not by TAP)
EGRESS_IFACE="$(ip route get 8.8.8.8 2>/dev/null \
  | awk '/dev/ {for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')"
[ -z "${EGRESS_IFACE}" ] && EGRESS_IFACE="ens4"

echo "[acore-net] Using egress interface: ${EGRESS_IFACE}"
echo "[acore-net] Setting TAP owner to UID:GID ${TAP_OWNER_UID}:${TAP_OWNER_GID}"

echo "[acore-net] Ensuring bridge ${BR_NAME} (${BR_CIDR})..."
if ! ip link show "${BR_NAME}" >/dev/null 2>&1; then
  ip link add name "${BR_NAME}" type bridge
fi
ip addr flush dev "${BR_NAME}" || true
ip addr add "${BR_CIDR}" dev "${BR_NAME}" 2>/dev/null || true
ip link set "${BR_NAME}" up

# Avoid ARP flux: ensure the host only replies to ARP on ${BR_NAME} for addresses configured on ${BR_NAME}.
sysctl -w "net.ipv4.conf.${BR_NAME}.arp_ignore=1" >/dev/null 2>&1 || true
sysctl -w "net.ipv4.conf.${BR_NAME}.arp_announce=2" >/dev/null 2>&1 || true

# Ensure the bridge is not accidentally connected to any non-sandbox interfaces.
# If a physical NIC is attached, ARP/DHCP can leak to the host network and cause DHCPDECLINE.
echo "[acore-net] Ensuring ${BR_NAME} only has sandbox TAP ports..."
while read -r iface; do
  [ -z "${iface}" ] && continue
  if [[ "${iface}" != ${TAP_PREFIX}* ]]; then
    echo "[acore-net] Detaching non-sandbox interface from ${BR_NAME}: ${iface}"
    ip link set dev "${iface}" nomaster 2>/dev/null || true
  fi
done < <(ip -o link show master "${BR_NAME}" 2>/dev/null | awk -F': ' '{print $2}' | awk '{print $1}')

echo "[acore-net] Ensuring TAP pool (${TAP_POOL_SIZE} devices) attached to ${BR_NAME}..."
for i in $(seq 0 $((TAP_POOL_SIZE - 1))); do
  TAP_NAME="${TAP_PREFIX}${i}"
  if ! ip link show "${TAP_NAME}" >/dev/null 2>&1; then
    ip tuntap add dev "${TAP_NAME}" mode tap user "${TAP_OWNER_UID}" group "${TAP_OWNER_GID}"
  fi
  ip addr flush dev "${TAP_NAME}" 2>/dev/null || true
  ip link set "${TAP_NAME}" master "${BR_NAME}" 2>/dev/null || true
  ip link set "${TAP_NAME}" up 2>/dev/null || true
  bridge link set dev "${TAP_NAME}" isolated on 2>/dev/null || true
  sysctl -w "net.ipv6.conf.${TAP_NAME}.disable_ipv6=1" >/dev/null 2>&1 || true
done

sysctl -w "net.ipv6.conf.${BR_NAME}.disable_ipv6=1" >/dev/null 2>&1 || true

echo "==> Applying Network Security Rules (Whitelist Strategy)..."

# Configure dnsmasq for bridge-scoped DNS whitelist + DHCP
cat > /etc/dnsmasq.d/acore-sandbox.conf <<EOF
interface=${BR_NAME}
bind-interfaces
bogus-priv
no-resolv
no-poll
dhcp-authoritative
dhcp-range=${DHCP_START},${DHCP_END},${DHCP_LEASE}
dhcp-option=option:router,172.16.0.1
dhcp-option=option:dns-server,172.16.0.1
server=/googleapis.com/8.8.8.8
server=/gcr.io/8.8.8.8
server=/pkg.dev/8.8.8.8
address=/#/
EOF
if ! dnsmasq --test -C /etc/dnsmasq.d/acore-sandbox.conf >/dev/null 2>&1; then
  echo "ERROR: dnsmasq config test failed for /etc/dnsmasq.d/acore-sandbox.conf" >&2
  dnsmasq --test -C /etc/dnsmasq.d/acore-sandbox.conf || true
  exit 1
fi
systemctl restart dnsmasq || {
  echo "ERROR: dnsmasq failed to restart. Inspect with: systemctl status dnsmasq && journalctl -xeu dnsmasq" >&2
  exit 1
}

# Clean and rebuild chains tagged for the sandbox bridge (idempotent)
iptables -D FORWARD -i "${BR_NAME}" -j ACORE_BR 2>/dev/null || true
iptables -D INPUT -i "${BR_NAME}" -j ACORE_INPUT 2>/dev/null || true
ensure_chain_reset ACORE_BR
ensure_chain_reset ACORE_INPUT

# Allow established traffic back
iptables -A ACORE_BR -m state --state ESTABLISHED,RELATED -j ACCEPT
# Allow DHCP/DNS/proxy to host services even if bridged traffic is inspected via FORWARD.
iptables -A ACORE_BR -d 172.16.0.1 -p udp --dport 67 -j ACCEPT
iptables -A ACORE_BR -d 172.16.0.1 -p udp --dport 53 -j ACCEPT
iptables -A ACORE_BR -d 172.16.0.1 -p tcp --dport 53 -j ACCEPT
iptables -A ACORE_BR -d 172.16.0.1 -p tcp --dport "${PROXY_PORT}" -j ACCEPT
# Immediately reject metadata server attempts to avoid long TCP hangs (needs TCP proto for reset)
iptables -A ACORE_BR -p tcp -d 169.254.169.254 -j REJECT --reject-with tcp-reset
# Drop everything else from the sandbox subnet (deny direct egress)
iptables -A ACORE_BR -j DROP

# Attach chain to FORWARD for this bridge as first rule
ensure_rule_first filter FORWARD -i "${BR_NAME}" -j ACORE_BR

# INPUT rules: allow DHCP/DNS/proxy on the bridge; drop everything else
iptables -A ACORE_INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A ACORE_INPUT -p udp --dport 67 -j ACCEPT
iptables -A ACORE_INPUT -s "${SUBNET_CIDR}" -d 172.16.0.1 -p udp --dport 53 -j ACCEPT
iptables -A ACORE_INPUT -s "${SUBNET_CIDR}" -d 172.16.0.1 -p tcp --dport 53 -j ACCEPT
iptables -A ACORE_INPUT -s "${SUBNET_CIDR}" -d 172.16.0.1 -p tcp --dport "${PROXY_PORT}" -j ACCEPT
iptables -A ACORE_INPUT -d 169.254.169.254 -j DROP
iptables -A ACORE_INPUT -j DROP
ensure_rule_first filter INPUT -i "${BR_NAME}" -j ACORE_INPUT

# NAT: Masquerade only sandbox subnet traffic (defensive; proxy runs on host)
ensure_rule nat POSTROUTING -s "${SUBNET_CIDR}" -o "${EGRESS_IFACE}" -j MASQUERADE

echo "   [Security] Firewall configured: Deny-All by default."
echo "   [Security] Holes opened: DHCP(67/udp), DNS(172.16.0.1:53), Proxy(172.16.0.1:${PROXY_PORT})."


########################################
# 13. Configure tinyproxy as Strict Egress Guard
########################################
echo "==> Configuring tinyproxy with Google-Only Whitelist..."

TINYCONF="/etc/tinyproxy/tinyproxy.conf"
# Changed from 'gcp-only.filter' to generic 'filter'
TINYFILTER="/etc/tinyproxy/filter"

cat > "${TINYCONF}" <<EOF
User nobody
Group nogroup
Port ${PROXY_PORT}
Listen 172.16.0.1
Bind 0.0.0.0

Timeout 600
LogLevel Info
MaxClients 100
StartServers 5
MinSpareServers 5
MaxSpareServers 20

PidFile "/run/tinyproxy/tinyproxy.pid"
LogFile "/var/log/tinyproxy/tinyproxy.log"

Allow 172.16.0.0/24

# STRICT FILTERING CONFIGURATION
Filter "${TINYFILTER}"
FilterURLs On
FilterDefaultDeny Yes
FilterExtended On
EOF

# Whitelist: Google Cloud APIs + Auth
cat > "${TINYFILTER}" <<'EOF'
^([^.]+\.)*googleapis\.com(:[0-9]+)?$
EOF

mkdir -p /run/tinyproxy
chown nobody:nogroup /run/tinyproxy

systemctl enable tinyproxy >/dev/null 2>&1 || true
systemctl restart tinyproxy || {
  echo "WARNING: tinyproxy failed to restart; check /var/log/tinyproxy/tinyproxy.log" >&2
}

# Disable IPv6 on bridge to avoid bypassing IPv4 firewall
sysctl -w "net.ipv6.conf.${BR_NAME}.disable_ipv6=1" >/dev/null 2>&1 || true

########################################
# 13c. Allow sandbox runner sudo (for PM2/boot-time)
########################################
# The validation API launches the sandbox runner via `sudo -n ...` (non-interactive).
# To make this work reliably under process managers like PM2 (no tty / no password prompt),
# we install a narrow sudoers rule for the TAP-owner user.
echo "==> Installing sudoers rule for sandbox runner..."
TAP_OWNER_USER="$(id -nu "${TAP_OWNER_UID}" 2>/dev/null || true)"
if [ -n "${TAP_OWNER_USER}" ]; then
cat > /etc/sudoers.d/alphacore-sandbox-runner <<'EOF'
# Allow the sandbox runner to execute via sudo without a password (needed for non-interactive supervisors).
# The worker pool invokes: sudo -n /usr/bin/python3 .../sandbox.py ...
#
# NOTE: the sandbox runner needs root for mounts/jailer setup, but it refuses to start Firecracker as uid=0.
EOF
  echo "${TAP_OWNER_USER} ALL=(root) NOPASSWD: /usr/bin/python3 *modules/evaluation/validation/sandbox/sandbox.py *" >> /etc/sudoers.d/alphacore-sandbox-runner
  chmod 440 /etc/sudoers.d/alphacore-sandbox-runner
else
  echo "WARNING: Could not resolve TAP owner UID ${TAP_OWNER_UID} to a username; skipping sudoers install." >&2
fi

########################################
# 13b. Install systemd unit for reboot persistence
########################################
echo "==> Installing systemd service for sandbox networking/proxy..."

install -m 0755 "${SCRIPT_DIR}/alphacore-sandbox-net.sh" /usr/local/sbin/alphacore-sandbox-net
install -m 0644 "${SCRIPT_DIR}/alphacore-sandbox-net.service" /etc/systemd/system/alphacore-sandbox-net.service

# Create or update the env file (keep ownership aligned with the sudo caller).
if [ ! -f /etc/default/alphacore-sandbox-net ]; then
  cat > /etc/default/alphacore-sandbox-net <<EOF
# Optional overrides for /usr/local/sbin/alphacore-sandbox-net
#
# Keep TAP ownership stable so non-root sandbox runners can open the TAP devices.
TAP_OWNER_UID=${TAP_OWNER_UID}
TAP_OWNER_GID=${TAP_OWNER_GID}
ACORE_TAP_POOL_SIZE=${TAP_POOL_SIZE}
EOF
else
  tmp="$(mktemp)"
  awk \
    -v uid="${TAP_OWNER_UID}" \
    -v gid="${TAP_OWNER_GID}" \
    -v pool="${TAP_POOL_SIZE}" \
    '
      BEGIN {seen_uid=0; seen_gid=0; seen_pool=0}
      /^TAP_OWNER_UID=/ {print "TAP_OWNER_UID=" uid; seen_uid=1; next}
      /^TAP_OWNER_GID=/ {print "TAP_OWNER_GID=" gid; seen_gid=1; next}
      /^ACORE_TAP_POOL_SIZE=/ {print "ACORE_TAP_POOL_SIZE=" pool; seen_pool=1; next}
      {print}
      END {
        if (!seen_uid) print "TAP_OWNER_UID=" uid
        if (!seen_gid) print "TAP_OWNER_GID=" gid
        if (!seen_pool) print "ACORE_TAP_POOL_SIZE=" pool
      }
    ' /etc/default/alphacore-sandbox-net > "$tmp"
  mv "$tmp" /etc/default/alphacore-sandbox-net
fi

systemctl daemon-reload
systemctl enable --now alphacore-sandbox-net.service >/dev/null 2>&1 || true

########################################
# 13d. Configure log retention (logrotate + tmpfiles)
########################################
echo "==> Configuring logrotate + tmpfiles cleanup..."

# Clean up any prior custom cleanup units (if present).
systemctl disable --now alphacore-log-cleanup.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/alphacore-log-cleanup.timer \
  /etc/systemd/system/alphacore-log-cleanup.service \
  /usr/local/sbin/alphacore-log-cleanup \
  /etc/default/alphacore-log-cleanup

install -m 0644 /dev/null /etc/logrotate.d/alphacore-logs
sed "s|@REPO_ROOT@|${REPO_ROOT}|g" \
  "${SCRIPT_DIR}/alphacore-logs.logrotate" > /etc/logrotate.d/alphacore-logs

install -m 0644 /dev/null /etc/tmpfiles.d/alphacore-logs.conf
sed "s|@REPO_ROOT@|${REPO_ROOT}|g" \
  "${SCRIPT_DIR}/alphacore-logs.tmpfiles" > /etc/tmpfiles.d/alphacore-logs.conf

systemctl daemon-reload
systemctl enable --now systemd-tmpfiles-clean.timer >/dev/null 2>&1 || true

########################################
# 14. Grant /dev/kvm to terraformrunner
########################################
echo "==> Ensuring KVM modules are loaded (best effort)..."
modprobe kvm 2>/dev/null || echo "WARNING: failed to load kvm module (continuing)" >&2
modprobe kvm_intel 2>/dev/null || echo "WARNING: failed to load kvm_intel module (continuing)" >&2

echo "==> Checking KVM availability (best effort)..."
if grep -Eq '(vmx|svm)' /proc/cpuinfo; then
  echo "KVM: CPU virtualization flags detected."
else
  echo "WARNING: CPU virtualization flags not detected; KVM may be unavailable." >&2
fi
if [ -d /sys/module/kvm ]; then
  echo "KVM: kernel module appears loaded."
else
  echo "WARNING: KVM kernel module not loaded." >&2
fi
if [ -e /dev/kvm ]; then
  echo "KVM: /dev/kvm present."
else
  echo "WARNING: /dev/kvm not found. Ensure virtualization is enabled." >&2
fi

echo "==> Granting ${TERRAFORM_USER} access to /dev/kvm (if present)..."
if [ -e /dev/kvm ]; then
  setfacl -m u:${TERRAFORM_USER}:rw /dev/kvm
fi

########################################
# 15. Final info
########################################
cat <<EOF2

===========================================================
Setup complete.

Terraform:
  Binary:      /usr/local/bin/terraform
  Wrapper:     /usr/local/bin/acore-tf
  Test proj:   /opt/acore-tf-test
  Mirror:      /opt/tf-providers
  CLI config:  /etc/terraform.d/terraform.rc

Sandbox bundle (baked into rootfs under /opt/acore-sandbox-bundle):
  /opt/acore-sandbox-bundle/
    bin/terraform
    config/terraform.rc
    providers/...

Firecracker:
  Binary:      /usr/local/bin/firecracker
  (maybe)      /usr/local/bin/jailer
  Kernel:      /opt/firecracker/acore-sandbox-kernel-v1.bin
  Rootfs:      /opt/firecracker/acore-sandbox-rootfs-v1.ext4

Networking (STRICT MODE):
  Bridge:      acore-br0 (host IP: 172.16.0.1/24)
  TAP pool:    acore-tap[0..$((TAP_POOL_SIZE - 1))] (isolated ports)
  Firewall:    All FORWARD traffic DROPPED by default.
  Holes:       DHCP (67/udp), DNS (172.16.0.1:53), Proxy (172.16.0.1:${PROXY_PORT}).
  Proxy:       tinyproxy on 172.16.0.1:8888
  Proxy Allow: googleapis.com, google.com, gcr.io, pkg.dev
  Metadata:    BLOCKED (169.254.169.254)

Quick Terraform validation on host:
  sudo -iu terraformrunner
  cd /opt/acore-tf-test
  acore-tf init -input=false
  acore-tf plan -input=false

Provisioning inside Firecracker microVM:
  - Kernel:  /opt/firecracker/acore-sandbox-kernel-v1.bin
  - Rootfs:  /opt/firecracker/acore-sandbox-rootfs-v1.ext4
  - Terraform: /opt/acore-sandbox-bundle/bin/terraform
  - Guest should:
      * Use DHCP on eth0 (172.16.0.0/24)
      * Export http_proxy=http://172.16.0.1:8888
===========================================================
EOF2
