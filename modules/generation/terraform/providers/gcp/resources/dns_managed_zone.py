from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_dns_zone(ctx: TemplateContext) -> ResourceInstance:
    suffix = ctx.nonce[:6]
    zone_name = f"zone-{suffix}"
    dns_name = f"{suffix}.acore.example."

    invariant = Invariant(
        resource_type="google_dns_managed_zone",
        match={
            "values.name": zone_name,
            "values.dns_name": dns_name,
        },
    )
    hint = f"Stand up a Cloud DNS managed zone {zone_name} for the domain {dns_name}"
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"dns_zone": {"name": zone_name, "dns_name": dns_name}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="dns_managed_zone",
            kind="dns managed zone",
            provides=("dns_zone",),
            builder=_build_dns_zone,
            base_hints=("Use Cloud DNS with a dedicated zone name.",),
            weight=0.8,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
