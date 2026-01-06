from __future__ import annotations

from modules.generation.terraform.providers.gcp import compositions
from modules.generation.terraform.providers.gcp.task_bank import (
    GCPDynamicTaskBank,
)
from modules.generation.yaml_config import get_yaml_config


def _build_bank() -> GCPDynamicTaskBank:
    config = get_yaml_config()
    enabled_families = config.get_enabled_families("gcp", "composite_resource")
    if enabled_families is None:
        families = list(compositions.COMPOSITE_FAMILIES)
    else:
        allowed = set(enabled_families)
        families = [fam for fam in compositions.COMPOSITE_FAMILIES if fam.name in allowed]
        if not families:
            raise RuntimeError("No composite families enabled after filtering.")

    min_res, max_res = config.get_resource_range("gcp", "composite_resource")
    bank = GCPDynamicTaskBank(
        min_resources=min_res,
        max_resources=max_res,
        families=families,
    )

    # Filter templates to only those used by enabled families.
    enabled_resources = set()
    for fam in families:
        enabled_resources.update(fam.mandatory)
        enabled_resources.update(fam.optional)

    if enabled_resources:
        _ = bank.templates
        bank._templates = {
            key: tmpl for key, tmpl in bank._templates.items() if key in enabled_resources
        }
        if not bank._templates:
            raise RuntimeError("No templates available for enabled composite families.")

    return bank


_BANK = _build_bank()


def build_task(validator_sa: str):
    return _BANK.build_task(validator_sa)
