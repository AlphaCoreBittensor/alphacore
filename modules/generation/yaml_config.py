"""
YAML-based hierarchical task configuration.

Provides a cleaner, more maintainable way to configure task generation
compared to environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class FamilyConfig:
    """Configuration for a composition family."""
    enabled: bool = True
    resources: List[str] = field(default_factory=list)


@dataclass
class TaskBankConfig:
    """Configuration for a task bank (single or composite)."""
    enabled: bool = True
    resources: List[str] = field(default_factory=list)
    families: Dict[str, FamilyConfig] = field(default_factory=dict)
    min_resources: int = 1
    max_resources: int = 3


@dataclass
class ProviderConfig:
    """Configuration for a cloud provider."""
    enabled: bool = True
    task_banks: Dict[str, TaskBankConfig] = field(default_factory=dict)


@dataclass
class LLMConfig:
    """LLM configuration for instruction generation."""
    enabled: bool = True
    model: str = "gpt-4o-mini"
    temperature: float = 0.6
    fallback_on_failure: bool = False


@dataclass
class RepositoryConfig:
    """Task repository configuration."""
    path: str = "."


@dataclass
class SettingsConfig:
    """Global settings."""
    repository: RepositoryConfig = field(default_factory=RepositoryConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


@dataclass
class YamlTaskConfig:
    """
    Hierarchical task configuration loaded from YAML.

    Structure:
        providers -> task_banks -> families -> resources
    """
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    settings: SettingsConfig = field(default_factory=SettingsConfig)

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> YamlTaskConfig:
        """Load configuration from YAML file."""
        if yaml is None:
            raise ImportError("PyYAML is required. Install with: pip install pyyaml")

        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {yaml_path}")

        with open(path, 'r') as f:
            data = yaml.safe_load(f) or {}

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict) -> YamlTaskConfig:
        """Parse configuration dictionary."""
        providers = {}
        for provider_name, provider_data in data.get('providers', {}).items():
            if not isinstance(provider_data, dict):
                continue

            task_banks = {}
            for bank_name, bank_data in provider_data.get('task_banks', {}).items():
                if not isinstance(bank_data, dict):
                    continue

                families = {}
                for family_name, family_data in bank_data.get('families', {}).items():
                    if not isinstance(family_data, dict):
                        continue
                    families[family_name] = FamilyConfig(
                        enabled=family_data.get('enabled', True),
                        resources=family_data.get('resources', [])
                    )

                task_banks[bank_name] = TaskBankConfig(
                    enabled=bank_data.get('enabled', True),
                    resources=bank_data.get('resources', []),
                    families=families,
                    min_resources=bank_data.get('min_resources', 1),
                    max_resources=bank_data.get('max_resources', 3)
                )

            providers[provider_name] = ProviderConfig(
                enabled=provider_data.get('enabled', True),
                task_banks=task_banks
            )

        # Parse settings
        settings_data = data.get('settings', {})
        llm_data = settings_data.get('llm', {})
        llm_config = LLMConfig(
            enabled=llm_data.get('enabled', True),
            model=llm_data.get('model', 'gpt-4o-mini'),
            temperature=llm_data.get('temperature', 0.6),
            fallback_on_failure=llm_data.get('fallback_on_failure', False)
        )

        # Parse repository config
        repo_data = settings_data.get('repository', {})
        repo_config = RepositoryConfig(
            path=repo_data.get('path', '.')
        )

        settings = SettingsConfig(repository=repo_config, llm=llm_config)

        return cls(providers=providers, settings=settings)

    # =============================================================================
    # Query Methods
    # =============================================================================

    def is_provider_enabled(self, provider: str) -> bool:
        """Check if a provider is enabled."""
        if provider not in self.providers:
            return False
        return self.providers[provider].enabled

    def get_enabled_providers(self) -> List[str]:
        """Get list of enabled providers."""
        return [name for name, config in self.providers.items() if config.enabled]

    def is_task_bank_enabled(self, provider: str, bank: str) -> bool:
        """Check if a task bank is enabled for a provider."""
        if not self.is_provider_enabled(provider):
            return False
        provider_config = self.providers[provider]
        if bank not in provider_config.task_banks:
            return False
        return provider_config.task_banks[bank].enabled

    def get_enabled_resources(self, provider: str, bank: str) -> Optional[List[str]]:
        """
        Get enabled resources for a task bank.

        Returns:
            List of enabled resources, or None if all resources are enabled
        """
        if not self.is_task_bank_enabled(provider, bank):
            return []

        bank_config = self.providers[provider].task_banks[bank]
        if not bank_config.resources:
            return None  # All resources enabled
        return bank_config.resources

    def get_enabled_families(self, provider: str, bank: str) -> List[str]:
        """Get list of enabled composition families."""
        if not self.is_task_bank_enabled(provider, bank):
            return []

        bank_config = self.providers[provider].task_banks[bank]
        return [name for name, config in bank_config.families.items() if config.enabled]

    def is_family_enabled(self, provider: str, bank: str, family: str) -> bool:
        """Check if a composition family is enabled."""
        if not self.is_task_bank_enabled(provider, bank):
            return False

        bank_config = self.providers[provider].task_banks[bank]
        if family not in bank_config.families:
            return True  # If not explicitly configured, it's enabled
        return bank_config.families[family].enabled

    def get_family_resources(self, provider: str, bank: str, family: str) -> Optional[List[str]]:
        """Get resources for a specific family."""
        if not self.is_task_bank_enabled(provider, bank):
            return []

        bank_config = self.providers[provider].task_banks[bank]
        if family not in bank_config.families:
            return None  # All resources enabled

        return bank_config.families[family].resources or None

    def get_resource_range(self, provider: str, bank: str) -> tuple[int, int]:
        """Get min/max resource count for composite tasks."""
        if not self.is_task_bank_enabled(provider, bank):
            return (1, 1)

        bank_config = self.providers[provider].task_banks[bank]
        return (bank_config.min_resources, bank_config.max_resources)

    def is_resource_enabled(self, provider: str, bank: str, resource: str) -> bool:
        """Check if a specific resource is enabled."""
        enabled_resources = self.get_enabled_resources(provider, bank)
        if enabled_resources is None:
            return True  # All resources enabled
        return resource in enabled_resources


# Global configuration instance
_yaml_config: Optional[YamlTaskConfig] = None


def get_yaml_config(config_path: Optional[str | Path] = None) -> YamlTaskConfig:
    """
    Get the global YAML configuration.

    Args:
        config_path: Path to YAML config file. If None, uses default locations:
                    1. ALPHACORE_CONFIG environment variable
                    2. ./task_config.yaml (current directory)
                    3. ~/.alphacore/task_config.yaml (home directory)

    Returns:
        YamlTaskConfig instance
    """
    global _yaml_config

    if _yaml_config is not None and config_path is None:
        return _yaml_config

    # Determine config path
    if config_path is None:
        # Check environment variable
        env_path = os.getenv('ALPHACORE_CONFIG')
        if env_path:
            config_path = Path(env_path)
        else:
            # Check default locations
            # Note: Many callers run from the repo root, while the default
            # config lives at `modules/task_config.yaml`. Search both.
            repo_root = Path(__file__).resolve().parents[2]
            candidates = [
                Path.cwd() / 'task_config.yaml',
                repo_root / 'modules' / 'task_config.yaml',
                repo_root / 'task_config.yaml',
                Path.home() / '.alphacore' / 'task_config.yaml',
            ]
            for candidate in candidates:
                if candidate.exists():
                    config_path = candidate
                    break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Create task_config.yaml or set ALPHACORE_CONFIG environment variable."
        )

    _yaml_config = YamlTaskConfig.from_yaml(config_path)
    return _yaml_config


def reset_yaml_config() -> None:
    """Reset the global YAML configuration cache."""
    global _yaml_config
    _yaml_config = None


def set_yaml_config(config: YamlTaskConfig) -> None:
    """Set a custom YAML configuration."""
    global _yaml_config
    _yaml_config = config
