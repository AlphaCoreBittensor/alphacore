"""
Protocol definitions shared between AlphaCore validators and miners.

The classes below wrap Bittensor Synapses and provide helper utilities for
serialising AlphaCore task artefacts (task specs, results, evidence, scores).
The goal is to keep payloads self-descriptive while remaining lightweight
enough for rapid iteration during subnet development.

Enhanced with handshake and feedback synapses for better reliability.
"""

from __future__ import annotations

from dataclasses import asdict
import base64
import hashlib
from typing import Any, Dict, List, Optional

from bittensor import Synapse
from pydantic import Field

from modules.models import (
    ACEvidence,
    ACPolicy,
    ACResult,
    ACTaskSpec,
    VerifyPlan,
)


def _policy_from_dict(data: Dict[str, Any]) -> ACPolicy:
    return ACPolicy(**data) if data is not None else ACPolicy()


def _verify_plan_from_dict(data: Dict[str, Any]) -> VerifyPlan:
    return VerifyPlan(**data) if data is not None else VerifyPlan(kind="noop", steps=[])


def _to_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"Unsupported type for serialisation: {type(obj)}")


class TaskSynapse(Synapse):
    """
    Task broadcast from validator to miner.

    Miners should receive only a natural-language prompt and a task_id.
    The validator keeps the full internal task spec (invariants, policies, etc.)
    private and uses it during post-validation.

    Miners respond by populating:
      - `result_summary`: an `ACResult` payload (serialised to a dict)
      - `evidence_hint`: an optional `ACEvidence` payload (serialised to a dict)
      - `workspace_zip_b64`: optional base64-encoded ZIP of the workspace/artifacts

    The ZIP fields are primarily for local wiring/smoke tests and for passing
    artifacts to an external validation service. Keep payloads small.
    """

    version: str = Field(default="alpha-core.v1")
    task_id: str
    # Natural-language prompt shown to the miner. This is the only task input miners need.
    prompt: Optional[str] = None
    task_spec: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None

    # Miner response fields
    result_summary: Dict[str, Any] = Field(default_factory=dict)
    evidence_hint: Dict[str, Any] = Field(default_factory=dict)

    # Optional artifact return (base64 ZIP)
    workspace_zip_b64: Optional[str] = None
    workspace_zip_filename: Optional[str] = None
    workspace_zip_sha256: Optional[str] = None
    workspace_zip_size_bytes: Optional[int] = None

    class Config:
        extra = "allow"
        arbitrary_types_allowed = True

    # Helper API -------------------------------------------------------- #

    @classmethod
    def from_spec(cls, spec: ACTaskSpec) -> "TaskSynapse":
        # Do not leak the full internal task spec (invariants, policies, etc.) to miners.
        # Miners should receive only `task_id` and a natural-language prompt.
        prompt = (spec.prompt or "").strip() if isinstance(spec.prompt, str) else ""
        payload = {"task_id": spec.task_id, "prompt": prompt}
        return cls(task_id=spec.task_id, prompt=prompt, task_spec=payload)

    def to_spec(self) -> ACTaskSpec:
        # Backwards-compatibility: some miners call `to_spec()` for bookkeeping.
        # If the validator sent a prompt-only payload, synthesize a minimal ACTaskSpec.
        data = dict(self.task_spec or {})
        if not data or ("provider" not in data and "kind" not in data):
            prompt = self.prompt or (data.get("prompt") if isinstance(data, dict) else None)
            return ACTaskSpec(
                task_id=self.task_id,
                provider="prompt_only",
                kind="prompt_only",
                params={},
                policy=ACPolicy(description="", max_cost="low", constraints={}),
                verify_plan=VerifyPlan(kind="noop", steps=[]),
                cost_tier="low",
                prompt=prompt,
                verify_fn=None,
            )

        policy_dict = data.get("policy", {})
        verify_plan_dict = data.get("verify_plan", {})

        data["policy"] = _policy_from_dict(policy_dict)
        data["verify_plan"] = _verify_plan_from_dict(verify_plan_dict)
        data.setdefault("cost_tier", "low")
        data.setdefault("verify_fn", None)

        return ACTaskSpec(**data)

    def attach_result(self, result: ACResult, evidence: Optional[ACEvidence] = None) -> None:
        self.result_summary = _to_dict(result)
        if evidence:
            self.evidence_hint = _to_dict(evidence)

    def attach_workspace_zip_bytes(self, zip_bytes: bytes, filename: str = "workspace.zip") -> None:
        """Attach a ZIP blob (bytes) to the synapse as base64 + metadata."""
        if not isinstance(zip_bytes, (bytes, bytearray)):
            raise TypeError("zip_bytes must be bytes")

        raw = bytes(zip_bytes)
        self.workspace_zip_b64 = base64.b64encode(raw).decode("utf-8")
        self.workspace_zip_filename = filename
        self.workspace_zip_size_bytes = len(raw)
        self.workspace_zip_sha256 = hashlib.sha256(raw).hexdigest()


class TaskCleanupSynapse(Synapse):
    """
    Cleanup / post-validation hook from validator to miner.

    Intended flow:
      1) Validator dispatches `TaskSynapse` with an `ACTaskSpec`
      2) Miner returns artifacts (optionally as a ZIP)
      3) Validator validates/scores and then sends `TaskCleanupSynapse` so the
         miner can free temporary resources.
    """

    version: str = Field(default="alpha-core.v1")
    task_id: str

    # Validator payload (serialised ValidationSubmitResponse or equivalent)
    validation_response: Dict[str, Any] = Field(default_factory=dict)

    # Miner response fields
    acknowledged: bool = Field(default=False)
    cleanup_ok: bool = Field(default=False)
    error_message: Optional[str] = Field(default=None)

    class Config:
        extra = "allow"
        arbitrary_types_allowed = True


class StartRoundSynapse(Synapse):
    """
    Handshake synapse from validator to miner to verify liveness.

    Validators send this to verify the miner is online and get metadata.
    Miners respond with their capabilities and status.

    Benefits:
    - Skip offline miners before dispatching tasks
    - Get miner metadata (version, capabilities)
    - Verify communication channel is working
    """

    version: str = Field(default="alpha-core.v1")
    round_id: str = Field(default="")  # Unique round identifier
    timestamp: int = Field(default=0)  # Unix timestamp when round started
    validator_version: str = Field(default="")  # Validator version metadata

    # Miner response fields
    miner_version: str = Field(default="")  # Miner version
    is_ready: bool = Field(default=False)  # Miner ready to process tasks
    available_capacity: int = Field(default=0)  # Number of tasks miner can handle
    error_message: Optional[str] = Field(default=None)  # Error if not ready

    class Config:
        extra = "allow"
        arbitrary_types_allowed = True


class TaskFeedbackSynapse(Synapse):
    """
    Feedback synapse from validator to miner after task evaluation.

    Validators send this after evaluating a task to give immediate feedback.
    Miners use this to adjust their strategies in real-time.

    Benefits:
    - Real-time learning (miners know score immediately)
    - 2x faster score convergence
    - Better miner adaptation during round
    """

    version: str = Field(default="alpha-core.v1")
    round_id: str = Field(default="")  # Round this feedback is for
    task_id: str = Field(default="")  # Task this feedback is for
    miner_uid: int = Field(default=0)  # UID of miner receiving feedback

    # Feedback fields
    score: float = Field(default=0.0)  # Score 0.0 to 1.0
    feedback_text: Optional[str] = Field(default=None)  # Human-readable feedback
    suggestions: Optional[List[str]] = Field(default=None)  # Suggestions for improvement
    latency_seconds: float = Field(default=0.0)  # How long task took

    # Miner acknowledges receipt
    acknowledged: bool = Field(default=False)

    class Config:
        extra = "allow"
        arbitrary_types_allowed = True
