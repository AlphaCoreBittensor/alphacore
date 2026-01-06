"""
Task generator that uses YAML configuration to generate random tasks.

This module provides a simple API for generating Terraform tasks based on
the hierarchical configuration in task_config.yaml.
"""

from __future__ import annotations

import os
import random
from typing import Optional

from modules.generation.instructions import TaskInstructionGenerator
from modules.generation.terraform.providers.gcp.task_bank import (
    GCPDynamicTaskBank,
)
from modules.generation.yaml_config import get_yaml_config
from modules.models import ACPolicy, ACTaskSpec, VerifyPlan
from modules.models import TerraformTask

DEFAULT_VALIDATOR_SA = os.getenv("ALPHACORE_VALIDATOR_SA", "")


class TaskGenerator:
    """
    Generate random Terraform tasks based on YAML configuration.

    This generator respects the hierarchical configuration:
    - Enabled/disabled providers
    - Enabled/disabled task banks (single vs composite)
    - Enabled/disabled resources
    - Enabled/disabled composition families
    - Resource count ranges for composite tasks

    Example:
        generator = TaskGenerator()
        task_spec = generator.generate()
    """

    def __init__(
        self,
        validator_sa: Optional[str] = None,
        instruction_generator: Optional[TaskInstructionGenerator] = None,
        config_path: Optional[str] = None,
    ) -> None:
        """
        Initialize task generator.

        Args:
            validator_sa: Service account email for validator. If None, uses ALPHACORE_VALIDATOR_SA (or empty string).
            instruction_generator: Custom instruction generator. If None, creates default.
            config_path: Path to YAML config file. If None, uses auto-discovery.
        """
        self.config = get_yaml_config(config_path)
        self.validator_sa = validator_sa or DEFAULT_VALIDATOR_SA
        self.instruction_generator = instruction_generator

        # Create instruction generator based on config
        if self.instruction_generator is None:
            # Always create instruction generator - config controls enable_llm flag
            llm_config = self.config.settings.llm
            self.instruction_generator = TaskInstructionGenerator(
                model=llm_config.model,
                temperature=llm_config.temperature,
                enable_llm=llm_config.enabled,
                fallback_on_failure=llm_config.fallback_on_failure
            )

        self._system_random = random.SystemRandom()
        self._task_banks = {}  # Cache for task bank instances

    def generate(self, provider: Optional[str] = None) -> ACTaskSpec:
        """
        Generate a random task based on configuration.

        Args:
            provider: Cloud provider ('gcp', 'aws', 'azure'). If None, picks randomly.

        Returns:
            ACTaskSpec ready to be sent to miners.

        Raises:
            RuntimeError: If no providers or task banks are enabled.
        """
        # Pick provider
        if provider is None:
            provider = self._pick_random_provider()
        elif not self.config.is_provider_enabled(provider):
            raise ValueError(f"Provider '{provider}' is not enabled in configuration")

        # Pick task bank
        task_bank_name = self._pick_random_task_bank(provider)

        # Generate task
        task = self._generate_terraform_task(provider, task_bank_name)

        # Convert to ACTaskSpec
        return self._to_ac_task_spec(task, provider)

    def generate_single_resource_task(self, provider: str = "gcp") -> ACTaskSpec:
        """
        Generate a single-resource task.

        Args:
            provider: Cloud provider (default: 'gcp')

        Returns:
            ACTaskSpec with a single resource.
        """
        if not self.config.is_task_bank_enabled(provider, "single_resource"):
            raise RuntimeError(f"single_resource task bank not enabled for {provider}")

        task = self._generate_terraform_task(provider, "single_resource")
        return self._to_ac_task_spec(task, provider)

    def generate_composite_task(self, provider: str = "gcp") -> ACTaskSpec:
        """
        Generate a multi-resource composite task.

        Args:
            provider: Cloud provider (default: 'gcp')

        Returns:
            ACTaskSpec with multiple related resources.
        """
        if not self.config.is_task_bank_enabled(provider, "composite_resource"):
            raise RuntimeError(f"composite_resource task bank not enabled for {provider}")

        task = self._generate_terraform_task(provider, "composite_resource")
        return self._to_ac_task_spec(task, provider)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _pick_random_provider(self) -> str:
        """Pick a random enabled provider."""
        enabled = self.config.get_enabled_providers()
        if not enabled:
            raise RuntimeError("No providers enabled in configuration")
        return self._system_random.choice(enabled)

    def _pick_random_task_bank(self, provider: str) -> str:
        """Pick a random enabled task bank for the given provider."""
        provider_config = self.config.providers.get(provider)
        if not provider_config or not provider_config.task_banks:
            raise RuntimeError(f"No task banks configured for provider '{provider}'")

        enabled_banks = [
            name for name, bank_config in provider_config.task_banks.items()
            if bank_config.enabled
        ]

        if not enabled_banks:
            raise RuntimeError(f"No task banks enabled for provider '{provider}'")

        return self._system_random.choice(enabled_banks)

    def _generate_terraform_task(self, provider: str, task_bank_name: str) -> TerraformTask:
        """Generate Terraform task using appropriate task bank."""
        if provider != "gcp":
            raise NotImplementedError(f"Provider '{provider}' not yet implemented")

        # Get or create task bank
        cache_key = f"{provider}:{task_bank_name}"
        if cache_key not in self._task_banks:
            self._task_banks[cache_key] = self._create_gcp_task_bank(task_bank_name)

        task_bank = self._task_banks[cache_key]
        task = task_bank.build_task(self.validator_sa)

        # Generate instructions
        if self.instruction_generator is not None:
            instructions = self.instruction_generator.generate(
                task,
                task_name=f"{provider}_{task_bank_name}"
            )
            task.instructions = instructions
            task.spec.prompt = instructions

            # Persist the final prompt to the configured task repository.
            try:
                from modules.generation.file_repository import get_file_task_repository

                repo = get_file_task_repository(self.config.settings.repository.path)
                repo.save(task)
            except Exception:
                pass

        return task

    def _create_gcp_task_bank(self, task_bank_name: str) -> GCPDynamicTaskBank:
        """Create a GCP task bank with proper configuration."""
        bank_config = self.config.providers["gcp"].task_banks[task_bank_name]

        if task_bank_name == "single_resource":
            # Single resource tasks
            from modules.generation.terraform.providers.gcp.compositions import (
                SINGLE_RESOURCE_FAMILIES,
            )

            # Filter families based on enabled resources
            enabled_resources = self.config.get_enabled_resources("gcp", "single_resource")
            families = self._filter_families_by_resources(
                SINGLE_RESOURCE_FAMILIES,
                enabled_resources
            )

            bank = GCPDynamicTaskBank(
                min_resources=1,
                max_resources=1,
                families=families,
            )

            # Filter templates based on enabled resources
            if enabled_resources is not None:
                self._filter_bank_templates(bank, enabled_resources)

            return bank

        elif task_bank_name == "composite_resource":
            # Composite resource tasks
            from modules.generation.terraform.providers.gcp.compositions import (
                COMPOSITE_FAMILIES,
            )

            # Filter families based on configuration
            enabled_families = self.config.get_enabled_families("gcp", "composite_resource")
            families = self._filter_composite_families(
                COMPOSITE_FAMILIES,
                enabled_families
            )

            min_res, max_res = self.config.get_resource_range("gcp", "composite_resource")

            bank = GCPDynamicTaskBank(
                min_resources=min_res,
                max_resources=max_res,
                families=families,
            )

            # For composite tasks, collect all resources from enabled families
            if enabled_families is not None:
                enabled_resources = set()
                for family in families:
                    enabled_resources.update(family.mandatory)
                    enabled_resources.update(family.optional)
                self._filter_bank_templates(bank, list(enabled_resources))

            return bank

        else:
            raise ValueError(f"Unknown task bank: {task_bank_name}")

    def _filter_bank_templates(self, bank: GCPDynamicTaskBank, enabled_resources: list[str]) -> None:
        """Filter task bank templates to only include enabled resources."""
        enabled_set = set(enabled_resources)
        # Force template loading
        _ = bank.templates
        # Filter the loaded templates
        bank._templates = {
            key: tmpl for key, tmpl in bank._templates.items()
            if key in enabled_set
        }
        if not bank._templates:
            raise RuntimeError(
                f"No templates available after filtering. "
                f"Enabled resources: {enabled_resources}"
            )

    def _filter_families_by_resources(
        self,
        families: tuple[CompositionFamily, ...],
        enabled_resources: list[str] | None
    ) -> list[CompositionFamily]:
        """Filter families to only include those with enabled resources."""
        if enabled_resources is None:
            # None means all resources enabled
            return list(families)

        enabled_set = set(enabled_resources)
        filtered = []

        for family in families:
            # Check if all mandatory resources are enabled
            if all(res in enabled_set for res in family.mandatory):
                filtered.append(family)

        return filtered

    def _filter_composite_families(
        self,
        all_families: tuple[CompositionFamily, ...],
        enabled_family_names: list[str] | None
    ) -> list[CompositionFamily]:
        """Filter composite families based on configuration."""
        if enabled_family_names is None:
            # None means all families enabled
            return list(all_families)

        enabled_set = set(enabled_family_names)
        return [f for f in all_families if f.name in enabled_set]

    def _to_ac_task_spec(self, task: TerraformTask, provider: str) -> ACTaskSpec:
        """Convert TerraformTask to ACTaskSpec."""
        payload = task.to_dict()
        prompt = payload.get("prompt") or payload.get("task", {}).get("prompt")
        params = dict(payload)
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


# Convenience function for quick task generation
def generate_task(
    provider: Optional[str] = None,
    validator_sa: Optional[str] = None,
    config_path: Optional[str] = None,
) -> ACTaskSpec:
    """
    Generate a random task with minimal boilerplate.

    Args:
        provider: Cloud provider ('gcp', 'aws', 'azure'). If None, picks randomly.
        validator_sa: Service account email. If None, uses config setting.
        config_path: Path to YAML config. If None, uses auto-discovery.

    Returns:
        ACTaskSpec ready to send to miners.

    Example:
        task = generate_task()
        task = generate_task(provider='gcp')
        task = generate_task(validator_sa='custom@example.com')
    """
    generator = TaskGenerator(
        validator_sa=validator_sa,
        config_path=config_path,
    )
    return generator.generate(provider=provider)
