"""
Base class for AlphaCore validators.

Validators coordinate task generation, dispatch synapses to miners, and submit
scores back to the Bittensor network.  Concrete validators implement the
`forward` coroutine that queries miners and processes responses.
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterable, Optional

import bittensor as bt
import numpy as np

from alphacore.base.neuron import BaseNeuron
from alphacore.bittensor_config import config as build_config


class BaseValidator(BaseNeuron):
    neuron_type: str = "ValidatorNeuron"

    def __init__(self, config: Optional[bt.config] = None) -> None:
        super().__init__(config=config or build_config(role="validator"))
        self.dendrite: Optional[bt.dendrite] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.scores: np.ndarray = np.zeros(0, dtype=np.float32)

        if self.wallet is not None:
            try:
                self.dendrite = bt.dendrite(wallet=self.wallet)
            except Exception as exc:
                bt.logging.warning(f"Failed to initialise validator dendrite: {exc}")
                self.dendrite = None

        self._refresh_score_buffer()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Default validator loop invoking the async `forward` coroutine."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            while not self.should_exit:
                self.loop.run_until_complete(self.forward())
        finally:
            self.loop.close()
            self.loop = None

    async def forward(self) -> None:  # pragma: no cover - abstract contract
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Score handling
    # ------------------------------------------------------------------ #

    def update_scores(self, rewards: np.ndarray, uids: Iterable[int]) -> None:
        """Persist the latest rewards for the given uids."""
        self._refresh_score_buffer()
        uids = list(uids)
        if len(uids) != len(rewards):
            raise ValueError("Rewards and uids must share the same length.")
        self.scores[uids] = rewards

    def _refresh_score_buffer(self) -> None:
        if self.metagraph is None:
            return
        size = getattr(self.metagraph, "n", len(getattr(self.metagraph, "uids", [])))
        if size <= 0:
            size = 0
        if self.scores.shape != (size,):
            self.scores = np.zeros(size, dtype=np.float32)

    # ------------------------------------------------------------------ #
    # Convenience helpers
    # ------------------------------------------------------------------ #

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def current_block(self) -> int:
        if self.subtensor is None:
            return int(time.time())
        try:
            return self.subtensor.get_current_block()
        except Exception:
            return int(time.time())
