from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_service_account(ctx: TemplateContext) -> ResourceInstance:
    suffix = ctx.nonce[:8]
    account_id = helpers.service_account_id(suffix)
    display_name = helpers.random_label(ctx.rng, min_len=8, max_len=16)
    # Keep description stable and short; avoid resource-type hints.
    description = helpers.random_label(ctx.rng, min_len=10, max_len=18)

    invariant = Invariant(
        resource_type="google_service_account",
        match={
            "values.account_id": account_id,
            "values.display_name": display_name,
            "values.description": description,
        },
    )
    hint = f"Create a standalone service account {account_id} named {display_name}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={
            "service_account": {
                "account_id": account_id,
                "display_name": display_name,
                "description": description,
            }
        },
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="service_account",
            kind="service account",
            provides=("service_account",),
            builder=_build_service_account,
            base_hints=("Expose a fresh service account for future bindings.",),
            weight=1.0,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
