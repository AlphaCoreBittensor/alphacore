"""
Reward calculation helpers for AlphaCore validators.

The current placeholder simply assigns zero rewards so that downstream
plumbing can be exercised without committing to a scoring policy.
"""

from __future__ import annotations

from typing import Dict

from alphacore.benchmark.core.models import ACScore


def compute_rewards(scores: Dict[int, ACScore]) -> Dict[int, float]:
    """
    Translate score envelopes into floating-point rewards.
    """
    return {uid: 0.0 for uid in scores}
