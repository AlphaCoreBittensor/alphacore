"""
Round manager for tracking validator phases and timing.

Implements a state machine for round phases with block-based timing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import bittensor as bt


class RoundPhase(Enum):
    """Phases of a validator round."""

    IDLE = "idle"  # Waiting for next round
    PREPARING = "preparing"  # Initializing round, getting block
    GENERATION = "generation"  # Generating tasks
    HANDSHAKE = "handshake"  # Dispatching tasks to miners
    TASK_EXECUTION = "task_execution"  # Waiting for miners to complete tasks
    CONSENSUS = "consensus"  # Evaluating responses and computing scores
    FINALIZING = "finalizing"  # Setting weights and cleaning up
    COMPLETE = "complete"  # Round finished


@dataclass
class RoundState:
    """State of a validator round."""

    round_id: str
    phase: RoundPhase = RoundPhase.IDLE
    start_block: int = 0
    end_block: int = 0
    start_epoch: int = 0  # Epoch-based tracking
    end_epoch: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    phase_start_times: dict[RoundPhase, datetime] = field(default_factory=dict)
    task_count: int = 0
    successful_responses: int = 0
    error_count: int = 0

    def duration_seconds(self) -> float:
        """Get total round duration in seconds."""
        return (datetime.now() - self.started_at).total_seconds()

    def phase_duration_seconds(self) -> float:
        """Get duration of current phase in seconds."""
        if self.phase not in self.phase_start_times:
            return 0
        return (datetime.now() - self.phase_start_times[self.phase]).total_seconds()


class RoundManager:
    """Manages validator round phases and timing with epoch support."""

    def __init__(self, round_duration_blocks: int = 100, tempo: int = 360):
        """
        Initialize round manager.

        Args:
            round_duration_blocks: Number of blocks per round
            tempo: Blocks per epoch (default 360 for Bittensor)
        """
        self.round_duration_blocks = round_duration_blocks
        self.tempo = tempo
        self.current_round: Optional[RoundState] = None
        self.previous_rounds: dict[str, RoundState] = {}

    def start_round(self, round_id: str, current_block: int) -> RoundState:
        """
        Start a new round with epoch tracking.

        Args:
            round_id: Unique round identifier
            current_block: Current blockchain block number

        Returns:
            RoundState for the new round
        """
        start_epoch = self.get_current_epoch(current_block)
        end_epoch = self.get_current_epoch(current_block + self.round_duration_blocks)

        self.current_round = RoundState(
            round_id=round_id,
            phase=RoundPhase.PREPARING,
            start_block=current_block,
            end_block=current_block + self.round_duration_blocks,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
        )
        self.current_round.phase_start_times[RoundPhase.PREPARING] = datetime.now()

        bt.logging.info(
            f"🔄 Started round {round_id} | Block {current_block} "
            f"→ {current_block + self.round_duration_blocks} | "
            f"Epoch {start_epoch} → {end_epoch}"
        )

        return self.current_round

    def get_current_epoch(self, block: int) -> int:
        """
        Calculate current epoch from block number.

        Args:
            block: Block number

        Returns:
            Epoch number
        """
        return block // self.tempo

    def get_blocks_until_next_epoch(self, current_block: int) -> int:
        """
        Calculate blocks remaining until next epoch.

        Args:
            current_block: Current block number

        Returns:
            Number of blocks until next epoch
        """
        current_epoch = self.get_current_epoch(current_block)
        next_epoch_start = (current_epoch + 1) * self.tempo
        return next_epoch_start - current_block

    def should_start_new_round(self, current_block: int) -> bool:
        """
        Determine if a new round should start based on epoch alignment.

        Args:
            current_block: Current block number

        Returns:
            True if new round should start
        """
        if self.current_round is not None:
            return False  # Round already in progress
        # Slot window gating is handled upstream; always allow a start here.
        return True

    def transition_phase(self, new_phase: RoundPhase) -> None:
        """
        Transition to a new phase.

        Args:
            new_phase: Target phase
        """
        if self.current_round is None:
            bt.logging.error("Cannot transition phase: no active round")
            return

        old_phase = self.current_round.phase
        self.current_round.phase = new_phase
        self.current_round.phase_start_times[new_phase] = datetime.now()

        duration = self.current_round.phase_duration_seconds()
        bt.logging.debug(f"→ {old_phase.value} → {new_phase.value} (duration: {duration:.2f}s)")

    def finish_round(self) -> Optional[RoundState]:
        """
        Finish the current round.

        Returns:
            The completed RoundState, or None if no active round
        """
        if self.current_round is None:
            return None

        self.current_round.phase = RoundPhase.COMPLETE
        self.previous_rounds[self.current_round.round_id] = self.current_round

        duration = self.current_round.duration_seconds()
        bt.logging.info(
            f"✓ Completed round {self.current_round.round_id} in {duration:.2f}s | "
            f"Phase: {self.current_round.phase.value}"
        )

        completed = self.current_round
        self.current_round = None
        return completed

    def get_round_status(self) -> dict:
        """Get status of current round."""
        if self.current_round is None:
            return {"status": "idle"}

        return {
            "round_id": self.current_round.round_id,
            "phase": self.current_round.phase.value,
            "duration": f"{self.current_round.duration_seconds():.2f}s",
            "task_count": self.current_round.task_count,
            "successful_responses": self.current_round.successful_responses,
            "error_count": self.current_round.error_count,
        }
