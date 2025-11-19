"""
Base class for AlphaCore miners.

The implementation is intentionally lightweight: it wires up a Bittensor axon
when possible, exposes lifecycle helpers, and validates incoming requests via
simple blacklist/priority hooks.  Concrete miners are expected to override the
`forward` coroutine endpoint.
"""

from __future__ import annotations

import abc
import time
from typing import Iterable, Optional, Tuple

import bittensor as bt

from alphacore.base.neuron import BaseNeuron
from alphacore.bittensor_config import config as build_config
from alphacore.protocol import TaskSynapse


class BaseMiner(BaseNeuron, abc.ABC):
    neuron_type: str = "MinerNeuron"

    def __init__(self, config: Optional[bt.config] = None) -> None:
        super().__init__(config=config or build_config(role="miner"))

        self.axon: Optional[bt.axon] = None
        self.validator_blacklist: set[str] = set()
        self._setup_axon()

    # ------------------------------------------------------------------ #
    # Axon wiring
    # ------------------------------------------------------------------ #

    def _setup_axon(self) -> None:
        """Attach miner endpoints to a Bittensor axon if a wallet is available."""
        if self.wallet is None:
            bt.logging.warning("Wallet not initialised; miner axon will not start.")
            return

        try:
            self.axon = bt.axon(wallet=self.wallet, config=self.config)
            self.axon.attach(
                forward_fn=self.forward,
                blacklist_fn=self.blacklist,
                priority_fn=self.priority,
            )
            bt.logging.info("Miner axon configured with default endpoints.")
        except Exception as exc:
            bt.logging.warning(f"Unable to configure miner axon: {exc}")
            self.axon = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """
        Default run loop: start serving the axon (when available) and keep the
        process alive until `should_exit` is signalled.
        """
        if self.axon is None:
            bt.logging.info("No axon configured; idle miner run loop started.")
        else:
            try:
                self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
                self.axon.start()
                bt.logging.info(
                    "Miner axon listening at %s:%s",
                    getattr(self.axon, "external_ip", "0.0.0.0"),
                    getattr(self.axon, "external_port", "unknown"),
                )
            except Exception as exc:
                bt.logging.warning(f"Failed to start miner axon: {exc}")

        try:
            while not self.should_exit:
                time.sleep(1)
        finally:
            if self.axon is not None:
                self.axon.stop()

    # ------------------------------------------------------------------ #
    # RPC endpoints
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def forward(self, synapse: TaskSynapse) -> TaskSynapse:
        """Handle a task request broadcast by a validator."""

    def set_validator_blacklist(self, hotkeys: Optional[Iterable[str]]) -> None:
        self.validator_blacklist = set(hotkeys or [])

    async def blacklist(self, synapse: TaskSynapse) -> Tuple[bool, str]:
        hotkey = getattr(getattr(synapse, "dendrite", None), "hotkey", None) or getattr(
            synapse, "validator_hotkey", None
        )
        if hotkey and hotkey in self.validator_blacklist:
            bt.logging.warning("Rejecting request from blacklisted validator hotkey=%s", hotkey)
            return True, "validator blacklisted"
        return False, "ok"

    def priority(self, synapse: TaskSynapse) -> float:
        return 0.0
