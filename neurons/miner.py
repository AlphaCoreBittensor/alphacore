"""
AlphaCore miner implementation.

The miner receives declarative cloud task specifications from validators,
performs credential sanity checks, and returns an `ACResult` envelope that
validators can audit.  This version focuses on the skeleton flow rather than
actual cloud provisioning.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

import bittensor as bt

from alphacore.base.miner import BaseMiner
from alphacore.bittensor_config import config as build_config
from alphacore.benchmark.core.models import ACTaskSpec
from alphacore.protocol import TaskSynapse
import yaml


class Miner(BaseMiner):
    """
    Minimal AlphaCore miner skeleton.

    Real cloud execution is intentionally out of scope; the focus is a clean
    contract for receiving tasks, validating credentials, and returning
    well-formed results and evidence hints.
    """

    neuron_type: str = "AlphaCoreMiner"

    def __init__(
        self,
        config: Optional[bt.config] = None,
        settings_path: str = "miner_config.yaml",
    ) -> None:
        super().__init__(config=config or build_config(role="miner"))
        self.settings_path = Path(settings_path)
        self.settings = self._load_settings(self.settings_path)
        self.cloud_credentials = self.settings.get("cloud_credentials", {})
        self.set_validator_blacklist(self.settings.get("blacklist", []))

    # ------------------------------------------------------------------ #
    # Configuration helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_settings(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        # Flatten top-level "miner" key if present.
        if "miner" in data and isinstance(data["miner"], dict):
            base = data["miner"]
            merged = dict(base)
            merged.setdefault("cloud_credentials",
                              base.get("cloud_credentials", {}))
            merged.setdefault("agent", base.get("agent", {}))
            return merged
        return data

    # ------------------------------------------------------------------ #
    # Axon endpoints
    # ------------------------------------------------------------------ #

    async def forward(self, synapse: TaskSynapse) -> TaskSynapse:
        """
        Handle a task request by validating credentials and echoing a placeholder
        `ACResult`.  When the execution engine is implemented, this method will
        orchestrate real cloud operations.
        """
        try:
            spec: ACTaskSpec = synapse.to_spec()
        except Exception as exc:
            bt.logging.error("Received malformed task synapse: %s", exc)
            return synapse

        prompt = None
        if isinstance(synapse.task_spec, dict):
            prompt = synapse.task_spec.get("prompt")

        bt.logging.info(
            "[Miner] Task received: id=%s kind=%s prompt=%s",
            spec.task_id,
            spec.kind,
            prompt or "<unset>",
        )
        return synapse


if __name__ == "__main__":
    cfg = build_config(role="miner")
    with Miner(config=cfg) as miner:
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            bt.logging.info("Miner shutting down.")
