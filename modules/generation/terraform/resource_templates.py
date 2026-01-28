"""
Generic resource template plumbing for Terraform task generation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from modules.models import Invariant


@dataclass
class TemplateContext:
    """
    Shared state passed to each resource template builder.

    - rng: deterministic random instance scoped to the resource.
    - task_id / nonce: identifiers for deterministic naming.
    - shared: capability map populated by previously instantiated templates.
    """

    rng: random.Random
    task_id: str
    nonce: str
    shared: Dict[str, Dict[str, Any]]
    # Validator identity (often a service account email) used for access-control templates.
    validator_sa: str = ""


@dataclass
class ResourceInstance:
    """
    Concrete output of a resource template.

    - invariants: validator checks contributed by this template.
    - prompt_hints: natural language cues fed into the LLM instruction generator.
    - shared_values: capability payloads exposed for downstream templates.
    """

    invariants: List[Invariant]
    prompt_hints: List[str] = field(default_factory=list)
    shared_values: Dict[str, Dict[str, Any]] = field(default_factory=dict)


BuilderFn = Callable[[TemplateContext], ResourceInstance]


def pick_naming_rule(template: "ResourceTemplate", rng: random.Random) -> str:
    """
    Pick a random allowed naming rule for a resource template.

    Args:
        template: The resource template
        rng: Random number generator for deterministic selection

    Returns:
        One of: "exact_match", "starts_with", "ends_with"
    """
    if not template.naming_rules:
        return "exact_match"
    return rng.choice(template.naming_rules)


@dataclass(frozen=True)
class ResourceTemplate:
    """
    Declarative description of a Terraform resource building block.

    - key: unique identifier.
    - kind: natural language description used in specs/prompts.
    - provides: capabilities offered to downstream templates (e.g., "network").
    - requires: capabilities that must already exist before instantiation.
    - base_hints: static hints describing the resource's intent.
    - weight: sampling probability tweak for selection algorithms.
    - builder: callback that realises invariants and shared state.
    - naming_rules: allowed comparison rules for name validation (exact_match, starts_with, ends_with).
                    For globally unique resources, use ["starts_with", "ends_with"] only.
    """

    key: str
    kind: str
    provides: Tuple[str, ...]
    builder: BuilderFn
    requires: Tuple[str, ...] = ()
    base_hints: Tuple[str, ...] = ()
    weight: float = 1.0
    naming_rules: Tuple[str, ...] = ("exact_match",)  # Allowed: exact_match, starts_with, ends_with
