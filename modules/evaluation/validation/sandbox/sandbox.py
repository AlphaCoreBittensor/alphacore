#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import threading
import subprocess
import sys
import tempfile
import time
import zipfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

RUNNER_SCRIPT = Path(__file__).with_name("terraform_runner.py")
INIT_SCRIPT = Path(__file__).with_name("init-sandbox.sh")
NET_CHECKS_SCRIPT = Path(__file__).with_name("net_checks.py")
CREDS_DEST = "gcp-creds.json"
DEFAULT_VALIDATE_SCRIPT = Path(__file__).with_name("validate.py")
DEFAULT_TAP_PREFIX = "acore-tap"


def mint_access_token_from_service_account(creds_path: Path) -> str:
    """Mint a short-lived OAuth access token from a service-account JSON key (host-side)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except Exception as exc:  # pragma: no cover
        raise SystemExit(
            "google-auth is required to mint an access token from --creds-file; "
            "install it in your host venv or provide GOOGLE_OAUTH_ACCESS_TOKEN."
        ) from exc

    credentials = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise SystemExit("Failed to mint access token from service account credentials.")
    return str(credentials.token)


def resolve_firecracker_ids(uid: Optional[int] = None, gid: Optional[int] = None) -> tuple[int, int]:
    """Resolve a non-root uid/gid for Firecracker, honoring sudo callers."""
    resolved_uid = os.geteuid() if uid is None else uid
    resolved_gid = os.getegid() if gid is None else gid

    if resolved_uid == 0:
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid and sudo_uid != "0" and sudo_gid != "0":
            resolved_uid, resolved_gid = int(sudo_uid), int(sudo_gid)
        else:
            raise SystemExit(
                "Refusing to start Firecracker as root (uid=0). Run as a non-root user "
                "or provide SUDO_UID/SUDO_GID when invoking via sudo."
            )

    if resolved_gid == 0:
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_gid and sudo_gid != "0":
            resolved_gid = int(sudo_gid)
        else:
            raise SystemExit("Refusing to start Firecracker with root gid (gid=0).")

    return resolved_uid, resolved_gid


def _chown(path: Path, uid: int, gid: int) -> None:
    """Best-effort chown; warn instead of crashing on permission errors."""
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        sys.stderr.write(f"Warning: could not chown {path} to {uid}:{gid}; continuing.\n")


@dataclass
class SandboxConfig:
    """Host-side configuration for the Firecracker sandbox test."""

    id: str
    chroot_base: Path = Path("/srv/jailer")
    fc_bin: Path = Path("/opt/firecracker/firecracker")
    jailer_bin: Path = Path("/opt/firecracker/jailer")
    kernel: Path = Path("/opt/firecracker/acore-sandbox-kernel-v1.bin")
    rootfs: Path = Path("/opt/firecracker/acore-sandbox-rootfs-v1.ext4")
    acore_tap: str = "acore-tap0"
    mem_mb: int = 512
    vcpus: int = 1
    workspace_rw_size_mb: int = int(os.environ.get("ACORE_WORKSPACE_RW_MB", "2048"))
    log_file: Optional[Path] = None
    jailer_uid: Optional[int] = None
    jailer_gid: Optional[int] = None

    def __post_init__(self) -> None:
        if self.log_file is None:
            self.log_file = Path(f"./firecracker-{self.id}.log")
        resolved_uid, resolved_gid = resolve_firecracker_ids(self.jailer_uid, self.jailer_gid)
        self.jailer_uid, self.jailer_gid = resolved_uid, resolved_gid

    @property
    def chroot(self) -> Path:
        return self.chroot_base / "firecracker" / self.id / "root"

    @property
    def api_socket(self) -> Path:
        return self.chroot / "run" / "fc.sock"

    @property
    def rootfs_copy(self) -> Path:
        return self.chroot / "rootfs.ext4"

    @property
    def workspace_image(self) -> Path:
        return self.chroot / "workspace.ext4"

    @property
    def workspace_rw_image(self) -> Path:
        return self.chroot / "workspace-rw.ext4"

    @property
    def results_image(self) -> Path:
        return self.chroot / "results.ext4"

    @property
    def validator_image(self) -> Path:
        return self.chroot / "validator.ext4"


@dataclass
class ExtractionResult:
    """Result extracted from the results image."""

    success: bool
    score: Optional[float] = None
    error: Optional[str] = None
    success_json: Optional[dict] = None
    error_json: Optional[dict] = None


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command and raise on failure, emitting stderr to help debugging."""
    result = subprocess.run(cmd, text=True, capture_output=True, **kwargs)
    if result.returncode != 0:
        sys.stderr.write(f"Command failed ({' '.join(cmd)}):\n{result.stderr or result.stdout}\n")
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def write_output_json(path: Path, payload: dict, uid: int, gid: int) -> None:
    """Best-effort write of a host-side JSON result file, chowning to the caller when possible."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        _chown(path, uid, gid)
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(f"Warning: failed to write output json to {path}: {exc}\n")


def fast_copy(src: Path, dst: Path) -> None:
    """Copy large artifacts efficiently, preferring reflinks when available."""
    cp_bin = shutil.which("cp")
    if cp_bin:
        try:
            subprocess.run(
                [cp_bin, "--reflink=auto", "--sparse=always", str(src), str(dst)],
                check=True,
                capture_output=True,
                text=True,
            )
            return
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(f"cp fallback triggered for {src} -> {dst}: {exc.stderr or exc.stdout}\n")
    shutil.copy2(src, dst)


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """Try to hardlink within the same filesystem; fall back to copying."""
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
        return
    except OSError:
        pass
    fast_copy(src, dst)


def preflight(config: SandboxConfig, require_workspace_tools: bool, require_runner: bool, init_script: Path) -> None:
    """Validate host prerequisites before attempting to launch Firecracker."""
    errors: list[str] = []

    if not Path("/dev/kvm").exists():
        errors.append("/dev/kvm missing.")
    if not config.fc_bin.exists():
        errors.append(f"Firecracker binary missing at {config.fc_bin}")
    if not config.jailer_bin.exists():
        errors.append(f"Jailer binary missing at {config.jailer_bin}")
    if not config.kernel.exists():
        errors.append(f"Kernel image missing at {config.kernel}")
    if not config.rootfs.exists():
        errors.append(f"Rootfs image missing at {config.rootfs}")
    if not init_script.exists():
        errors.append(f"Init script missing at {init_script}")

    tap_check = subprocess.run(["ip", "link", "show", config.acore_tap], capture_output=True)
    if tap_check.returncode != 0:
        errors.append(
            f"TAP interface {config.acore_tap} not found. Run setup.sh first (expects a TAP pool like {DEFAULT_TAP_PREFIX}0...)."
        )

    if require_workspace_tools:
        if shutil.which("mkfs.ext4") is None:
            errors.append("mkfs.ext4 not found (required to build workspace image).")
        if shutil.which("dd") is None:
            errors.append("dd not found (required to size workspace image).")
        if not RUNNER_SCRIPT.exists():
            errors.append(f"Terraform runner script missing at {RUNNER_SCRIPT}")

    if errors:
        for err in errors:
            sys.stderr.write(f"Pre-flight error: {err}\n")
        raise SystemExit(1)


def list_taps(prefix: str) -> list[str]:
    """Return tap device names matching prefix (sorted)."""
    result = subprocess.run(["ip", "-o", "link", "show"], text=True, capture_output=True)
    if result.returncode != 0:
        return []
    taps: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        name = parts[1].strip()
        if name.startswith(prefix):
            taps.append(name)
    return sorted(taps)


def allocate_tap(prefix: str, lock_dir: Path) -> tuple[str, Path]:
    """
    Allocate a tap device from a pre-created pool using an exclusive lock file.

    This avoids requiring CAP_NET_ADMIN at runtime (setup.sh creates the pool).
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    taps = list_taps(prefix)
    if not taps:
        raise SystemExit(f"No TAP devices found with prefix '{prefix}'. Run setup.sh to create a TAP pool.")

    for tap in taps:
        lock_path = lock_dir / f"{tap}.lock"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Attempt to reclaim stale locks.
            try:
                payload = lock_path.read_text(encoding="utf-8", errors="ignore")
                pid_text = payload.split("pid=", 1)[1].splitlines()[0].strip() if "pid=" in payload else ""
                stale_pid = int(pid_text) if pid_text else None
            except Exception:
                stale_pid = None

            if stale_pid is not None:
                try:
                    os.kill(stale_pid, 0)
                except ProcessLookupError:
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    continue
                except PermissionError:
                    pass
            continue
        except OSError as exc:
            raise SystemExit(f"Failed to allocate tap lock {lock_path}: {exc}") from exc
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\n")
            return tap, lock_path

    raise SystemExit(f"No free TAP devices available for prefix '{prefix}' (all locked).")


def release_tap(lock_path: Optional[Path]) -> None:
    """Best-effort release of a previously allocated TAP lock."""
    if not lock_path:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        sys.stderr.write(f"Warning: failed to release tap lock {lock_path}: {exc}\n")


def generate_guest_mac(seed: str) -> str:
    """
    Generate a locally-administered unicast MAC address.

    With a shared bridge, MACs must be unique across VMs.
    """
    digest = uuid.uuid5(uuid.NAMESPACE_DNS, seed).bytes
    mac_bytes = bytes([0x02, 0xFC, digest[0], digest[1], digest[2], digest[3]])
    return ":".join(f"{b:02X}" for b in mac_bytes)


def derive_static_ip_from_tap(tap_name: str) -> str:
    """
    Derive a deterministic, non-DHCP IPv4 address from a tap device name.

    This avoids DHCP bursts under high parallelism. The address is in 172.16.0.0/24 and
    uses host-reserved space outside the dnsmasq DHCP pool (defaults to 172.16.0.100-199).
    """
    match = re.search(r"(\d+)$", tap_name)
    if not match:
        raise SystemExit(f"Cannot derive static IP from tap name '{tap_name}' (expected trailing digits).")
    index = int(match.group(1))
    last_octet = 2 + index
    if last_octet < 2 or last_octet > 254:
        raise SystemExit(f"Tap index {index} produces invalid IPv4 last octet {last_octet}.")
    return f"172.16.0.{last_octet}/24"


def prepare_chroot(config: SandboxConfig) -> None:
    """Create the jailed filesystem and stage kernel/rootfs artifacts."""
    chroot = config.chroot
    chroot.parent.mkdir(parents=True, exist_ok=True)
    (chroot / "dev").mkdir(parents=True, exist_ok=True)
    (chroot / "run").mkdir(parents=True, exist_ok=True)
    (chroot / "tmp").mkdir(parents=True, exist_ok=True)
    (chroot / "results").mkdir(parents=True, exist_ok=True)

    kernel_dest = chroot / "vmlinux.bin"
    fast_copy(config.kernel, kernel_dest)

    # Rootfs is mounted read-only; hardlink within the same filesystem to avoid full copies when possible.
    hardlink_or_copy(config.rootfs, config.rootfs_copy)

    kvm_path = chroot / "dev" / "kvm"
    if kvm_path.exists():
        kvm_path.unlink()

    _chown(chroot, config.jailer_uid, config.jailer_gid)
    for sub in ("dev", "run", "tmp"):
        _chown(chroot / sub, config.jailer_uid, config.jailer_gid)
    _chown(chroot / "results", config.jailer_uid, config.jailer_gid)
    _chown(chroot / "vmlinux.bin", config.jailer_uid, config.jailer_gid)
    _chown(config.rootfs_copy, config.jailer_uid, config.jailer_gid)


def inject_guest_payload(config: SandboxConfig, init_script: Path) -> None:
    """Mount the copied rootfs and drop the init payload inside."""
    mount_dir = Path(tempfile.mkdtemp())
    mounted = False
    try:
        run(["mount", "-o", "loop", str(config.rootfs_copy), str(mount_dir)])
        mounted = True

        payload_path = mount_dir / "init-sandbox.sh"
        payload_path.write_text(init_script.read_text(encoding="utf-8"))
        payload_path.chmod(0o755)

        net_checks_dest = mount_dir / "acore-net-checks.py"
        net_checks_dest.write_text(NET_CHECKS_SCRIPT.read_text(encoding="utf-8"))
        net_checks_dest.chmod(0o755)
    finally:
        if mounted:
            run(["umount", str(mount_dir)])
        shutil.rmtree(mount_dir, ignore_errors=True)


def safe_extract_zip(zip_path: Path) -> Path:
    """Extract a zip archive defensively to a temp directory with strict limits."""
    extract_dir = Path(tempfile.mkdtemp(prefix="workspace-"))
    max_files = 100
    max_bytes = 50 * 1024 * 1024  # 50MB total
    max_entry_bytes = 100 * 1024 * 1024  # per-file guardrail
    total_files = 0
    total_bytes = 0

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"Refusing unsafe zip entry: {member.filename}")

                # Block symlinks in the archive (avoid host path escapes).
                is_symlink = (member.external_attr >> 16) & 0o120000 == 0o120000
                if is_symlink:
                    raise ValueError(f"Refusing symlink entry in archive: {member.filename}")

                if member.file_size > max_entry_bytes:
                    raise ValueError(f"Refusing zip entry over size limit ({member.file_size} bytes): {member.filename}")

                target = (extract_dir / member_path).resolve()
                if not str(target).startswith(str(extract_dir.resolve())):
                    raise ValueError(f"Refusing path traversal attempt: {member.filename}")

                total_files += 1
                if total_files > max_files:
                    raise ValueError(f"Refusing zip: file count exceeds limit ({max_files}).")

                total_bytes += member.file_size
                if total_bytes > max_bytes:
                    raise ValueError(f"Refusing zip: extracted size exceeds limit ({max_bytes} bytes).")

                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as source, open(target, "wb") as dest:
                    shutil.copyfileobj(source, dest)
    except Exception:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise

    return extract_dir


def copy_workspace_dir(source: Path) -> Path:
    """Copy a local workspace directory to a temp location."""
    if not source.exists() or not source.is_dir():
        raise NotADirectoryError(f"Workspace source is not a directory: {source}")

    dest_dir = Path(tempfile.mkdtemp(prefix="workspace-"))
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", ".terraform", "terraform.tfstate.d")
    shutil.copytree(source, dest_dir, dirs_exist_ok=True, ignore=ignore)
    return dest_dir


def sanitize_workspace_dir(workspace_dir: Path) -> None:
    """
    Sanitizes the workspace by enforcing a strict file extension allowlist.
    Deletes .terraform folders, lock files, and any non-infrastructure files.
    """
    # 1. ALLOWLIST: Terraform configs plus required metadata/state files.
    allowed_extensions = {".tf", ".tf.json", ".tfvars", ".tfvars.json", ".tfstate"}
    allowed_filenames = {
        "task.json",
        "miner.json",
        "terraform.tfstate",
        "terraform.tfstate.backup",
    }

    # 2. BLACKLIST: Explicitly unwanted directories
    # (Though we clean everything else, it's good to be explicit for logging/debugging)
    remove_dirs = {".terraform", ".git", "__pycache__"}

    # Walk the directory tree bottom-up so we can safely remove directories
    for root, dirs, files in os.walk(workspace_dir, topdown=False):
        root_path = Path(root)

        # A. Remove blacklisted directories immediately
        for d in dirs:
            if d in remove_dirs:
                shutil.rmtree(root_path / d)

        # Prune dirs list to prevent walking into them (if topdown=True)
        # Since we are topdown=False, this is just for the current loop context
        dirs[:] = [d for d in dirs if d not in remove_dirs]

        # B. Filter Files
        for filename in files:
            file_path = root_path / filename

            # Special Case: .terraform.lock.hcl
            # Delete this to ensure the miner cannot pin to a malicious provider version
            # that ignores your filesystem mirror.
            if filename == ".terraform.lock.hcl":
                file_path.unlink()
                continue

            # Check Extension
            # .suffixes returns list like ['.tf'] or ['.tf', '.json']
            # We check the final suffix or the full compound extension
            ext = "".join(file_path.suffixes).lower()

            # Simple check: does the file end with one of our allowed extensions?
            is_allowed = filename in allowed_filenames or any(filename.lower().endswith(allowed) for allowed in allowed_extensions)

            if not is_allowed:
                # Log it if you want debugging, otherwise just destroy it
                # print(f"Sanitizing: Removing disallowed file {file_path.name}")
                try:
                    file_path.unlink()
                except OSError:
                    pass

    # 3. Final Safety: Ensure .terraform is definitely gone
    shutil.rmtree(workspace_dir / ".terraform", ignore_errors=True)


def stage_workspace(
    source: Path,
    creds_path: Optional[Path],
    *,
    is_zip: bool,
    tf_provider_debug: bool = False,
) -> Path:
    """Prepare a workspace (zip or directory), copy the runner, and bundle auth artifacts."""
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    # Security model: only a short-lived token enters the guest. If a creds file is supplied,
    # we use it host-side to mint a token and still only inject the token.
    if not token and creds_path is not None:
        token = mint_access_token_from_service_account(creds_path)
    if not token:
        raise SystemExit("GOOGLE_OAUTH_ACCESS_TOKEN must be set before running the sandbox.")

    if is_zip:
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Workspace zip not found or not a file: {source}")
        workspace_dir = safe_extract_zip(source)
    else:
        workspace_dir = copy_workspace_dir(source)
    sanitize_workspace_dir(workspace_dir)
    # Remove any pre-baked Terraform state/providers to force init against the trusted mirror.
    shutil.rmtree(workspace_dir / ".terraform", ignore_errors=True)
    try:
        (workspace_dir / ".terraform.lock.hcl").unlink()
    except FileNotFoundError:
        pass

    runner_target = workspace_dir / RUNNER_SCRIPT.name
    shutil.copy2(RUNNER_SCRIPT, runner_target)
    runner_target.chmod(0o755)

    # Persist the token for the guest; init scripts will refuse to proceed if it is missing.
    token_path = workspace_dir / "gcp-access-token"
    token_path.write_text(token, encoding="utf-8")
    os.chmod(token_path, 0o600)

    if tf_provider_debug:
        (workspace_dir / "tf-provider-debug").write_text("1", encoding="utf-8")

    # Create a minimal ADC file that short-circuits metadata probing inside client libraries.
    # This contains only the short-lived token (no refresh token, no service-account key).
    adc_payload = {
        "type": "authorized_user",
        "client_id": "acore-sandbox-local",
        "client_secret": "acore-sandbox-local",
        "token": token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/cloud-platform",
        ],
    }
    adc_path = workspace_dir / CREDS_DEST
    adc_path.write_text(json.dumps(adc_payload), encoding="utf-8")
    os.chmod(adc_path, 0o600)

    return workspace_dir


def build_validator_bundle(validate_script: Path) -> Path:
    """Create a temporary validator bundle directory with validate.py and repo code."""
    bundle_dir = Path(tempfile.mkdtemp(prefix="validator-bundle-"))

    repo_root: Optional[Path] = None
    for parent in Path(__file__).resolve().parents:
        if (parent / "modules").is_dir() and (parent / "subnet").is_dir():
            repo_root = parent
            break
    if repo_root is None:
        raise FileNotFoundError("Could not locate repo root (looked for both modules/ and subnet/ in parent directories)")

    validate_src = validate_script
    if not validate_src.is_absolute():
        validate_src = (Path(__file__).resolve().parent / validate_src).resolve()
    if not validate_src.exists():
        raise FileNotFoundError(f"Validator script not found: {validate_src}")

    # Copy validate.py to bundle root.
    shutil.copy2(validate_src, bundle_dir / "validate.py")

    # Copy the packages needed by validate.py into the bundle.
    copy_into_workspace(repo_root / "modules", bundle_dir)
    copy_into_workspace(repo_root / "subnet", bundle_dir)

    # Optional task_config for validator.
    config_src = repo_root / "modules" / "task_config.yaml"
    if config_src.exists():
        shutil.copy2(config_src, bundle_dir / "task_config.yaml")

    return bundle_dir


def copy_into_workspace(src: Path, workspace_dir: Path) -> None:
    """Copy a file or directory into the workspace root (best-effort, overwriting existing)."""
    if not src.exists():
        raise FileNotFoundError(f"Include path not found: {src}")

    dest = workspace_dir / src.name
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", "terraform.tfstate.d", ".terraform")
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except FileNotFoundError:
                continue
    return total


def build_workspace_image(workspace_dir: Path, config: SandboxConfig) -> None:
    """Create an ext4 image containing the workspace contents."""
    required_bytes = _dir_size_bytes(workspace_dir)
    # Reserve headroom for provider installs and state (providers can be large).
    size_mb = max(512, int((required_bytes + (1024 * 1024) - 1) / (1024 * 1024)) + 128)

    if config.workspace_image.exists():
        config.workspace_image.unlink()

    # Sparse allocate the image to avoid zero-filling delays.
    with config.workspace_image.open("wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    run(
        [
            "mkfs.ext4",
            "-F",
            "-E",
            "lazy_itable_init=1,lazy_journal_init=1",
            "-d",
            str(workspace_dir),
            str(config.workspace_image),
        ]
    )
    _chown(config.workspace_image, config.jailer_uid, config.jailer_gid)


def build_workspace_rw_image(config: SandboxConfig) -> None:
    """Create a writable scratch image for overlay upper/workdir (keeps base workspace read-only)."""
    if config.workspace_rw_image.exists():
        config.workspace_rw_image.unlink()
    with config.workspace_rw_image.open("wb") as fh:
        fh.truncate(config.workspace_rw_size_mb * 1024 * 1024)
    run(
        [
            "mkfs.ext4",
            "-F",
            "-E",
            "lazy_itable_init=1,lazy_journal_init=1",
            str(config.workspace_rw_image),
        ]
    )
    _chown(config.workspace_rw_image, config.jailer_uid, config.jailer_gid)


def build_results_image(config: SandboxConfig, size_mb: int = 8) -> None:
    """Create a tiny ext4 image for result artifacts."""
    if config.results_image.exists():
        config.results_image.unlink()
    with config.results_image.open("wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    run(
        [
            "mkfs.ext4",
            "-F",
            "-E",
            "lazy_itable_init=1,lazy_journal_init=1",
            str(config.results_image),
        ]
    )
    _chown(config.results_image, config.jailer_uid, config.jailer_gid)


def build_validator_image(bundle_dir: Path, config: SandboxConfig) -> None:
    """Create a read-only ext4 image containing the validator bundle."""
    if config.validator_image.exists():
        config.validator_image.unlink()
    required_bytes = _dir_size_bytes(bundle_dir)
    size_mb = max(64, int((required_bytes + (1024 * 1024) - 1) / (1024 * 1024)) + 16)
    with config.validator_image.open("wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    run(
        [
            "mkfs.ext4",
            "-F",
            "-E",
            "lazy_itable_init=1,lazy_journal_init=1",
            "-d",
            str(bundle_dir),
            str(config.validator_image),
        ]
    )
    _chown(config.validator_image, config.jailer_uid, config.jailer_gid)


def wait_for_socket(api_socket: Path, timeout: float = 5.0) -> None:
    """Wait for the Firecracker API socket to appear."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api_socket.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"API socket did not appear at {api_socket}")


def curl_put(api_socket: Path, endpoint: str, data: str) -> None:
    """Send a PUT request to the Firecracker API via curl."""
    cmd = [
        "curl",
        "-fs",
        "--unix-socket",
        str(api_socket),
        "-X",
        "PUT",
        f"http://localhost/{endpoint}",
        "-H",
        "Content-Type: application/json",
        "-d",
        data,
    ]
    run(cmd)


def start_firecracker(config: SandboxConfig) -> subprocess.Popen:
    """Launch the jailer + Firecracker process."""
    log_handle = config.log_file.open("w", encoding="utf-8")
    cmd = [
        str(config.jailer_bin),
        "--id",
        config.id,
        "--uid",
        str(config.jailer_uid),
        "--gid",
        str(config.jailer_gid),
        "--chroot-base-dir",
        str(config.chroot_base),
        "--exec-file",
        str(config.fc_bin),
        "--",
        "--api-sock",
        "/run/fc.sock",
    ]
    process = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT)
    log_handle.close()
    return process


def stream_log(config: SandboxConfig, process: subprocess.Popen, max_bytes: int = 10 * 1024 * 1024) -> None:
    """Continuously stream the Firecracker log until the process exits or max_bytes is hit."""
    consumed = 0
    try:
        with config.log_file.open("r", encoding="utf-8") as fh:
            fh.seek(0, os.SEEK_END)
            while process.poll() is None:
                where = fh.tell()
                line = fh.readline()
                if not line:
                    time.sleep(0.2)
                    fh.seek(where)
                else:
                    consumed += len(line.encode("utf-8", errors="ignore"))
                    print(line.rstrip("\n"))
                    if consumed >= max_bytes:
                        print(f"(log streaming truncated after {max_bytes} bytes)")
                        process.terminate()
                        break
    except FileNotFoundError:
        print("(log file not found for streaming)")


def dump_guest_log(config: SandboxConfig) -> None:
    """Print the VM log to stdout."""
    print("\n=== LOG OUTPUT ===")
    try:
        print(config.log_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("(No log output found)")
    print("=== END LOG ===\n")


def extract_results(config: SandboxConfig) -> ExtractionResult:
    """Mount the results image, read success/error JSON, and return a parsed result."""
    results_img = config.results_image
    if not results_img.exists():
        return ExtractionResult(success=False, error="Results image not found.")

    mount_dir = Path(tempfile.mkdtemp())
    mounted = False
    try:
        mount_errors: list[str] = []
        for opts in ("loop,ro,noexec,nosuid,noload", "loop,ro,noexec,nosuid"):
            try:
                subprocess.run(
                    ["mount", "-o", opts, str(results_img), str(mount_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                mounted = True
                break
            except subprocess.CalledProcessError as exc:
                mount_errors.append(exc.stderr or exc.stdout or str(exc))

        if not mounted:
            return ExtractionResult(success=False, error="; ".join(mount_errors) or "Failed to mount results image.")

        success_path = mount_dir / "success.json"
        if success_path.exists():
            payload = json.loads(success_path.read_text(encoding="utf-8"))
            score = payload.get("score")
            return ExtractionResult(
                success=True,
                score=float(score) if score is not None else None,
                success_json=payload,
            )

        error_path = mount_dir / "error.json"
        if error_path.exists():
            payload = json.loads(error_path.read_text(encoding="utf-8"))
            score = payload.get("score")
            message = payload.get("msg") or payload.get("error") or "Unknown error"
            return ExtractionResult(
                success=False,
                score=float(score) if score is not None else None,
                error=message,
                error_json=payload,
            )

        return ExtractionResult(success=False, error="No result files found.", error_json={"msg": "No result files found.", "score": 0})
    except Exception as exc:  # pylint: disable=broad-except
        return ExtractionResult(success=False, error=str(exc))
    finally:
        if mounted:
            run(["umount", str(mount_dir)])
        shutil.rmtree(mount_dir, ignore_errors=True)


def print_result_files(chroot: Path, results_path: str = "results.ext4") -> None:
    """Attempt to mount and print result JSON files from the results image."""
    results_img = chroot / results_path
    if not results_img.exists():
        print("(No results image found)")
        return
    mount_dir = Path(tempfile.mkdtemp())
    mounted = False
    try:
        try:
            run(["mount", "-o", "loop,ro,noexec,nosuid,noload", str(results_img), str(mount_dir)])
        except subprocess.CalledProcessError:
            run(["mount", "-o", "loop,ro,noexec,nosuid", str(results_img), str(mount_dir)])
        mounted = True
        found_any = False
        for name in ("success.json", "error.json"):
            candidate = mount_dir / name
            if candidate.exists():
                print(f"\n=== {name.upper()} ===\n")
                try:
                    content = candidate.read_text(encoding="utf-8")
                    print(content)
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"(Failed to read {name}: {exc})")
                found_any = True
        if not found_any:
            print("(No result JSON files found)")
        print("\n=== END RESULTS ===\n")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"(Could not read result files: {exc})")
    finally:
        if mounted:
            run(["umount", str(mount_dir)])
        shutil.rmtree(mount_dir, ignore_errors=True)


def copy_results(chroot: Path, dest_dir: Path) -> bool:
    """Copy success/error JSON files out of the results image into dest_dir. Returns True if any file copied."""
    results_img = chroot / "results.ext4"
    if not results_img.exists():
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    mount_dir = Path(tempfile.mkdtemp())
    mounted = False
    found_any = False
    try:
        for opts in ("loop,ro,noexec,nosuid,noload", "loop,ro,noexec,nosuid"):
            try:
                subprocess.run(
                    ["mount", "-o", opts, str(results_img), str(mount_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                mounted = True
                break
            except subprocess.CalledProcessError:
                continue
        for name in ("success.json", "error.json"):
            if mounted:
                src = mount_dir / name
                if src.exists():
                    shutil.copy2(src, dest_dir / name)
                    found_any = True
    except Exception:
        pass
    finally:
        if mounted:
            run(["umount", str(mount_dir)])
        shutil.rmtree(mount_dir, ignore_errors=True)
    return found_any


def cleanup(config: SandboxConfig, process: Optional[subprocess.Popen]) -> None:
    """Best-effort cleanup of Firecracker process and chroot."""
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    shutil.rmtree(config.chroot_base / "firecracker" / config.id, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Firecracker sandbox runner with optional Terraform workspace.")
    workspace_group = parser.add_mutually_exclusive_group()
    workspace_group.add_argument("--workspace-zip", type=Path, help="Path to a workspace zip to mount in the guest VM.")
    workspace_group.add_argument("--workspace-dir", type=Path, help="Path to a workspace directory to copy into the guest VM.")
    parser.add_argument("--creds-file", type=Path, help="Path to a GCP creds JSON file to inject into the workspace.")
    parser.add_argument(
        "--stream-log",
        action="store_true",
        help="Stream the Firecracker log to stdout while the VM is running.",
    )
    parser.add_argument(
        "--quiet-kernel",
        action="store_true",
        help="Append quiet loglevel=3 to kernel boot args to suppress most kernel console output.",
    )
    parser.add_argument(
        "--include-path",
        action="append",
        type=Path,
        default=[],
        help="Additional file or directory to copy into the workspace root before building the image (repeatable).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for the guest VM to complete before termination (default: 120).",
    )
    parser.add_argument(
        "--enable-tf-provider-debug",
        action="store_true",
        help="Enable TF_LOG_PROVIDER=DEBUG inside the guest (for terraform provider debugging).",
    )
    parser.add_argument(
        "--net-checks",
        action="store_true",
        help="Enable guest network-policy self-checks at boot (also enabled via host env ACORE_NET_CHECKS=1).",
    )
    parser.add_argument(
        "--net-check-timeout",
        type=int,
        default=5,
        help="Timeout in seconds for individual guest network checks (default: 5).",
    )
    parser.add_argument(
        "--tap",
        help="Host tap device to attach the microVM (defaults to allocating a free tap from the pool).",
    )
    parser.add_argument(
        "--tap-prefix",
        default=DEFAULT_TAP_PREFIX,
        help=f"TAP pool prefix to allocate from (default: {DEFAULT_TAP_PREFIX}).",
    )
    parser.add_argument(
        "--tap-lock-dir",
        type=Path,
        default=Path(os.environ.get("ACORE_TAP_LOCK_DIR", "/tmp/acore-tap-locks")),
        help="Directory for per-tap lockfiles (default: /tmp/acore-tap-locks or $ACORE_TAP_LOCK_DIR).",
    )
    parser.add_argument(
        "--static-ip",
        help="Configure a static guest IPv4 on eth0 (skips DHCP). Accepts 172.16.0.X or 172.16.0.X/24.",
    )
    parser.add_argument(
        "--dhcp",
        action="store_true",
        help="Use DHCP for the guest network (overrides the default static IP derived from the tap).",
    )
    parser.add_argument(
        "--static-ip-from-tap",
        action="store_true",
        help="Derive a unique static guest IPv4 from the allocated tap suffix (skips DHCP).",
    )
    parser.add_argument(
        "--static-gateway",
        default="172.16.0.1",
        help="Default gateway for --static-ip mode (default: 172.16.0.1).",
    )
    parser.add_argument(
        "--static-dns",
        default="172.16.0.1",
        help="DNS server for --static-ip mode (default: 172.16.0.1).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Write a host-side JSON summary of the sandbox run (success/score/error).",
    )
    args = parser.parse_args()

    workspace_is_zip = args.workspace_zip is not None
    workspace_source: Optional[Path] = args.workspace_zip if workspace_is_zip else args.workspace_dir
    if workspace_source is None:
        sys.stderr.write("Workspace is required (--workspace-dir or --workspace-zip).\n")
        return 1

    test_id = f"fctest-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    config = SandboxConfig(id=test_id)
    process: Optional[subprocess.Popen] = None
    tap_lock: Optional[Path] = None
    guest_mac = generate_guest_mac(config.id)
    workspace_dir: Optional[Path] = None
    validator_bundle_dir: Optional[Path] = None
    workspace_enabled = workspace_source is not None
    stream_thread: Optional[threading.Thread] = None
    results_tmp_dir: Optional[tempfile.TemporaryDirectory] = None

    try:
        if args.tap:
            config.acore_tap = args.tap
        else:
            config.acore_tap, tap_lock = allocate_tap(args.tap_prefix, args.tap_lock_dir)
        print(f"==> [Host] Using TAP device: {config.acore_tap}")

        print(f"==> [Host] Preparing chroot: {config.chroot}")
        preflight(
            config,
            require_workspace_tools=workspace_enabled,
            require_runner=workspace_enabled,
            init_script=INIT_SCRIPT,
        )
        prepare_chroot(config)

        if workspace_source:
            if workspace_is_zip:
                print("==> [Host] Preparing workspace image from zip (safe extraction)...")
            else:
                print("==> [Host] Preparing workspace image from directory...")
            workspace_dir = stage_workspace(
                workspace_source,
                args.creds_file,
                is_zip=workspace_is_zip,
                tf_provider_debug=args.enable_tf_provider_debug,
            )
            for include in args.include_path:
                print(f"==> [Host] Including path in workspace: {include}")
                copy_into_workspace(include, workspace_dir)
            validator_bundle_dir = build_validator_bundle(DEFAULT_VALIDATE_SCRIPT)
            build_workspace_image(workspace_dir, config)
            build_workspace_rw_image(config)
            print("==> [Host] Preparing results image...")
            build_results_image(config)
            print("==> [Host] Preparing validator bundle image...")
            build_validator_image(validator_bundle_dir, config)

        print("==> [Host] Injecting init script...")
        inject_guest_payload(config, INIT_SCRIPT)

        print(f"==> [Host] Starting Firecracker (Logs -> {config.log_file})...")
        process = start_firecracker(config)
        stream_thread: Optional[threading.Thread] = None
        if args.stream_log:
            stream_thread = threading.Thread(target=stream_log, args=(config, process), daemon=True)
            stream_thread.start()
        wait_for_socket(config.api_socket)

        print("==> [Host] Configuring VM...")
        if not os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN") and args.creds_file is None:
            raise SystemExit("GOOGLE_OAUTH_ACCESS_TOKEN must be set before running the sandbox (or pass --creds-file to mint one).")
        curl_put(config.api_socket, "machine-config", f'{{"vcpu_count": {config.vcpus}, "mem_size_mib": {config.mem_mb}}}')
        boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init-sandbox.sh root=/dev/vda ro"

        if args.dhcp and (args.static_ip or args.static_ip_from_tap):
            raise SystemExit("Use only one of --dhcp and --static-ip/--static-ip-from-tap.")

        static_ip = args.static_ip
        if args.static_ip_from_tap:
            static_ip = derive_static_ip_from_tap(config.acore_tap)
        elif not args.dhcp and not static_ip:
            # Default to a deterministic static IPv4 derived from the tap suffix. This avoids
            # DHCP contention when running many microVMs in parallel.
            static_ip = derive_static_ip_from_tap(config.acore_tap)
        if static_ip:
            boot_args = (
                f"{boot_args} acore_static_ip={static_ip} "
                f"acore_static_gw={args.static_gateway} acore_static_dns={args.static_dns}"
            )

        net_checks = args.net_checks or os.environ.get("ACORE_NET_CHECKS") == "1"
        if net_checks:
            timeout = args.net_check_timeout
            timeout_env = os.environ.get("ACORE_NET_CHECK_TIMEOUT")
            if timeout_env:
                try:
                    timeout = int(timeout_env)
                except ValueError:
                    pass
            boot_args = f"{boot_args} acore_net_checks=1 acore_net_check_timeout={timeout}"
        if args.quiet_kernel:
            boot_args = f"{boot_args} quiet loglevel=3"
        curl_put(
            config.api_socket,
            "boot-source",
            f'{{"kernel_image_path": "/vmlinux.bin", "boot_args": "{boot_args}"}}',
        )
        curl_put(
            config.api_socket,
            "drives/rootfs",
            '{"drive_id": "rootfs", "path_on_host": "/rootfs.ext4", "is_root_device": true, "is_read_only": true}',
        )
        if workspace_enabled:
            curl_put(
                config.api_socket,
                "drives/workspace",
                '{"drive_id": "workspace", "path_on_host": "/workspace.ext4", "is_root_device": false, "is_read_only": true}',
            )
            curl_put(
                config.api_socket,
                "drives/workspacerw",
                '{"drive_id": "workspacerw", "path_on_host": "/workspace-rw.ext4", "is_root_device": false, "is_read_only": false}',
            )
            curl_put(
                config.api_socket,
                "drives/results",
                '{"drive_id": "results", "path_on_host": "/results.ext4", "is_root_device": false, "is_read_only": false}',
            )
            curl_put(
                config.api_socket,
                "drives/validator",
                '{"drive_id": "validator", "path_on_host": "/validator.ext4", "is_root_device": false, "is_read_only": true}',
            )
        curl_put(
            config.api_socket,
            "network-interfaces/eth0",
            f'{{"iface_id":"eth0","guest_mac":"{guest_mac}","host_dev_name":"{config.acore_tap}"}}',
        )
        print("==> [Host] Booting...")
        curl_put(config.api_socket, "actions", '{"action_type": "InstanceStart"}')

        print(f"==> [Host] Watching process {process.pid}...")
        try:
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        extracted_result = extract_results(config)
        if args.stream_log and stream_thread:
            stream_thread.join(timeout=2)
        print("==> [Host] VM Exited. Verifying results...")
        results_tmp_dir = tempfile.TemporaryDirectory(prefix="sandbox-results-")
        results_dir = Path(results_tmp_dir.name)
        found_results = copy_results(config.chroot, results_dir)
        print_result_files(config.chroot)
        if not args.stream_log:
            dump_guest_log(config)

        result_payload = {
            "id": config.id,
            "success": extracted_result.success,
            "score": extracted_result.score,
            "error": extracted_result.error,
            "tap": config.acore_tap,
            "success_json": extracted_result.success_json,
            "error_json": extracted_result.error_json,
        }
        if args.output_json:
            write_output_json(args.output_json, result_payload, config.jailer_uid or 0, config.jailer_gid or 0)

        if extracted_result.success:
            if extracted_result.score is not None:
                print(f"final score: {extracted_result.score}")
            try:
                config.log_file.unlink()
            except FileNotFoundError:
                pass
            return 0

        print("❌ FAILURE: Validation failed.")
        if extracted_result.error:
            print(f"Error: {extracted_result.error}")
        if extracted_result.score is not None:
            print(f"Score: {extracted_result.score}")
        if not found_results:
            print("❌ FAILURE: No result JSON found.")
        print(f"See full log at: {config.log_file}")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted by user.\n")
        if "config" in locals() and args.output_json:
            write_output_json(
                args.output_json,
                {"id": config.id, "success": False, "score": None, "error": "interrupted", "tap": config.acore_tap},
                config.jailer_uid or 0,
                config.jailer_gid or 0,
            )
        return 130
    except Exception as exc:  # pylint: disable=broad-except
        sys.stderr.write(f"Sandbox run failed: {exc}\n")
        if "config" in locals() and args.output_json:
            write_output_json(
                args.output_json,
                {"id": config.id, "success": False, "score": None, "error": str(exc), "tap": config.acore_tap},
                config.jailer_uid or 0,
                config.jailer_gid or 0,
            )
        return 1
    finally:
        release_tap(tap_lock)
        cleanup(config, process)
        if workspace_dir:
            if workspace_is_zip:
                print("==> [Host] Cleaning up extracted workspace...")
            shutil.rmtree(workspace_dir, ignore_errors=True)
        if validator_bundle_dir:
            shutil.rmtree(validator_bundle_dir, ignore_errors=True)
        if results_tmp_dir:
            results_tmp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(main())
