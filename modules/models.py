"""
Shared benchmark dataclasses for AlphaCore.

These models intentionally remain lightweight so that both the validator and
miner stacks can share them without pulling in the full Terraform pipeline.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ACPolicy:
    """
    Declarative policy hints bundled with a task.
    """

    description: str = ""
    max_cost: str = "low"
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyPlan:
    """
    Simple description of how the validator will verify a submission.
    """

    kind: str = "noop"
    steps: List[str] = field(default_factory=list)


@dataclass
class ACTaskSpec:
    """
    Canonical task payload broadcast to miners.
    """

    task_id: str
    provider: str
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)
    policy: ACPolicy = field(default_factory=ACPolicy)
    verify_plan: VerifyPlan = field(default_factory=VerifyPlan)
    cost_tier: str = "low"
    prompt: Optional[str] = None
    verify_fn: Optional[Any] = None


@dataclass
class ACResult:
    """
    Envelope returned by a miner to describe what was executed.
    """

    task_id: str
    status: str
    bundle_dir: Optional[str] = None
    resource_identifiers: Dict[str, Any] = field(default_factory=dict)
    notes: Optional[str] = None


@dataclass
class ACEvidence:
    """
    Optional metadata describing where the validator can find artefacts.
    """

    task_id: str
    bundle_dir: Optional[str] = None
    attachments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ACScore:
    """
    Normalised score emitted by the evaluator.
    """

    task_id: str
    pass_fail: int = 0
    quality: float = 0.0
    timeliness: float = 0.0
    policy_adherence: float = 0.0


@dataclass
class Invariant:
    """
    A single check the validator will perform against the refreshed Terraform state.

    For fields requiring flexible matching (e.g., globally unique names),
    use comparison_rule to specify how to validate the field value.

    Example:
      resource_type = "google_compute_instance"
      match = {
        "values.name": "minimal-vm",
        "values.zone": "us-central1-a",
      }
      comparison_rule = {"values.name": "exact_match"}  # Optional, defaults to exact_match

    Comparison rules:
      - exact_match: Field must equal the value exactly
      - starts_with: Field must start with the value
      - ends_with: Field must end with the value
    """

    resource_type: str
    match: Dict[str, Any]
    comparison_rule: Dict[str, str] = field(default_factory=dict)  # field -> rule (exact_match, starts_with, ends_with)


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
    metadata: Optional[Dict[str, Any]] = None

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
        if self.metadata:
            payload["metadata"] = self.metadata
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
