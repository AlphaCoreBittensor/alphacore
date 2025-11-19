"""
AlphaCore validator skeleton with task dispatch → evaluation → reward flow.
"""

from __future__ import annotations

import random
import time
from typing import Dict, List, Optional, Tuple

import bittensor as bt
import numpy as np

from alphacore.base.validator import BaseValidator
from alphacore.bittensor_config import config as build_config
from alphacore.benchmark.core.evaluation import Evaluator
from alphacore.benchmark.core.task import TaskGenerationPipeline
from alphacore.benchmark.core.models import ACScore
from alphacore.protocol import TaskSynapse
from alphacore.validator.config import (
    MAX_DISPATCH_PER_ROUND,
    ROUND_CADENCE_SECONDS,
    TASK_TIMEOUT_SECONDS,
)
from alphacore.validator.rewards import compute_rewards
from alphacore.validator.synapse_handlers import send_task_synapse_to_miners


class Validator(BaseValidator):
    neuron_type: str = "AlphaCoreValidator"

    def __init__(
        self,
        config: Optional[bt.config] = None,
    ) -> None:
        super().__init__(config=config or build_config(role="validator"))

        self.pipeline = TaskGenerationPipeline()
        self.evaluator = Evaluator()
        self.round_cadence = ROUND_CADENCE_SECONDS
        self.task_timeout = TASK_TIMEOUT_SECONDS
        self.version = "alpha-core.dev"

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def forward(self) -> None:
        try:
            spec = self.pipeline.generate()
        except RuntimeError as exc:
            bt.logging.warning("Task generation failed: %s", exc)
            await self.sleep(self.round_cadence)
            return

        validator_hotkey = self.wallet.hotkey.ss58_address if self.wallet else None
        task_synapse = TaskSynapse.from_spec(spec, validator_hotkey=validator_hotkey)

        miner_axons = self._gather_miner_axons()
        if not miner_axons:
            bt.logging.info("No miner axons available; skipping task %s.", spec.task_id)
            await self.sleep(self.round_cadence)
            return

        if MAX_DISPATCH_PER_ROUND > 0:
            miner_axons = miner_axons[:MAX_DISPATCH_PER_ROUND]

        uids, axons = zip(*miner_axons)
        uid_sequence = [int(uid) for uid in uids]
        responses = await send_task_synapse_to_miners(
            validator=self,
            miner_axons=list(axons),
            task_synapse=task_synapse,
            timeout=self.task_timeout,
        )

        task_payload = dict(spec.params or {})
        task_payload.setdefault("task_id", spec.task_id)
        task_payload.setdefault("kind", spec.kind)
        if spec.prompt and "prompt" not in task_payload:
            task_payload["prompt"] = spec.prompt

        score_inputs: Dict[int, ACScore] = {}
        for uid, response in zip(uid_sequence, responses):
            if response is None:
                continue
            response_payload = {
                "task_id": getattr(response, "task_id", None),
                "result_summary": getattr(response, "result_summary", {}),
                "notes": getattr(response, "notes", None),
            }
            score_inputs[int(uid)] = self.evaluator.evaluate(task_payload, response_payload)

        if not score_inputs:
            bt.logging.info("No miner responses received for task %s.", spec.task_id)
            await self.sleep(self.round_cadence)
            return

        reward_map = compute_rewards(score_inputs)
        rewards_vector = np.array(
            [float(reward_map.get(uid, 0.0)) for uid in uid_sequence],
            dtype=np.float32,
        )
        self.update_scores(rewards_vector, uid_sequence)

        await self.sleep(self.round_cadence)

    # ------------------------------------------------------------------ #
    # Miner helpers
    # ------------------------------------------------------------------ #

    def _gather_miner_axons(self) -> List[Tuple[int, bt.AxonInfo]]:
        if self.metagraph is None:
            return []

        entries = [
            (uid, self.metagraph.axons[uid])
            for uid in range(self.metagraph.n)
            if self.metagraph.axons[uid] is not None
        ]
        random.shuffle(entries)
        return entries


if __name__ == "__main__":
    cfg = build_config(role="validator")
    with Validator(config=cfg) as validator:
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            bt.logging.info("Validator shutting down.")
