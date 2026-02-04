from __future__ import annotations

from modules.generation.terraform.providers.gcp import helpers
from modules.models import Invariant
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _basic_instance(ctx: TemplateContext) -> ResourceInstance:
    name = helpers.rfc1035_name(ctx.rng)
    _, zone = helpers.pick_region_and_zone(ctx.rng)
    machine_type = helpers.pick_machine_type(ctx.rng)
    token = f"{ctx.task_id[:6]}-{ctx.nonce[:6]}"
    startup = helpers.startup_script(token, ctx.rng)

    invariant = Invariant(
        resource_type="google_compute_instance",
        match={
            "values.name": name,
            "values.zone": zone,
            "values.machine_type": machine_type,
            "values.metadata_startup_script": startup,
        },
    )
    hint = f"Launch a lone VM in {zone} using machine type {machine_type} and include the tokenised startup script."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"instance": {"name": name, "zone": zone}},
    )


def _networked_instance(ctx: TemplateContext) -> ResourceInstance:
    subnet = ctx.shared.get("subnetwork")
    if not subnet:
        raise RuntimeError("subnetwork capability missing for networked instance template.")
    zone = subnet.get("zone") or subnet.get("region") + "-a"
    name = helpers.rfc1035_name(ctx.rng)
    machine_type = helpers.pick_machine_type(ctx.rng)
    token = f"{ctx.task_id[:6]}-{ctx.nonce[:6]}"
    startup = helpers.startup_script(f"{token}-net", ctx.rng)

    invariant = Invariant(
        resource_type="google_compute_instance",
        match={
            "values.name": name,
            "values.zone": zone,
            "values.machine_type": machine_type,
            "values.network_interface.0.subnetwork": subnet["name"],
            "values.metadata_startup_script": startup,
        },
    )
    hint = f"Attach the VM {name} to subnetwork {subnet['name']} in zone {zone}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"instance": {"name": name, "zone": zone}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="compute_instance_basic",
            kind="basic virtual machine",
            provides=("instance",),
            builder=_basic_instance,
            base_hints=("Provision exactly one Compute Engine VM.",),
            weight=1.2,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        ),
        ResourceTemplate(
            key="compute_instance_networked",
            kind="networked virtual machine",
            provides=("instance",),
            requires=("subnetwork",),
            builder=_networked_instance,
            base_hints=("Use the dedicated VPC stack before adding the VM.",),
            weight=0.9,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        ),
    ]
