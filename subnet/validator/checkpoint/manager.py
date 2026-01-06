"""
Checkpoint system for saving and resuming validator rounds.

Enables crash recovery and round state persistence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Optional

import bittensor as bt

from modules.models import ACTaskSpec
from subnet.validator.round_manager import RoundState


class CheckpointManager:
    """Manage round state checkpoints."""

    def __init__(self, checkpoint_dir: Optional[Path] = None):
        """
        Initialize checkpoint manager.

        Args:
            checkpoint_dir: Directory to store checkpoints. Default: <repo>/tasks
        """
        if checkpoint_dir is None:
            checkpoint_dir = Path(__file__).resolve().parents[3] / "tasks"

        self.checkpoint_dir = checkpoint_dir
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def save_round_state(self, round_state: RoundState) -> Path:
        """
        Save round state to checkpoint.

        Args:
            round_state: RoundState to save

        Returns:
            Path to saved checkpoint
        """
        round_dir = self.checkpoint_dir / round_state.round_id
        round_dir.mkdir(parents=True, exist_ok=True)

        checkpoint_path = round_dir / "validator_state.json"

        checkpoint_data = {
            "round_id": round_state.round_id,
            "phase": round_state.phase.value,
            "start_block": round_state.start_block,
            "end_block": round_state.end_block,
            "started_at": round_state.started_at.isoformat(),
            "task_count": round_state.task_count,
            "successful_responses": round_state.successful_responses,
            "error_count": round_state.error_count,
        }

        with open(checkpoint_path, "w") as f:
            json.dump(checkpoint_data, f, indent=2)

        bt.logging.debug(f"💾 Saved checkpoint: {checkpoint_path}")

        return checkpoint_path

    def save_tasks(self, round_id: str, tasks: list[ACTaskSpec]) -> Path:
        """
        Save tasks for a round.

        Args:
            round_id: Round identifier
            tasks: List of tasks to save

        Returns:
            Path to saved tasks file
        """
        round_dir = self.checkpoint_dir / round_id
        round_dir.mkdir(parents=True, exist_ok=True)

        tasks_path = round_dir / "tasks.json"

        tasks_data = []
        for task_idx, task in enumerate(tasks):
            if task is None:
                continue
            if is_dataclass(task):
                tasks_data.append(asdict(task))
            elif isinstance(task, dict):
                tasks_data.append(task)
            else:
                if task_idx < 3:  # Only log first few to avoid spam
                    bt.logging.warning(f"[CHECKPOINT] tasks param type: {type(tasks)}, tasks={tasks[:50] if isinstance(tasks, (list,str)) else tasks}")
                bt.logging.warning(f"Skipping task - not dataclass or dict: {type(task)}")
                continue

        with open(tasks_path, "w") as f:
            json.dump(tasks_data, f, indent=2, default=str)

        bt.logging.debug(f"💾 Saved {len(tasks_data)} tasks: {tasks_path}")

        return tasks_path

    def save_scores(self, round_id: str, scores: dict) -> Path:
        """
        Save evaluation scores for a round.

        Args:
            round_id: Round identifier
            scores: Dictionary mapping uid -> score

        Returns:
            Path to saved scores file
        """
        round_dir = self.checkpoint_dir / round_id
        round_dir.mkdir(parents=True, exist_ok=True)

        scores_path = round_dir / "scores.json"

        scores_data = {str(uid): float(score) for uid, score in scores.items()}

        with open(scores_path, "w") as f:
            json.dump(scores_data, f, indent=2)

        bt.logging.debug(f"💾 Saved scores: {scores_path}")

        return scores_path

    def load_round_state(self, round_id: str) -> Optional[RoundState]:
        """
        Load round state from checkpoint.

        Args:
            round_id: Round identifier

        Returns:
            RoundState if exists, None otherwise
        """
        checkpoint_path = self.checkpoint_dir / round_id / "validator_state.json"

        if not checkpoint_path.exists():
            return None

        try:
            with open(checkpoint_path, "r") as f:
                data = json.load(f)

            # Reconstruct RoundState
            from datetime import datetime

            from subnet.validator.round_manager import RoundPhase

            round_state = RoundState(
                round_id=data["round_id"],
                phase=RoundPhase(data.get("phase", "idle")),
                start_block=data["start_block"],
                end_block=data["end_block"],
                started_at=datetime.fromisoformat(data["started_at"]),
                task_count=data.get("task_count", 0),
                successful_responses=data.get("successful_responses", 0),
                error_count=data.get("error_count", 0),
            )

            bt.logging.debug(f"✓ Loaded checkpoint: {checkpoint_path}")

            return round_state

        except Exception as e:
            bt.logging.error(f"✗ Failed to load checkpoint {round_id}: {e}")
            return None

    def round_exists(self, round_id: str) -> bool:
        """Check if checkpoint for round exists."""
        return (self.checkpoint_dir / round_id / "validator_state.json").exists()

    def get_last_round_id(self) -> Optional[str]:
        """Get the most recently saved round ID."""
        try:
            rounds = sorted(
                [d.name for d in self.checkpoint_dir.iterdir() if d.is_dir()],
                reverse=True,
            )
            return rounds[0] if rounds else None
        except Exception:
            return None
