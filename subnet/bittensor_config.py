from __future__ import annotations

import sys
import subprocess
import argparse
from pathlib import Path
import os
import bittensor as bt


# ───────────────────────── utilities ───────────────────────── #


def is_cuda_available() -> str:
    """Return 'cuda' if a CUDA device/toolchain looks available, else 'cpu'."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "-L"], stderr=subprocess.STDOUT)
        if b"NVIDIA" in out:
            return "cuda"
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["nvcc", "--version"], stderr=subprocess.STDOUT)
        if b"release" in out:
            return "cuda"
    except Exception:
        pass
    return "cpu"


# ───────────────────── argument groups (defaults) ───────────────────── #


def add_shared_args(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by miners and validators."""
    parser.add_argument("--netuid", type=int, default=1, help="Subnet netuid.")
    parser.add_argument("--mock", action="store_true", default=False,
                        help="Mock neuron and all network components.")

    parser.add_argument("--neuron.device", type=str,
                        default=is_cuda_available(), help="Device to run on (cpu|cuda).")
    parser.add_argument("--neuron.epoch_length", type=int,
                        default=360, help="Epoch length in (~12s) blocks.")
    parser.add_argument("--neuron.events_retention_size", type=int, default=2
                        # 2 GiB
                        * 1024 * 1024 * 1024, help="Max size for persisted event logs (bytes).")
    parser.add_argument("--neuron.dont_save_events", action="store_true",
                        default=False, help="If set, events are not saved to a log file.")


def add_validator_args(parser: argparse.ArgumentParser) -> None:
    """Default validator arguments."""
    parser.add_argument("--neuron.name", type=str, default="validator",
                        help="Trials go in neuron.root/(wallet_cold-wallet_hot)/neuron.name.")

    parser.add_argument("--neuron.timeout", type=float,
                        default=10.0, help="Timeout per forward (seconds).")
    parser.add_argument("--neuron.num_concurrent_forwards",
                        type=int, default=1, help="Concurrent forwards.")
    parser.add_argument("--neuron.sample_size", type=int,
                        default=50, help="Number of miners to query per step.")

    # NOTE: Validators can opt-out of serving an Axon; default is to NOTserve.
    parser.add_argument(
        "--neuron.axon_off",
        "--axon_off",
        action="store_true",
        default=True,
        help="Set this flag to not attempt to serve an Axon (validators only).",
    )

    parser.add_argument("--neuron.vpermit_tao_limit", type=int, default=4096,
                        help="Max TAO allowed to query a validator with a vpermit.")

    parser.add_argument(
        "--neuron.moving_average_alpha",
        type=float,
        default=0.1,
        help="Moving average alpha parameter for validator rewards blending.",
    )

    parser.add_argument(
        "--neuron.disable_set_weights",
        action="store_true",
        help="Disables setting weights.",
        default=True,
    )

    parser.add_argument(
        "--validator.epoch_slots",
        type=int,
        default=None,
        help="Number of slots per epoch for validator round starts.",
    )
    parser.add_argument(
        "--validator.epoch_slot_index",
        type=int,
        default=None,
        help="Override which slot (0-based) this validator uses within the epoch.",
    )
    parser.add_argument(
        "--validator.weights_min_tasks_before_emit",
        type=int,
        default=None,
        help="Minimum completed tasks before emitting weights.",
    )


def add_miner_args(parser: argparse.ArgumentParser) -> None:
    """Default miner arguments."""
    parser.add_argument("--neuron.name", type=str, default="miner",
                        help="Trials go in neuron.root/(wallet_cold-wallet_hot)/neuron.name.")

    # IMPORTANT: default False so AuctionStart/WinSynapse work without vpermit
    parser.add_argument("--blacklist.force_validator_permit", action="store_true",
                        default=True, help="Force incoming requests to have a validator permit.")
    parser.add_argument("--no-blacklist.force_validator_permit", action="store_false",
                        dest="blacklist.force_validator_permit", help="Do NOT force incoming requests to have a validator permit.")

    parser.add_argument("--blacklist.minimum_stake_requirement", type=int,
                        default=10_000, help="Minimum stake required to send requests to miners.")
    parser.add_argument("--blacklist.allow_non_registered", action="store_true",
                        default=False, help="Accept queries from non-registered entities (dangerous).")


# ──────────────────────── main entrypoint ───────────────────────── #


def config(role: str = "auto") -> bt.config:
    """
    Build and return a bittensor config with explicit, layered arg addition:
      1) Core Bittensor groups
      2) Shared defaults
      3) Role-specific defaults (validator | miner | both)
    Args:
        role: "validator", "miner", or "auto" (adds both miner and validator args).
    """
    parser = argparse.ArgumentParser(conflict_handler="resolve")

    # 1) Core bittensor argument groups (SDK 10 uppercase classes)
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)

    # 2) Shared defaults
    add_shared_args(parser)

    # 3) Role-specific
    role = role.lower()
    if role == "validator":
        add_validator_args(parser)
    elif role == "miner":
        add_miner_args(parser)
    else:  # "auto" → include both
        add_validator_args(parser)
        add_miner_args(parser)

    cfg = bt.Config(parser)

    # Apply environment-driven overrides for wallet/network before registration.
    # Wallet name and hotkey must be provided to ensure deterministic identity.
    env_wallet_name = os.getenv("ALPHACORE_WALLET_NAME") or os.getenv("AC_WALLET_NAME")
    if env_wallet_name:
        try:
            cfg.wallet.name = env_wallet_name
        except Exception:
            pass

    env_wallet_hotkey = os.getenv("ALPHACORE_WALLET_HOTKEY") or os.getenv("AC_WALLET_HOTKEY")
    if env_wallet_hotkey:
        try:
            cfg.wallet.hotkey = env_wallet_hotkey
        except Exception:
            pass

    env_wallet_path = os.getenv("ALPHACORE_WALLET_PATH") or os.getenv("BT_WALLET_PATH")
    if env_wallet_path:
        try:
            cfg.wallet.path = env_wallet_path
        except Exception:
            pass

    env_netuid = os.getenv("ALPHACORE_NETUID") or os.getenv("AC_NETUID")
    if env_netuid:
        try:
            cfg.netuid = int(env_netuid)
        except Exception:
            pass

    # Subtensor network and endpoint overrides
    env_network = os.getenv("ALPHACORE_NETWORK") or os.getenv("BT_NETWORK")
    if env_network:
        try:
            cfg.subtensor.network = env_network
        except Exception:
            pass

    env_chain_endpoint = os.getenv("ALPHACORE_CHAIN_ENDPOINT")
    if env_chain_endpoint:
        try:
            cfg.subtensor.chain_endpoint = env_chain_endpoint
        except Exception:
            pass

    # Axon overrides (critical for running multiple local processes without port conflicts).
    env_axon_port = os.getenv("ALPHACORE_AXON_PORT") or os.getenv("BT_AXON_PORT")
    if env_axon_port:
        try:
            cfg.axon.port = int(env_axon_port)
        except Exception:
            pass

    env_axon_ip = os.getenv("ALPHACORE_AXON_IP") or os.getenv("BT_AXON_IP")
    if env_axon_ip:
        try:
            cfg.axon.ip = env_axon_ip
        except Exception:
            pass

    env_external_ip = os.getenv("ALPHACORE_AXON_EXTERNAL_IP") or os.getenv("BT_AXON_EXTERNAL_IP")
    if env_external_ip:
        try:
            cfg.axon.external_ip = env_external_ip
        except Exception:
            pass

    env_external_port = os.getenv("ALPHACORE_AXON_EXTERNAL_PORT") or os.getenv("BT_AXON_EXTERNAL_PORT")
    if env_external_port:
        try:
            cfg.axon.external_port = int(env_external_port)
        except Exception:
            pass

    def _parse_bool(raw: str) -> bool:
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")

    # Blacklist overrides (useful for local development).
    # These are NOT standard bittensor env vars; they are AlphaCore helpers.
    env_force_validator_permit = os.getenv("ALPHACORE_BLACKLIST_FORCE_VALIDATOR_PERMIT") or os.getenv(
        "BT_BLACKLIST_FORCE_VALIDATOR_PERMIT"
    )
    if env_force_validator_permit is not None and env_force_validator_permit != "":
        try:
            cfg.blacklist.force_validator_permit = _parse_bool(env_force_validator_permit)
        except Exception:
            pass

    env_allow_non_registered = os.getenv("ALPHACORE_BLACKLIST_ALLOW_NON_REGISTERED") or os.getenv(
        "BT_BLACKLIST_ALLOW_NON_REGISTERED"
    )
    if env_allow_non_registered is not None and env_allow_non_registered != "":
        try:
            cfg.blacklist.allow_non_registered = _parse_bool(env_allow_non_registered)
        except Exception:
            pass

    env_min_stake = os.getenv("ALPHACORE_BLACKLIST_MINIMUM_STAKE") or os.getenv(
        "BT_BLACKLIST_MINIMUM_STAKE"
    )
    if env_min_stake is not None and env_min_stake != "":
        try:
            cfg.blacklist.minimum_stake_requirement = int(env_min_stake)
        except Exception:
            pass

    return cfg


# ─────────────────────────── convenience ─────────────────────────── #


def detect_role_from_context() -> str:
    """Helper to pick a role at runtime based on script name."""
    exe = Path(sys.argv[0]).name.lower()
    if "validator" in exe:
        return "validator"
    if "miner" in exe:
        return "miner"
    return "auto"
