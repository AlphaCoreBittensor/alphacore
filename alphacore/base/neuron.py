"""
Shared base functionality for AlphaCore neurons.

These helpers intentionally keep the runtime lightweight: they configure
logging, set up (optional) Bittensor primitives, and expose a minimal
life-cycle contract that both miners and validators can extend.
"""

from __future__ import annotations

import abc
import asyncio
import threading
import time
from typing import Optional

import bittensor as bt

from alphacore.bittensor_config import config as build_config


class BaseNeuron(abc.ABC):
    """
    Minimal Bittensor-aware neuron scaffold.

    Subclasses receive a ready-to-use config object and, when possible,
    pre-initialised wallet/subtensor/metagraph handles.  Network calls are
    wrapped in best-effort guards so the skeleton can run in development
    environments without a live chain.
    """

    neuron_type: str = "BaseNeuron"

    def __init__(self, config: Optional[bt.config] = None) -> None:
        self.config = config or build_config(role="auto")

        # Configure logging if the bittensor config provides a section.
        if getattr(self.config, "logging", None):
            try:
                bt.logging.set_config(config=self.config.logging)
            except Exception:
                # Fall back to default logging without failing init.
                bt.logging.warning("Unable to apply custom logging config; using defaults.")

        self.wallet: Optional[bt.wallet] = None
        self.subtensor: Optional[bt.subtensor] = None
        self.metagraph: Optional[bt.metagraph] = None
        self.axon: Optional[bt.axon] = None

        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.lock = asyncio.Lock()

        self._initialise_chain_objects()

    # --------------------------------------------------------------------- #
    # Lifecycle helpers
    # --------------------------------------------------------------------- #

    def _initialise_chain_objects(self) -> None:
        """Best-effort creation of wallet, subtensor, and metagraph handles."""
        if getattr(self.config, "mock", False):
            bt.logging.info("Running in mock mode; skipping Bittensor initialisation.")
            return

        try:
            self.wallet = bt.wallet(config=self.config)
            self.subtensor = bt.subtensor(config=self.config)
            self.metagraph = self.subtensor.metagraph(self.config.netuid)
            bt.logging.info(
                "Initialised wallet %s on netuid %s.",
                getattr(self.wallet, "hotkey", None),
                getattr(self.config, "netuid", "unknown"),
            )
        except Exception as exc:
            bt.logging.warning(
                "Failed to initialise Bittensor components (continuing in degraded mode): %s",
                exc,
            )
            self.wallet = None
            self.subtensor = None
            self.metagraph = None

    def run_in_background_thread(self) -> None:
        """Spin up the neuron loop in a daemon thread."""
        if self.is_running:
            return
        self.should_exit = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.is_running = True

    def stop_run_thread(self) -> None:
        """Signal the loop to exit and join the background thread."""
        if not self.is_running:
            return
        self.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.is_running = False

    def __enter__(self) -> "BaseNeuron":
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop_run_thread()

    # --------------------------------------------------------------------- #
    # Utilities
    # --------------------------------------------------------------------- #

    @property
    def block(self) -> int:
        """Return the current block height if available, else a monotonic clock fallback."""
        if self.subtensor is None:
            return int(time.time())
        try:
            return self.subtensor.get_current_block()
        except Exception:
            return int(time.time())

    # --------------------------------------------------------------------- #
    # Abstract interface
    # --------------------------------------------------------------------- #

    @abc.abstractmethod
    def run(self) -> None:
        """Main event loop executed when the neuron is serving."""
