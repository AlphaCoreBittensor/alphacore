"""
Task generation pipeline that bridges the Terraform registry with ACTaskSpecs.
"""

from __future__ import annotations

import os
import random
from typing import Dict, List, Optional

from modules.models import ACPolicy, ACTaskSpec, VerifyPlan
from modules.generation.instructions import TaskInstructionGenerator
from modules.generation.terraform.registry import terraform_task_registry
from modules.generation.yaml_config import get_yaml_config

DEFAULT_VALIDATOR_SA = os.getenv("ALPHACORE_VALIDATOR_SA", "validator@example.com")


class TaskGenerationPipeline:
    """
    High-level orchestrator used by validators to generate task specs.
    """

    def __init__(
        self,
        validator_sa: Optional[str] = None,
        provider_weights: Optional[Dict[str, float]] = None,
        instruction_generator: Optional[TaskInstructionGenerator] = None,
    ) -> None:
        self.validator_sa = validator_sa or DEFAULT_VALIDATOR_SA
        self.registry = terraform_task_registry
        providers = self.registry.get_all_providers()
        if not providers:
            raise RuntimeError("No Terraform task providers registered.")
        self.provider_weights = self._normalise_weights(provider_weights, providers)
        if instruction_generator is not None:
            self.instruction_generator = instruction_generator
        else:
            llm_config = get_yaml_config().settings.llm
            self.instruction_generator = TaskInstructionGenerator(
                model=llm_config.model,
                temperature=llm_config.temperature,
                enable_llm=llm_config.enabled,
                fallback_on_failure=llm_config.fallback_on_failure,
                llm_retries=llm_config.retries,
            )

    def generate(self) -> ACTaskSpec:
        """
        Select a provider, build a Terraform task, and wrap it inside ACTaskSpec.
        """
        provider = self._pick_provider()
        task = self.registry.build_random_task(
            provider=provider,
            validator_sa=self.validator_sa,
            instruction_generator=self.instruction_generator,
        )
        payload = task.to_dict()
        prompt = payload.get("prompt") or payload.get("task", {}).get("prompt")
        params = dict(payload)
        # Avoid duplicating the prompt at both ACTaskSpec.prompt and params.prompt.
        params.pop("prompt", None)
        params.setdefault("kind", task.spec.kind)
        params.setdefault("task_id", task.spec.task_id)

        return ACTaskSpec(
            task_id=task.spec.task_id,
            provider=provider,
            kind=task.spec.kind,
            params=params,
            prompt=prompt,
            policy=ACPolicy(
                description="Terraform provisioning benchmark",
                max_cost="low",
                constraints={"engine": task.engine},
            ),
            verify_plan=VerifyPlan(
                kind="terraform-invariants",
                steps=[
                    "refresh terraform state",
                    "run terraform plan with no changes",
                    "assert invariants from spec.invariants",
                ],
            ),
            cost_tier="low",
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _pick_provider(self) -> str:
        providers = list(self.provider_weights.keys())
        weights = list(self.provider_weights.values())
        return random.choices(providers, weights=weights, k=1)[0]

    @staticmethod
    def _normalise_weights(
        weights: Optional[Dict[str, float]], providers: List[str]
    ) -> Dict[str, float]:
        if not weights:
            size = len(providers)
            return {provider: 1.0 / size for provider in providers}
        positive_total = sum(max(value, 0.0) for value in weights.values())
        if positive_total <= 0:
            raise ValueError("Provider weights must include at least one positive value.")
        distribution = {
            provider: max(weights.get(provider, 0.0), 0.0) / positive_total for provider in providers
        }
        provided_sum = sum(distribution.values())
        leftover = [p for p, w in distribution.items() if w == 0.0]
        residual = max(0.0, 1.0 - provided_sum)
        if leftover and residual > 0:
            share = residual / len(leftover)
            for provider in leftover:
                distribution[provider] = share
        elif not leftover and provided_sum > 0:
            # Normalise again to counter floating-point drift when all providers were weighted.
            distribution = {provider: weight / provided_sum for provider, weight in distribution.items()}
        return distribution
