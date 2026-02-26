"""
Registry for Terraform task generators.

Provider task files live under:
modules/generation/terraform/providers/<provider>/*.py
"""

from __future__ import annotations

import importlib
import os
import random
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from modules.generation.instructions import TaskInstructionGenerator
from modules.models import TerraformTask
from modules.generation.yaml_config import get_yaml_config
from modules.generation.file_repository import get_file_task_repository


task_builder_type = Callable[..., TerraformTask]
PROVIDERS_PACKAGE = "modules.generation.terraform.providers"


class TerraformTaskRegistry:
    def __init__(
        self, instruction_generator: Optional[TaskInstructionGenerator] = None
    ) -> None:
        self.providers: Dict[str, Dict[str, task_builder_type]] = {}
        self.default_instruction_generator = instruction_generator or TaskInstructionGenerator()
        self._scan_providers()

    def _scan_providers(self) -> None:
        """
        Discover provider directories by walking the providers package on disk.
        Each .py file that exposes a `build_task` callable becomes a candidate.
        Respects YAML configuration for enabled/disabled providers.
        """
        config = get_yaml_config()
        package = importlib.import_module(PROVIDERS_PACKAGE)

        for base in getattr(package, "__path__", []):
            base_path = Path(base)
            if not base_path.exists():
                continue
            for provider_dir in base_path.iterdir():
                if not provider_dir.is_dir() or provider_dir.name.startswith("_"):
                    continue
                provider_name = provider_dir.name

                # Skip disabled providers
                if not config.is_provider_enabled(provider_name):
                    continue

                self.providers.setdefault(provider_name, {})
                for task_file in provider_dir.rglob("*.py"):
                    if task_file.stem.startswith("_") or task_file.stem == "__init__":
                        continue
                    # Skip files living in private folders such as __pycache__
                    if "__pycache__" in task_file.parts:
                        continue
                    relative_module = task_file.relative_to(provider_dir).with_suffix("")
                    module_suffix = ".".join(relative_module.parts)
                    task_key = module_suffix

                    module_name = f"{PROVIDERS_PACKAGE}.{provider_name}.{module_suffix}"
                    module = importlib.import_module(module_name)
                    builder = getattr(module, "build_task", None)
                    if callable(builder):
                        self.providers[provider_name][task_key] = self._wrap_builder(
                            builder, task_key
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
        # Optional task bank filter for local testing (e.g., composite-only tasks).
        prefer_bank = (os.getenv("ALPHACORE_TASK_BANK") or "").strip().lower() or "composite"
        filtered = builders
        if prefer_bank in {"composite", "composite_resource"}:
            filtered = {k: v for k, v in builders.items() if "composite" in k}
        elif prefer_bank in {"single", "single_resource"}:
            filtered = {k: v for k, v in builders.items() if "single" in k}
        if filtered:
            builders = filtered
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
        # `builder` is typically wrapped to auto-generate prompts using the registry's
        # default instruction generator. When a caller provides an explicit
        # instruction_generator we want to avoid generating prompts twice (and avoid
        # failures being attributed to the wrong generator / stale traces).
        raw_builder = getattr(builder, "_alphacore_raw_builder", None)
        effective_builder = raw_builder or builder
        task = effective_builder(validator_sa=validator_sa)
        generator = instruction_generator or self.default_instruction_generator
        self._ensure_prompt(task, task_name, generator, replace=True)
        return task

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _wrap_builder(self, builder: task_builder_type, task_name: str) -> task_builder_type:
        def wrapped_builder(*args, **kwargs):
            task = builder(*args, **kwargs)
            self._ensure_prompt(task, task_name, self.default_instruction_generator)
            return task

        # Expose the raw builder so callers can avoid double prompt generation.
        try:
            setattr(wrapped_builder, "_alphacore_raw_builder", builder)
        except Exception:
            pass
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
        self._persist_task(task)

    @staticmethod
    def _persist_task(task: TerraformTask) -> None:
        """
        Best-effort persist of the generated task into the configured file repository.

        This keeps `miner.json` in sync with the final prompt (LLM or fallback) and
        avoids writing placeholder/null prompts before instructions are generated.
        """
        try:
            config = get_yaml_config()
            repo_path = getattr(getattr(config, "settings", None), "repository", None)
            path = getattr(repo_path, "path", None) if repo_path is not None else None
            if not path:
                return
            repo = get_file_task_repository(path)
            repo.save(task)
        except Exception:
            return


terraform_task_registry = TerraformTaskRegistry()
