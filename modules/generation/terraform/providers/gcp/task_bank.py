from __future__ import annotations

import importlib
import pkgutil
import random
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from modules.models import Invariant, TaskSpec, TerraformTask
from modules.generation.repository import TaskRepository
from modules.generation.terraform.providers.gcp import compositions
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)

RESOURCE_PACKAGE = "modules.generation.terraform.providers.gcp.resources"

# Module-level cache to avoid repeated filesystem scans
_TEMPLATE_CACHE: Optional[Dict[str, ResourceTemplate]] = None
_CAPABILITY_CACHE: Optional[Dict[str, List[str]]] = None


class GCPDynamicTaskBank:
    """
    Builds Terraform tasks by sampling resource templates and composing them
    into single-resource or multi-resource assignments on demand.

    Respects TaskConfig for resource-level filtering.
    """

    def __init__(
        self,
        min_resources: int = 1,
        max_resources: int = 3,
        families: Sequence[compositions.CompositionFamily] | None = None,
    ) -> None:
        if min_resources < 1:
            raise ValueError("min_resources must be at least 1.")
        if max_resources < min_resources:
            raise ValueError("max_resources must be >= min_resources.")
        self.min_resources = min_resources
        self.max_resources = max_resources
        self._system_random = random.SystemRandom()
        # Use cached templates to avoid repeated filesystem scans
        self._templates = None
        self._capability_map = None
        self.families = list(families or [])

        # Default to an in-memory repository to avoid filesystem side-effects.
        # Callers can override `self._repository` with a persistent TaskRepository.
        self._repository = TaskRepository(db_path=":memory:")

    @property
    def templates(self) -> Dict[str, ResourceTemplate]:
        """Lazy-loaded templates with module-level caching."""
        if self._templates is None:
            global _TEMPLATE_CACHE
            if _TEMPLATE_CACHE is None:
                _TEMPLATE_CACHE = self._load_templates()
            self._templates = _TEMPLATE_CACHE
        return self._templates

    @property
    def capability_map(self) -> Dict[str, List[str]]:
        """Lazy-loaded capability map with module-level caching."""
        if self._capability_map is None:
            global _CAPABILITY_CACHE
            if _CAPABILITY_CACHE is None:
                _CAPABILITY_CACHE = self._build_capability_map()
            self._capability_map = _CAPABILITY_CACHE
        return self._capability_map

    def build_task(self, validator_sa: str) -> TerraformTask:
        task_id = TaskSpec.new_id()
        nonce = TaskSpec.new_nonce()
        seed_material = f"{task_id}:{nonce}"
        selection_rng = random.Random(seed_material)
        selected, resolved, family = self._select_templates(selection_rng)
        order = self._topological_order(selected, resolved)
        invariants, hints = self._realise_templates(order, task_id, nonce, validator_sa)

        resource_kinds = [self.templates[key].kind for key in order]
        kind_label = " + ".join(resource_kinds)
        metadata = {
            "resource_keys": order,
            "resource_kinds": resource_kinds,
            "hints": hints,
            "requires_validator_access": True,
        }
        if family:
            metadata["composition_family"] = family.name

        spec = TaskSpec(
            version="v0",
            task_id=task_id,
            nonce=nonce,
            kind=kind_label,
            invariants=invariants,
            metadata=metadata,
        )
        task = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa=validator_sa,
            spec=spec,
        )

        # Persist best-effort (tests and validators rely on this behavior).
        try:
            if getattr(self, "_repository", None) is not None:
                self._repository.save(task)
        except Exception:
            pass

        return task

    # ------------------------------------------------------------------ #
    # Template discovery
    # ------------------------------------------------------------------ #

    def _load_templates(self) -> Dict[str, ResourceTemplate]:
        templates: Dict[str, ResourceTemplate] = {}
        package = importlib.import_module(RESOURCE_PACKAGE)
        for finder, name, ispkg in pkgutil.walk_packages(
            package.__path__, package.__name__ + "."
        ):
            if ispkg:
                continue
            module = importlib.import_module(name)
            candidates = []
            if hasattr(module, "get_templates"):
                candidates = module.get_templates() or []
            elif hasattr(module, "get_template"):
                template = module.get_template()
                candidates = [template] if template else []
            for template in candidates:
                if template.key in templates:
                    raise RuntimeError(f"Duplicate template key detected: {template.key}")
                templates[template.key] = template
        return templates

    def _build_capability_map(self) -> Dict[str, List[str]]:
        capability_map: Dict[str, List[str]] = {}
        for key, template in self.templates.items():
            for capability in template.provides:
                capability_map.setdefault(capability, []).append(key)
        return capability_map

    # ------------------------------------------------------------------ #
    # Selection + ordering
    # ------------------------------------------------------------------ #

    def _select_templates(
        self, rng: random.Random
    ) -> Tuple[Set[str], Dict[str, Dict[str, str]], compositions.CompositionFamily | None]:
        selected: Set[str] = set()
        resolved_dependencies: Dict[str, Dict[str, str]] = {}
        family = None

        if self.families:
            family = rng.choice(self.families)
            seeds = family.pick_templates(rng)
            for key in seeds:
                self._include_with_dependencies(key, selected, resolved_dependencies, rng)
        else:
            target = rng.randint(self.min_resources, self.max_resources)
            keys = list(self.templates.keys())
            while len(selected) < target:
                key = rng.choice(keys)
                self._include_with_dependencies(key, selected, resolved_dependencies, rng)
        return selected, resolved_dependencies, family

    def _include_with_dependencies(
        self,
        key: str,
        selected: Set[str],
        resolved: Dict[str, Dict[str, str]],
        rng: random.Random,
    ) -> None:
        if key in selected:
            return
        template = self.templates[key]
        dependency_map = resolved.setdefault(key, {})
        for capability in template.requires:
            provider_key = self._select_capability_provider(capability, selected, rng)
            dependency_map[capability] = provider_key
            if provider_key not in selected:
                self._include_with_dependencies(provider_key, selected, resolved, rng)
        selected.add(key)

    def _select_capability_provider(
        self, capability: str, selected: Set[str], rng: random.Random
    ) -> str:
        providers = self.capability_map.get(capability, [])
        if not providers:
            raise RuntimeError(f"No templates provide required capability '{capability}'.")
        chosen = [provider for provider in providers if provider in selected]
        if chosen:
            return chosen[0]
        return rng.choice(providers)

    def _topological_order(
        self, selected: Set[str], resolved: Dict[str, Dict[str, str]]
    ) -> List[str]:
        adjacency: Dict[str, Set[str]] = {key: set() for key in selected}
        indegree: Dict[str, int] = {key: 0 for key in selected}
        for key, dependencies in resolved.items():
            for provider in dependencies.values():
                if provider not in adjacency:
                    adjacency[provider] = set()
                    indegree[provider] = indegree.get(provider, 0)
                adjacency[provider].add(key)
                indegree[key] = indegree.get(key, 0) + 1
        ready = [node for node, degree in indegree.items() if degree == 0]
        order: List[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for neighbor in adjacency.get(node, set()):
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    ready.append(neighbor)
        if len(order) != len(selected):
            raise RuntimeError("Cycle detected while ordering resource templates.")
        return order

    # ------------------------------------------------------------------ #
    # Instantiation
    # ------------------------------------------------------------------ #

    def _realise_templates(
        self, order: List[str], task_id: str, nonce: str, validator_sa: str
    ) -> Tuple[List[Invariant], List[str]]:
        from modules.generation.terraform.resource_templates import pick_naming_rule

        shared_state: Dict[str, Dict[str, Any]] = {}
        invariants: List[Invariant] = []
        hints: List[str] = []
        for key in order:
            template = self.templates[key]
            scoped_seed = f"{task_id}:{nonce}:{key}"
            ctx_rng = random.Random(scoped_seed)
            ctx = TemplateContext(
                rng=ctx_rng,
                task_id=task_id,
                nonce=nonce,
                shared=shared_state,
                validator_sa=validator_sa,
            )
            instance: ResourceInstance = template.builder(ctx)

            # Apply naming rules to invariants based on template configuration
            for invariant in instance.invariants:
                # Pick a naming rule for name fields based on template's allowed rules
                name_field = "values.name"
                if name_field in invariant.match and template.naming_rules:
                    # Use the same RNG for deterministic rule selection
                    rule = pick_naming_rule(template, ctx_rng)
                    if rule != "exact_match":
                        invariant.comparison_rule[name_field] = rule

            invariants.extend(instance.invariants)
            hints.extend(template.base_hints)
            hints.extend(instance.prompt_hints)
            for capability in template.provides:
                provided_values = instance.shared_values.get(capability, {})
                shared_state[capability] = provided_values
        return invariants, hints
