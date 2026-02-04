from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_subnetwork(ctx: TemplateContext) -> ResourceInstance:
    network = ctx.shared.get("network")
    if not network:
        raise RuntimeError("network capability missing for subnetwork template.")
    suffix = ctx.nonce[:6]
    name = f"subnet-{suffix}"
    # If region is provided in shared context, use it; else pick randomly
    region = ctx.shared.get("region") or ctx.shared.get("subnetwork", {}).get("region")
    if not region:
        region, zone = helpers.pick_region_and_zone(ctx.rng)
    else:
        # If region is provided, pick a zone in that region
        zone = f"{region}-a"
    cidr = helpers.random_cidr_block(ctx.rng)
    invariant = Invariant(
        resource_type="google_compute_subnetwork",
        match={
            "values.name": name,
            "values.network": network["name"],
            "values.region": region,
            "values.ip_cidr_range": cidr,
        },
    )
    hint = f"Carve CIDR {cidr} inside {network['name']} and keep the subnetwork regional to {region}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"subnetwork": {"name": name, "region": region, "cidr": cidr, "zone": zone}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="subnetwork",
            kind="subnetwork",
            provides=("subnetwork",),
            requires=("network",),
            builder=_build_subnetwork,
            base_hints=("Attach the subnetwork to the custom VPC.",),
            weight=1.0,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
