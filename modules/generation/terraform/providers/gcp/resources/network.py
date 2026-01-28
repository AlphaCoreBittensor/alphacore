from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_network(ctx: TemplateContext) -> ResourceInstance:
    suffix = ctx.nonce[:6]
    name = f"net-{suffix}"
    invariant = Invariant(
        resource_type="google_compute_network",
        match={
            "values.name": name,
            "values.auto_create_subnetworks": False,
        },
    )
    hint = f"Create a custom VPC named {name} with auto subnet creation disabled."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"network": {"name": name}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="vpc_network",
            kind="vpc network",
            provides=("network",),
            builder=_build_network,
            base_hints=("Use a dedicated VPC instead of default networks.",),
            weight=1.0,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
