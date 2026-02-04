from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_dns_record(ctx: TemplateContext) -> ResourceInstance:
    zone = ctx.shared.get("dns_zone")
    zone_name = (zone or {}).get("name") or helpers.dns_zone_name(ctx.nonce[:8])
    zone_dns = (zone or {}).get("dns_name") or f"{ctx.nonce[:6]}.acore.example."
    zone_dns = zone_dns if zone_dns.endswith(".") else f"{zone_dns}."

    record_type = helpers.dns_record_type(ctx.rng)
    record_label = helpers.dns_label(ctx.rng)
    record_name = f"{record_label}.{zone_dns}"
    ttl = helpers.dns_record_ttl(ctx.rng)
    # Deterministic, per-task record data so tasks don't all share the same placeholder.
    token = ctx.nonce[:6]
    if record_type == "A":
        rrdatas = [f"192.0.2.{ctx.rng.randint(1, 254)}"]
    elif record_type == "CNAME":
        rrdatas = [f"alias-{token}.example.com."]
    elif record_type == "TXT":
        rrdatas = [helpers.txt_rrdata(ctx.rng)]
    elif record_type == "MX":
        rrdatas = [f"10 mail-{token}.example.com."]
    else:
        rrdatas = [f"192.0.2.1"]

    invariant = Invariant(
        resource_type="google_dns_record_set",
        match={
            "values.name": record_name,
            "values.type": record_type,
            "values.ttl": ttl,
            "values.managed_zone": zone_name,
            "values.rrdatas": rrdatas,
        },
    )
    hint = f"Create a DNS {record_type} record {record_name} in zone {zone_name} with TTL {ttl} and data {rrdatas}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"dns_record_set": {"name": record_name, "type": record_type}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="dns_record_set",
            kind="dns record set",
            provides=("dns_record_set",),
            requires=("dns_zone",),
            builder=_build_dns_record,
            base_hints=("Add DNS record to existing managed zone.",),
            weight=0.85,
        )
    ]
