"""Settlement mixin: score aggregation and EMA updates (no weight emission)."""

from __future__ import annotations

import bittensor as bt
import numpy as np

from subnet.validator.config import BURN_UID
from subnet.validator.task_ledger import TaskLedger


class SettlementMixin:
    """Handles the settlement phase: consensus and score updates only."""

    async def _run_settlement_phase(
        self, scores: dict[int, float], active_uids: list[int]
    ) -> dict[int, float]:
        """
        Execute the settlement phase and update EMA scores.
        
        Phases:
        1. Compute WTA rewards from scores
        2. Update EMA scores for positive scorers (no burn, no weights)
        
        Args:
            scores: UID -> score mapping from evaluation phase
            active_uids: List of UIDs that participated this round
            
        Returns:
            Final settled rewards dict
        """
        bt.logging.info(
            f"⚖️ [SETTLEMENT] Starting settlement phase with {len(active_uids)} active miners"
        )
        task_ledger: TaskLedger = getattr(self, "_task_ledger", TaskLedger())
        round_id = getattr(self, "get_current_round_id", lambda: None)()
        try:
            task_ledger.write(
                "settlement_start",
                {
                    "round_id": round_id,
                    "active_uids": [int(uid) for uid in (active_uids or [])],
                    "scores": {str(int(uid)): float(score) for uid, score in (scores or {}).items()},
                },
            )
        except Exception:
            pass

        # If we have no on-chain active miners, skip settlement entirely.
        if not active_uids:
            bt.logging.info("⏭️ [SETTLEMENT] Skipping settlement (no active miners)")
            return {}

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Normalize positive scores for EMA updates
        # ─────────────────────────────────────────────────────────────────────
        try:
            n = int(getattr(self.metagraph, "n", len(self.metagraph.uids)))
            candidates = sorted({int(uid) for uid in (active_uids or []) if 0 <= int(uid) < n})
            if not candidates:
                bt.logging.info("⏭️ [SETTLEMENT] Skipping settlement (no eligible miner candidates)")
                return {}

            positive_scores: dict[int, float] = {}
            for uid in candidates:
                score = float(scores.get(uid, 0.0))
                if np.isfinite(score) and score > 0.0:
                    if int(uid) != int(BURN_UID):
                        positive_scores[int(uid)] = score

            if not positive_scores:
                bt.logging.info("⏭️ [SETTLEMENT] Skipping score update (no positive scores)")
                return {}

            score_uids = list(positive_scores.keys())
            score_values = np.array([positive_scores[uid] for uid in score_uids], dtype=np.float32)
            score_sum = float(np.sum(score_values))
            if score_sum <= 0.0:
                bt.logging.info("⏭️ [SETTLEMENT] Skipping score update (no positive score sum)")
                return {}

            score_values = score_values / score_sum
            bt.logging.info("[SETTLEMENT] Scores exclude burn (positive scores only)")
            self.update_scores(score_values, score_uids)
            bt.logging.info(
                f"✓ [SETTLEMENT] Updated rolling scores (uids={len(score_uids)})"
            )

            settled_rewards = {uid: float(val) for uid, val in zip(score_uids, score_values)}
            try:
                settlement_by_round = getattr(self, "_settlement_by_round", None)
                if not isinstance(settlement_by_round, dict):
                    settlement_by_round = {}
                    setattr(self, "_settlement_by_round", settlement_by_round)
                settlement_by_round[str(round_id)] = {
                    "set_weights": False,
                    "pending_emission": True,
                    "non_zero_count": int(len(score_uids)),
                    "scores": {str(int(uid)): float(val) for uid, val in settled_rewards.items()},
                }
            except Exception:
                pass
            try:
                task_ledger.write(
                    "settlement_complete",
                    {
                        "round_id": round_id,
                        "set_weights": False,
                        "pending_emission": True,
                        "non_zero_count": int(len(score_uids)),
                        "scores": {str(int(uid)): float(val) for uid, val in settled_rewards.items()},
                    },
                )
            except Exception:
                pass

            return settled_rewards

        except Exception as e:
            bt.logging.error(f"✗ [SETTLEMENT] Failed to finalize EMA scores: {e}")
            return {}

    def get_settlement_stats(self) -> dict:
        """Get current settlement configuration stats."""
        return {
            "burn_uid": BURN_UID,
        }
