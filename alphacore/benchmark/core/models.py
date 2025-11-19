"""
Shared benchmark dataclasses for AlphaCore.

These models intentionally remain lightweight so that both the validator and
miner stacks can share them without pulling in the full Terraform pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
