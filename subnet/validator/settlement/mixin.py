"""Settlement mixin: consensus and weight finalization phase."""

from __future__ import annotations

import bittensor as bt
import numpy as np

from subnet.validator.config import BURN_AMOUNT_PERCENTAGE, BURN_UID
from subnet.validator.task_ledger import TaskLedger
from subnet.validator.settlement.rewards import (
    apply_burn_mechanism,
    wta_rewards,
)


class SettlementMixin:
    """Handles the settlement phase: consensus, burning, and weight finalization."""

    async def _run_settlement_phase(
        self, scores: dict[int, float], active_uids: list[int]
    ) -> dict[int, float]:
        """
        Execute the settlement phase with burning mechanism.
        
        Phases:
        1. Compute WTA rewards from scores
        2. Apply burning mechanism (send % to burn UID, rest to winner)
        3. Set weights on chain
        
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
        # This prevents confusing burn/winner logs when running local-only wiring tests.
        if not active_uids:
            bt.logging.info("⏭️ [SETTLEMENT] Skipping settlement (no active miners)")
            return {}

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Compute WTA rewards from scores
        # ─────────────────────────────────────────────────────────────────────
        
        try:
            # Restrict winning to UIDs that were active (handshake targets) this round.
            n = int(getattr(self.metagraph, "n", len(self.metagraph.uids)))
            candidates = sorted({int(uid) for uid in (active_uids or []) if 0 <= int(uid) < n})
            if not candidates:
                bt.logging.info("⏭️ [SETTLEMENT] Skipping settlement (no eligible miner candidates)")
                return {}

            # Winner selection: highest score among candidates.
            winner_uid = max(candidates, key=lambda u: float(scores.get(u, float("-inf"))))
            winner_score = float(scores.get(winner_uid, float("-inf")))
            if not np.isfinite(winner_score) or winner_score <= 0.0:
                bt.logging.info(
                    "⏭️ [SETTLEMENT] Skipping score update (no positive winning score among active miners)"
                )
                return {}

            wta_weights = np.zeros(n, dtype=np.float32)
            wta_weights[int(winner_uid)] = 1.0
            bt.logging.info(f"🏆 [SETTLEMENT] Winner UID {int(winner_uid)} score={winner_score:.4f}")

        except Exception as e:
            bt.logging.error(f"✗ [SETTLEMENT] Failed to compute winner: {e}")
            return {}

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: Apply burning mechanism
        # ─────────────────────────────────────────────────────────────────────

        try:
            burned_weights = apply_burn_mechanism(
                wta_weights,
                burn_uid=BURN_UID,
                burn_percentage=BURN_AMOUNT_PERCENTAGE,
            )

            # Log burning stats
            burn_amount = burned_weights[BURN_UID] if BURN_UID < len(burned_weights) else 0
            winner_uid = int(np.argmax(wta_weights))
            winner_amount = burned_weights[winner_uid] if 0 <= winner_uid < len(burned_weights) else 0

            bt.logging.info(
                f"✓ [SETTLEMENT] Burning applied: "
                f"UID {BURN_UID} gets {burn_amount:.4f}, "
                f"Winner UID {winner_uid} gets {winner_amount:.4f} "
                f"(ratio {BURN_AMOUNT_PERCENTAGE:.1%}:{(1-BURN_AMOUNT_PERCENTAGE):.1%})"
            )
            try:
                task_ledger.write(
                    "settlement_burn_applied",
                    {
                        "round_id": round_id,
                        "burn_uid": int(BURN_UID),
                        "burn_percentage": float(BURN_AMOUNT_PERCENTAGE),
                        "burn_amount": float(burn_amount),
                        "winner_uid": int(winner_uid),
                        "winner_amount": float(winner_amount),
                    },
                )
            except Exception:
                pass

        except Exception as e:
            bt.logging.error(f"✗ [SETTLEMENT] Failed to apply burning: {e}")
            burned_weights = wta_weights

        # ─────────────────────────────────────────────────────────────────────
        # Phase 3: Update rolling scores (weights are emitted separately)
        # ─────────────────────────────────────────────────────────────────────

        try:
            # Normalize weights
            if burned_weights.sum() > 0:
                final_weights = burned_weights / burned_weights.sum()
            else:
                final_weights = burned_weights

            # Ensure we meet chain constraints (min_allowed_weights) without falling back to
            # arbitrary "burn" behavior. If padding is required, it will dilute the burn/winner
            # weights slightly; log it so operators can see why.
            try:
                min_allowed = int(self.subtensor.min_allowed_weights(netuid=self.config.netuid))
            except Exception:
                min_allowed = 0
            if min_allowed > 0:
                non_zero = int(np.count_nonzero(final_weights > 0))
                if non_zero < min_allowed:
                    needed = int(min_allowed - non_zero)
                    n = int(final_weights.shape[0])
                    pad_candidates = [uid for uid in range(n) if final_weights[uid] <= 0]
                    pad = pad_candidates[:needed]
                    eps = 1e-5
                    for uid in pad:
                        final_weights[int(uid)] = eps
                    final_weights = final_weights / float(final_weights.sum())
                    bt.logging.warning(
                        f"[SETTLEMENT] Padded weights to satisfy min_allowed_weights={min_allowed} "
                        f"(added={len(pad)}, epsilon={eps})."
                    )

            # Update rolling scores (EMA). Weight emission happens in a separate window.
            try:
                uids = np.arange(len(final_weights)).tolist()
                self.update_scores(final_weights, uids)
                bt.logging.info("✓ [SETTLEMENT] Updated rolling scores (pending weight emission)")
            except Exception as exc:
                bt.logging.error(f"✗ [SETTLEMENT] Failed to update rolling scores: {exc}")

            non_zero_count = np.count_nonzero(final_weights)
            bt.logging.info(
                f"✓ [SETTLEMENT] Final weights computed ({non_zero_count} non-zero)"
            )

            # Convert to dict for return
            settled_rewards = {
                uid: float(final_weights[uid])
                for uid in range(len(final_weights))
                if final_weights[uid] > 0
            }
            try:
                settlement_by_round = getattr(self, "_settlement_by_round", None)
                if not isinstance(settlement_by_round, dict):
                    settlement_by_round = {}
                    setattr(self, "_settlement_by_round", settlement_by_round)
                settlement_by_round[str(round_id)] = {
                    "set_weights": False,
                    "pending_emission": True,
                    "non_zero_count": int(non_zero_count),
                    "weights": {str(int(uid)): float(w) for uid, w in settled_rewards.items()},
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
                        "non_zero_count": int(non_zero_count),
                        "weights": {str(int(uid)): float(w) for uid, w in settled_rewards.items()},
                    },
                )
            except Exception:
                pass

            return settled_rewards

        except Exception as e:
            bt.logging.error(f"✗ [SETTLEMENT] Failed to finalize weights: {e}")
            return {}

    def get_settlement_stats(self) -> dict:
        """Get current settlement configuration stats."""
        return {
            "burn_uid": BURN_UID,
            "burn_percentage": BURN_AMOUNT_PERCENTAGE,
            "winner_percentage": 1.0 - BURN_AMOUNT_PERCENTAGE,
        }
