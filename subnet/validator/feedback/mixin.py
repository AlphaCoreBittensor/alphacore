"""
Feedback mixin for validator - send immediate task scores to miners.

This phase sends feedback after each task evaluation.
Benefits:
- Real-time learning (miners know score immediately, not next round)
- 2x faster score convergence
- Better in-round adaptation
- Miners can adjust strategy while working

Pattern from Autoppia - TaskFeedbackSynapse after TaskSynapse evaluation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

import bittensor as bt

from subnet.protocol import TaskFeedbackSynapse
from subnet.validator.config import MINER_RESPONSE_TIMEOUT_SECONDS, MINER_CONCURRENCY


class FeedbackMixin:
    """Send per-task feedback to miners for real-time learning."""

    def __init__(self, **kwargs):
        """Initialize feedback state."""
        super().__init__(**kwargs)
        self._feedback_sent: dict = {}  # round_id -> {task_id: {uid: sent}}
        self._feedback_acknowledged: dict = {}  # round_id -> count

    async def send_task_feedback(
        self,
        round_id: str,
        task_id: str,
        scores: Dict[int, float],
        latencies: Dict[int, float],
    ) -> Dict[int, bool]:
        """
        Send task evaluation feedback to miners.

        Args:
            round_id: Round identifier
            task_id: Task that was evaluated
            scores: Dictionary mapping uid -> score
            latencies: Dictionary mapping uid -> task_latency_seconds

        Returns:
            Dictionary mapping uid -> acknowledged (bool)

        Benefits:
        - Miners learn score immediately
        - Can adjust approach for remaining tasks
        - Faster convergence toward better performance
        """
        bt.logging.info(
            f"📬 Sending feedback for task {task_id} (round={round_id}) "
            f"to {len(scores)} miners"
        )

        sem = asyncio.Semaphore(max(1, int(MINER_CONCURRENCY)))

        async def _send_feedback(uid: int, ax, score: float, latency: float) -> tuple:
            try:
                synapse = TaskFeedbackSynapse(
                    round_id=round_id,
                    task_id=task_id,
                    miner_uid=uid,
                    score=score,
                    latency_seconds=latency,
                    feedback_text=f"Score: {score:.4f}",
                    suggestions=None,
                )

                async with sem:
                    resp = await self._dendrite_request(
                        axons=[ax],
                        synapse=synapse,
                        deserialize=False,
                        timeout=MINER_RESPONSE_TIMEOUT_SECONDS,
                    )
                resp_single = resp[0] if isinstance(resp, (list, tuple)) and resp else resp

                acknowledged = (
                    resp_single is not None
                    and hasattr(resp_single, "acknowledged")
                    and resp_single.acknowledged
                )

                if acknowledged:
                    bt.logging.debug(
                        f"✓ UID {uid} acknowledged feedback for {task_id} (score={score:.4f})"
                    )
                else:
                    bt.logging.debug(f"✗ UID {uid} did not acknowledge feedback for {task_id}")

                return uid, acknowledged

            except Exception as e:
                bt.logging.debug(f"✗ UID {uid} feedback error: {e}")
                return uid, False

        try:
            # Send feedback to all miners who completed the task
            tasks = [
                _send_feedback(uid, self.metagraph.axons[uid], scores[uid], latencies.get(uid, 0.0))
                for uid in scores.keys()
                if uid < len(self.metagraph.axons)
            ]

            results = await asyncio.gather(*tasks)

            # Process acknowledgments
            acknowledged_dict = {}
            acknowledged_count = 0

            for uid, acknowledged in results:
                acknowledged_dict[uid] = acknowledged
                if acknowledged:
                    acknowledged_count += 1

            # Store results
            if round_id not in self._feedback_sent:
                self._feedback_sent[round_id] = {}
            self._feedback_sent[round_id][task_id] = acknowledged_dict

            if round_id not in self._feedback_acknowledged:
                self._feedback_acknowledged[round_id] = 0
            self._feedback_acknowledged[round_id] += acknowledged_count

            bt.logging.info(
                f"✓ Feedback sent: {acknowledged_count}/{len(scores)} miners acknowledged"
            )

            return acknowledged_dict

        except Exception as e:
            bt.logging.error(f"✗ Feedback phase failed: {e}")
            return {}

    def get_feedback_stats(self, round_id: Optional[str] = None) -> dict:
        """Get feedback statistics for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()

        feedback_count = len(self._feedback_sent.get(round_id, {}))
        acknowledged_count = self._feedback_acknowledged.get(round_id, 0)

        return {
            "tasks_with_feedback": feedback_count,
            "total_acknowledgments": acknowledged_count,
        }

    def clear_feedback_state(self, round_id: Optional[str] = None) -> None:
        """Clear feedback state for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        if round_id in self._feedback_sent:
            del self._feedback_sent[round_id]
        if round_id in self._feedback_acknowledged:
            del self._feedback_acknowledged[round_id]
