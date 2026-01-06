from __future__ import annotations

from modules.generation.terraform.providers.gcp import compositions
from modules.generation.terraform.providers.gcp.task_bank import (
    GCPDynamicTaskBank,
)
from modules.generation.yaml_config import get_yaml_config


def _build_bank() -> GCPDynamicTaskBank:
    config = get_yaml_config()
    enabled_resources = config.get_enabled_resources("gcp", "single_resource")
    families = list(compositions.SINGLE_RESOURCE_FAMILIES)

    if enabled_resources is not None:
        allowed = set(enabled_resources)
        families = [
            fam for fam in families
            if all(res in allowed for res in fam.mandatory)
        ]
        if not families:
            raise RuntimeError("No single-resource families enabled after filtering.")

    bank = GCPDynamicTaskBank(
        min_resources=1,
        max_resources=1,
        families=families,
    )

    if enabled_resources is not None:
        allowed = set(enabled_resources)
        # Force template load before filtering
        _ = bank.templates
        bank._templates = {key: tmpl for key, tmpl in bank._templates.items() if key in allowed}
        if not bank._templates:
            raise RuntimeError("No templates available for enabled single-resource list.")

    return bank

_BANK = _build_bank()


def build_task(validator_sa: str):
    return _BANK.build_task(validator_sa)
