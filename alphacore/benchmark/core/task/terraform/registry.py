"""
Registry for Terraform task generators.

Provider task files live under:
alphacore/benchmark/core/task/terraform/providers/<provider>/*.py
"""

from __future__ import annotations

import importlib
import random
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from alphacore.benchmark.core.task.instructions import TaskInstructionGenerator
from alphacore.benchmark.core.task.models import TerraformTask


task_builder_type = Callable[..., TerraformTask]
PROVIDERS_PACKAGE = "alphacore.benchmark.core.task.terraform.providers"


class TerraformTaskRegistry:
    def __init__(self) -> None:
        self.providers: Dict[str, Dict[str, task_builder_type]] = {}
        self.default_instruction_generator = TaskInstructionGenerator()
        self._scan_providers()

    def _scan_providers(self) -> None:
        """
        Discover provider directories by walking the providers package on disk.
        Each .py file that exposes a `build_task` callable becomes a candidate.
        """
        package = importlib.import_module(PROVIDERS_PACKAGE)

        for base in getattr(package, "__path__", []):
            base_path = Path(base)
            if not base_path.exists():
                continue
            for provider_dir in base_path.iterdir():
                if not provider_dir.is_dir() or provider_dir.name.startswith("_"):
                    continue
                provider_name = provider_dir.name
                self.providers.setdefault(provider_name, {})
                for task_file in provider_dir.glob("*.py"):
                    if task_file.stem.startswith("_"):
                        continue
                    module_name = f"{PROVIDERS_PACKAGE}.{provider_name}.{task_file.stem}"
                    module = importlib.import_module(module_name)
                    builder = getattr(module, "build_task", None)
                    if callable(builder):
                        self.providers[provider_name][task_file.stem] = self._wrap_builder(
                            builder, task_file.stem
                        )

    def get_task_builders(self, provider: str) -> Dict[str, task_builder_type]:
        return self.providers.get(provider, {})

    def get_all_providers(self) -> List[str]:
        return list(self.providers.keys())

    def get_all_tasks(self) -> Dict[str, Dict[str, task_builder_type]]:
        return self.providers

    def pick_random_task(self, provider: str) -> Tuple[str, task_builder_type]:
        builders = self.get_task_builders(provider)
        if not builders:
            raise RuntimeError(f"No task builders registered for provider '{provider}'.")
        name = random.choice(list(builders.keys()))
        return name, builders[name]

    def build_random_task(
        self,
        provider: str,
        validator_sa: str,
        instruction_generator: Optional[TaskInstructionGenerator] = None,
    ) -> TerraformTask:
        """
        Convenience helper that selects a builder, instantiates the task, and
        optionally generates natural language instructions.
        """
        task_name, builder = self.pick_random_task(provider)
        task = builder(validator_sa=validator_sa)
        if instruction_generator is not None:
            self._ensure_prompt(task, task_name, instruction_generator, replace=True)
        return task

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _wrap_builder(self, builder: task_builder_type, task_name: str) -> task_builder_type:
        def wrapped_builder(*args, **kwargs):
            task = builder(*args, **kwargs)
            self._ensure_prompt(task, task_name, self.default_instruction_generator)
            return task

        return wrapped_builder

    def _ensure_prompt(
        self,
        task: TerraformTask,
        task_name: str,
        generator: Optional[TaskInstructionGenerator],
        replace: bool = False,
    ) -> None:
        if not replace:
            prompt = task.spec.prompt or task.instructions
            if prompt:
                task.spec.prompt = prompt
                task.instructions = prompt
                return
        generator = generator or self.default_instruction_generator
        instructions = generator.generate(task, task_name=task_name)
        task.instructions = instructions
        task.spec.prompt = instructions


terraform_task_registry = TerraformTaskRegistry()
