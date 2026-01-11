from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_firewall(ctx: TemplateContext) -> ResourceInstance:
    network = ctx.shared.get("network")
    if not network:
        raise RuntimeError("network capability missing for firewall template.")
    suffix = ctx.nonce[:6]
    profile = helpers.random_firewall_profile(ctx.rng)
    name = f"fw-{profile.label}-{suffix}"
    allow_match: dict[str, object] = {
        "values.allow.0.protocol": profile.allow_protocol,
    }
    if profile.allow_ports:
        allow_match["values.allow.0.ports.0"] = profile.allow_ports[0]
    invariant = Invariant(
        resource_type="google_compute_firewall",
        match={
            "values.name": name,
            "values.network": network["name"],
            "values.direction": profile.direction,
            "values.priority": profile.priority,
            "values.disabled": profile.disabled,
            **allow_match,
        },
    )
    allow_hint = (
        f"{profile.allow_protocol}/{profile.allow_ports[0]}"
        if profile.allow_ports
        else profile.allow_protocol
    )
    hint = (
        f"Add a {profile.direction.lower()} firewall {name} on {network['name']} with priority {profile.priority} "
        f"allowing {allow_hint}."
    )
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"firewall": {"name": name}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="firewall_rule",
            kind="firewall rule",
            provides=("firewall",),
            requires=("network",),
            builder=_build_firewall,
            base_hints=("Restrict ingress on the bespoke VPC.",),
            weight=0.8,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
