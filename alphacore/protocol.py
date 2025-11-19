"""
Protocol definitions shared between AlphaCore validators and miners.

The classes below wrap Bittensor Synapses and provide helper utilities for
serialising AlphaCore task artefacts (task specs, results, evidence, scores).
The goal is to keep payloads self-descriptive while remaining lightweight
enough for rapid iteration during subnet development.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from bittensor import Synapse
from pydantic import Field

from alphacore.benchmark.core.models import (
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

    Validators populate `task_spec` with the declarative description of the
    cloud action to execute.  Miners respond by filling `result_summary` with
    an `ACResult` payload (serialised to a dict).
    """

    version: str = Field(default="alpha-core.v1")
    task_id: str
    task_spec: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None

    # Miner response fields
    result_summary: Dict[str, Any] = Field(default_factory=dict)
    evidence_hint: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "allow"
        arbitrary_types_allowed = True

    # Helper API -------------------------------------------------------- #

    @classmethod
    def from_spec(cls, spec: ACTaskSpec, validator_hotkey: Optional[str] = None) -> "TaskSynapse":
        payload = asdict(spec)
        payload.pop("verify_fn", None)  # functions are not serialisable
        return cls(
            task_id=spec.task_id,
            task_spec=payload,
            validator_hotkey=validator_hotkey,
        )

    def to_spec(self) -> ACTaskSpec:
        data = dict(self.task_spec or {})
        if not data:
            raise ValueError("Task synapse missing task_spec payload.")

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
