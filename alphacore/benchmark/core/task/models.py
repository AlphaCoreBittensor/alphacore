# ac_tasks/core.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
import uuid
import secrets
import json

from alphacore.benchmark.core.models import (
    ACScore,
)  # re-export for evaluator compatibility


@dataclass
class Invariant:
    """
    A single check the validator will perform against the refreshed Terraform state.
    Example:
      resource_type = "google_compute_instance"
      match = {
        "values.name": "minimal-vm",
        "values.zone": "us-central1-a",
      }
    """

    resource_type: str
    match: Dict[str, Any]


@dataclass
class TaskSpec:
    """
    Canonical description of a task. Natural language is derived from this;
    the validator uses only this spec (plus engine/provider metadata).
    """

    version: str
    task_id: str
    nonce: str
    kind: str
    invariants: List[Invariant]
    prompt: Optional[str] = None

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def new_nonce() -> str:
        # 16 hex chars (8 bytes) is fine for now
        return secrets.token_hex(8)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "version": self.version,
            "task_id": self.task_id,
            "nonce": self.nonce,
            "kind": self.kind,
            "invariants": [asdict(inv) for inv in self.invariants],
        }
        if self.prompt:
            payload["prompt"] = self.prompt
        return payload

    def to_json(self, **json_kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **json_kwargs)


@dataclass
class TerraformTask:
    """
    A task that must be fulfilled via Terraform.

    - engine: always "terraform" for now.
    - provider: e.g., "gcp", "aws", "azure".
    - validator_sa: email of the SA the miner must grant read access to.
    - spec: TaskSpec with invariants (what to look for in refreshed state).
    """

    engine: str
    provider: str
    validator_sa: str
    spec: TaskSpec
    instructions: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        spec_dict = self.spec.to_dict()
        prompt = spec_dict.get("prompt") or self.instructions
        if prompt:
            spec_dict["prompt"] = prompt
        payload = {
            "engine": self.engine,
            "provider": self.provider,
            "validator_sa": self.validator_sa,
            "task": spec_dict,
            "submit_requirements": {
                "code": ".",
                "state": "terraform.tfstate",
                "package_format": "archive",
            },
        }
        return payload

    def to_json(self, **json_kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **json_kwargs)
